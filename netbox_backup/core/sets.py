"""
Reading sets back: listing, manifest loading, level-1 integrity checks, and fetch-then-decrypt.

Restores spool every object to local disk first (parallel pg_restore needs a seekable file), check its size and
ciphertext SHA-384 against the signed manifest, and only then decrypt. gpg emits plaintext before its integrity
check finishes, so decrypted output goes to a staging file that is used only after gpg exits successfully.
"""
import contextlib
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..storage import ObjectNotFound
from . import layout
from . import manifest as manifest_mod

CHUNK = 1024 * 1024


class SetError(Exception):
    pass


@dataclass
class SetListing:
    instance_id: str
    set_id: str
    objects: dict = field(default_factory=dict)  # object name -> ObjectInfo

    @property
    def complete(self):
        return layout.COMPLETE in self.objects and layout.MANIFEST in self.objects

    @property
    def created(self):
        return layout.ulid_time(self.set_id)

    @property
    def size(self):
        return sum(o.size for o in self.objects.values())


def list_instances(backend):
    instances = set()
    for info in backend.list(''):
        parts = info.key.split('/')
        if len(parts) == 2 and parts[1] == layout.INSTANCE_FILE:
            instances.add(parts[0])
        elif len(parts) > 2 and parts[1] in ('sets', 'journal'):
            instances.add(parts[0])
    return sorted(instances)


def list_sets(backend, instance_id, include_incomplete=True):
    """
    Every set below the instance, oldest first. Listing needs no key and no database.
    """
    sets = {}
    for info in backend.list(layout.sets_prefix(instance_id)):
        parsed = layout.parse_set_key(info.key)
        if not parsed or parsed[0] != instance_id:
            continue
        listing = sets.setdefault(parsed[1], SetListing(instance_id, parsed[1]))
        listing.objects[parsed[2]] = info
    result = sorted(sets.values(), key=lambda s: s.set_id)
    return result if include_incomplete else [s for s in result if s.complete]


def resolve(backend, instance_id, identifier='latest'):
    """
    Find a complete set by ULID, unique ULID prefix, or "latest".
    """
    sets = list_sets(backend, instance_id, include_incomplete=False)
    if not sets:
        raise SetError(f"No complete sets for instance {instance_id} on {backend.describe()}")
    if identifier in (None, '', 'latest'):
        return sets[-1]
    matches = [s for s in sets if s.set_id.startswith(str(identifier).upper())]
    if len(matches) != 1:
        raise SetError(f"{'No' if not matches else 'More than one'} complete set matches {identifier!r}")
    return matches[0]


def load_manifest(backend, instance_id, set_id):
    """
    Read manifest.json and check it against _COMPLETE. The signature is checked separately (verify_manifest).
    """
    try:
        raw = backend.get_bytes(layout.object_key(instance_id, set_id, layout.MANIFEST))
    except ObjectNotFound:
        raise SetError(f"Set {set_id} has no manifest") from None
    try:
        marker = backend.get_bytes(layout.object_key(instance_id, set_id, layout.COMPLETE))
    except ObjectNotFound:
        raise SetError(f"Set {set_id} is incomplete (no _COMPLETE)") from None
    manifest_mod.check_complete(raw, marker)
    manifest = manifest_mod.parse(raw)
    if manifest.set_id != set_id or manifest.instance_id != instance_id:
        raise SetError(f"Manifest at {set_id} describes {manifest.instance_id}/{manifest.set_id}")
    return manifest


def level1(backend, manifest, keyring, target=None, expected=None):
    """
    Level-1 integrity: _COMPLETE hash (checked by load_manifest), header signature, and per object the size and the
    locally computed checksum against what the backend reports, plus retention status on immutable targets. Needs
    no private key. Returns a list of {'check', 'ok', 'detail'}; the overall verdict is all(ok).

    The manifest records the checksums computed while writing to the primary target. A replica's objects were
    written by a copy job with its own part layout, so its checks take the copy's records as expected
    ({object name: {'checksum': {...}}}); a replica without records (e.g. a rebuilt catalog) is checked by size.
    """
    results = []

    def record(check, ok, detail=''):
        results.append({'check': check, 'ok': bool(ok), 'detail': detail})

    try:
        signer = manifest_mod.verify(manifest, keyring)
        record('signature', True, signer)
    except Exception as e:
        record('signature', False, str(e))
    for name, obj in sorted(manifest.objects.items()):
        info = backend.stat(obj['key'])
        if info is None:
            record(f'{name}: present', False, obj['key'])
            continue
        record(f'{name}: size', info.size == obj['size'], f"{info.size} bytes, expected {obj['size']}")
        checksum = obj.get('checksum') if expected is None else (expected.get(name) or {}).get('checksum')
        if checksum and info.checksum_unavailable:
            record(f'{name}: checksum', True, f'size only ({info.checksum_unavailable})')
        elif checksum:
            record(f"{name}: {checksum['type']}", info.checksum == checksum['value'],
                   f"server {info.checksum}, local {checksum['value']}")
        elif expected is not None and backend.capabilities.object_checksum:
            record(f'{name}: checksum', True, 'size only (no copy record for this replica)')
        else:
            record(f'{name}: checksum', True, 'size only (backend reports no checksum)')
        if target is not None and target.retention_days:
            record(f'{name}: locked', info.retain_until is not None and info.retain_until > datetime.now(UTC),
                   f'retain until {info.retain_until:%Y-%m-%d}' if info.retain_until else 'no retention lock')
    return results


def fetch(backend, obj, dest):
    """
    Download one object to dest while hashing; delete it and raise on a size or SHA-384 mismatch.
    """
    digest = hashlib.sha384()
    size = 0
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'wb') as out, backend.open_read(obj['key']) as src:
            for chunk in iter(lambda: src.read(CHUNK), b''):
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if size != obj['size'] or digest.hexdigest() != obj['sha384']:
            raise SetError(
                f"{obj['key']}: downloaded {size} bytes with SHA-384 {digest.hexdigest()[:16]}…, expected "
                f"{obj['size']} bytes with {obj['sha384'][:16]}…"
            )
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(dest)
        raise
    return dest


def decrypt_file(keyring, src, dest):
    """
    Decrypt src into dest via a staging file; dest appears only after gpg verified the object's integrity.
    """
    staging = f'{dest}.staging'
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with open(src, 'rb') as fin, os.fdopen(fd, 'wb') as fout:
            proc = keyring.decrypt_process(stdin=fin, stdout=fout)
            keyring.check_decryption(proc)
        os.replace(staging, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(staging)
        raise
    return dest


def fetch_and_decrypt(backend, keyring, manifest, components, spool, keep_ciphertext=False, keys_used=None):
    """
    Fetch every requested component into spool, verify it, then decrypt it. Returns {component: plaintext path}.
    Every object is downloaded and hash-checked before the first one is decrypted.
    """
    os.makedirs(spool, mode=0o700, exist_ok=True)
    fetched = {}
    for component in components:
        name = layout.OBJECT_NAMES[component]
        obj = manifest.objects.get(name)
        if obj is None:
            raise SetError(f"Set {manifest.set_id} has no {component} component")
        fetched[component] = fetch(backend, obj, os.path.join(spool, name))
    plaintext = {}
    for component, path in fetched.items():
        plaintext[component] = decrypt_file(keyring, path, path.removesuffix('.enc'))
        if keys_used is not None:
            keys_used[component] = keyring.last_decryption_key
        if not keep_ciphertext:
            os.unlink(path)
    return plaintext


def read_instance(backend, instance_id):
    try:
        return json.loads(backend.get_bytes(layout.instance_key(instance_id)))
    except ObjectNotFound:
        return None
