"""
Replicas. A policy writes to one primary target; ReplicateJob then copies each set to every replica: the objects
first, then manifest.json, then _COMPLETE, so a half-copied set is invisible on the replica just as on the primary.

Within one provider (S3 to S3 on the same endpoint, GCS to GCS) the copy is server-side; across providers, or when
the replica's credential cannot read the source, the objects stream through the worker. Either way every object is
read back once and its SHA-384 checked against the signed manifest, so a replica counts only once it provably
holds the set. That read also yields the checksum level 1 compares on the replica: a server-side copy gets its own
part layout (S3 UploadPartCopy) or the whole-object MD5 (GCS), so the copy job computes the whole-object MD5 and the
part MD5s for the part size it used, and records them in the copy's catalog row, not in the signed manifest.

Each copy has its own status. A failed replica leaves the set "partial"; the primary does not prune a set until
every replica holds it, and the scheduler retries failed copies.
"""
import hashlib
import logging
from datetime import timedelta

from django.utils import timezone

from .. import conf
from ..choices import CopyStatusChoices, SetStatusChoices, VerifyLevelChoices
from ..core import layout, sets
from ..core import manifest as manifest_mod
from ..core.gpg import Keyring
from ..storage import ObjectExists, StorageError
from ..storage.base import md5_etag
from . import catalog
from .locks import advisory_lock

logger = logging.getLogger('netbox_backup')

CHUNK = 1024 * 1024
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(hours=1)


class ReplicationError(Exception):
    pass


class _Hasher:
    """
    SHA-384 and MD5 over a stream, plus the MD5 of every part_size-sized part (S3's multipart ETag).
    """
    def __init__(self, part_size=None):
        self.sha384 = hashlib.sha384()
        self.md5 = hashlib.md5(usedforsecurity=False)
        self.part_size = part_size
        self.parts = []
        self.part = hashlib.md5(usedforsecurity=False)
        self.in_part = 0
        self.size = 0

    def update(self, data):
        self.sha384.update(data)
        self.md5.update(data)
        self.size += len(data)
        if not self.part_size:
            return
        view = memoryview(data)
        while view:
            take = min(len(view), self.part_size - self.in_part)
            self.part.update(view[:take])
            self.in_part += take
            view = view[take:]
            if self.in_part == self.part_size:
                self.parts.append(self.part.digest())
                self.part = hashlib.md5(usedforsecurity=False)
                self.in_part = 0

    def part_digests(self):
        return self.parts + ([self.part.digest()] if self.in_part else [])


def _read_back(backend, key, part_size=None):
    hasher = _Hasher(part_size)
    with backend.open_read(key) as f:
        for chunk in iter(lambda: f.read(CHUNK), b''):
            hasher.update(chunk)
    return hasher


def _checksum(backend, hasher, part_size):
    kind = backend.capabilities.object_checksum
    if kind == 'etag-md5':
        if part_size:
            return {'type': kind, 'value': md5_etag(hasher.part_digests(), multipart=True)}
        return {'type': kind, 'value': hasher.md5.hexdigest()}
    if kind == 'md5':
        return {'type': kind, 'value': hasher.md5.hexdigest()}
    return None


def _check(obj, hasher, where):
    if hasher.size != obj['size'] or hasher.sha384.hexdigest() != obj['sha384']:
        raise ReplicationError(f"{obj['key']} on {where}: {hasher.size} bytes with SHA-384 "
                               f"{hasher.sha384.hexdigest()[:16]}…, expected {obj['size']} bytes with "
                               f"{obj['sha384'][:16]}…")


def copy_object(src, dst, obj, log=None):
    """
    Put one object of the set on dst and prove it is there. Returns the copy record for level 1.
    """
    key = obj['key']
    where = dst.target.name
    existing = dst.stat(key)
    if existing is not None:
        # A rerun after a failed attempt: keep what is there only if it is the right object
        hasher = _read_back(dst, key)
        _check(obj, hasher, where)
        checksum = {'type': dst.capabilities.object_checksum, 'value': existing.checksum} \
            if dst.capabilities.object_checksum and existing.checksum else None
        return {'size': hasher.size, 'checksum': checksum, 'part_size': None, 'method': 'existing'}
    try:
        result = dst.copy_from(src, key, obj['size'])
        part_size = result.get('part_size')
        hasher = _read_back(dst, key, part_size)
        _check(obj, hasher, where)
        return {'size': hasher.size, 'checksum': _checksum(dst, hasher, part_size), 'part_size': part_size,
                'method': 'server-side'}
    except NotImplementedError:
        pass
    except StorageError as e:
        if dst.stat(key) is not None:
            raise
        (log or logger).info(f"{key}: server-side copy to {where} failed ({e}); streaming instead")
    hasher = _Hasher()
    with src.open_read(key) as f, dst.open_write(key, size_hint=obj['size']) as writer:
        for chunk in iter(lambda: f.read(CHUNK), b''):
            hasher.update(chunk)
            writer.write(chunk)
        _check(obj, hasher, src.target.name)  # verified before the object becomes visible
        info = writer.commit()
    checksum = {'type': dst.capabilities.object_checksum, 'value': info.checksum} \
        if dst.capabilities.object_checksum and info.checksum else None
    return {'size': info.size, 'checksum': checksum, 'part_size': None, 'method': 'stream'}


def _put_exact(dst, key, data):
    """
    Create-only write of a small object; an existing one must be byte-identical (a rerun).
    """
    try:
        dst.put_bytes(key, data)
    except ObjectExists:
        if dst.get_bytes(key) != data:
            raise ReplicationError(f"{key} on {dst.target.name} exists with other content") from None


def _ensure_instance(src, dst, instance_id):
    key = layout.instance_key(instance_id)
    if dst.stat(key) is not None:
        found = sets.read_instance(dst, instance_id)
        if found and found.get('instance_id') != instance_id:
            raise ReplicationError(f"{key} on {dst.target.name} belongs to {found.get('instance_id')}")
        return
    try:
        dst.put_bytes(key, src.get_bytes(key))
    except ObjectExists:
        pass


def replicate_to(config, backup_set, manifest, src, target_name, keyring, log=None):
    """
    Copy one set to one replica target and run level 1 there. Returns the BackupCopy row.
    """
    from ..models import BackupCopy
    log = log or logger
    targets = catalog.sync_static(config)
    copy, _ = BackupCopy.objects.get_or_create(set=backup_set, target=targets[target_name],
                                               defaults={'is_primary': False})
    copy.status = CopyStatusChoices.UPLOADING
    copy.attempts += 1
    copy.last_attempt = timezone.now()
    copy.error = ''
    copy.save()
    target = config.target(target_name)
    try:
        dst = catalog.configured_backend(target)
        _ensure_instance(src, dst, backup_set.instance_id)
        records = {}
        for name, obj in sorted(manifest.objects.items()):
            records[name] = copy_object(src, dst, obj, log=log)
        _put_exact(dst, layout.object_key(backup_set.instance_id, backup_set.set_id, layout.MANIFEST), manifest.raw)
        _put_exact(dst, layout.object_key(backup_set.instance_id, backup_set.set_id, layout.COMPLETE),
                   manifest_mod.complete_marker(manifest.raw))
        copy.copied_objects = records
        copy.uploaded = timezone.now()
        copy.status = CopyStatusChoices.UPLOADED
        copy.checksum_mode = dst.capabilities.object_checksum or 'size'
        stats = [dst.stat(o['key']) for o in manifest.objects.values()]
        locks = [s.retain_until for s in stats if s and s.retain_until]
        copy.locked_until = min(locks) if locks else None
        copy.save()
        loaded = sets.load_manifest(dst, backup_set.instance_id, backup_set.set_id)
        results = sets.level1(dst, loaded, keyring, target=target, expected=records)
        if not catalog.mark_level(backup_set, results, VerifyLevelChoices.INTEGRITY, target=target_name):
            failed = '; '.join(f"{r['check']}: {r['detail']}" for r in results if not r['ok'])
            raise ReplicationError(f"level 1 on {target_name} failed: {failed}")
        copy.refresh_from_db()
        methods = sorted({r['method'] for r in records.values()})
        log.info(f"Set {backup_set.set_id}: replica on {target_name} verified ({', '.join(methods) or 'no objects'})")
    except Exception as e:
        copy.refresh_from_db()
        copy.status = CopyStatusChoices.FAILED
        copy.error = f'{type(e).__name__}: {e}'
        copy.save()
        log.error(f"Set {backup_set.set_id}: replica on {target_name} failed: {copy.error}")
    return copy


def update_set_status(backup_set, config=None):
    """
    partial while a required replica failed; back to verified (or uploaded) once every replica holds the set.
    """
    config = config or conf.get_config()
    policy = config.policies.get(backup_set.policy_name)
    replicas = set(policy.replicas) if policy else set()
    if backup_set.status not in SetStatusChoices.USABLE or not replicas:
        return backup_set
    failed = backup_set.copies.filter(target__name__in=replicas, status=CopyStatusChoices.FAILED).exists()
    if failed:
        status = SetStatusChoices.PARTIAL
    else:
        status = SetStatusChoices.VERIFIED if backup_set.verify_level >= VerifyLevelChoices.INTEGRITY \
            else SetStatusChoices.UPLOADED
    if status != backup_set.status:
        backup_set.status = status
        backup_set.save(update_fields=('status',))
    return backup_set


def replicate(set_id, targets=None, log=None):
    """
    Copy a set to its policy's replicas (or the given targets). Returns {target: copy status}.
    """
    from ..models import BackupSet
    log = log or logger
    config = conf.get_config()
    backup_set = BackupSet.objects.get(set_id=set_id)
    policy = config.policies.get(backup_set.policy_name)
    wanted = list(targets or (policy.replicas if policy else ()))
    if not wanted:
        return {}
    if backup_set.status not in SetStatusChoices.USABLE:
        raise ReplicationError(f"Set {set_id} is {backup_set.status}; only usable sets are replicated")
    with advisory_lock(f'replicate:{set_id}'):
        src, source_name = _source(config, backup_set, policy, exclude=wanted)
        with Keyring(config.crypto) as keyring:
            manifest = sets.load_manifest(src, backup_set.instance_id, set_id)
            manifest_mod.verify(manifest, keyring)
            results = {}
            for name in wanted:
                if name == source_name:
                    continue
                copy = replicate_to(config, backup_set, manifest, src, name, keyring, log=log)
                results[name] = copy.status
        backup_set.refresh_from_db()
        update_set_status(backup_set, config)
    return results


def _source(config, backup_set, policy, exclude=()):
    """
    The copy to replicate from: the primary when it holds a verified copy, else any other verified copy.
    """
    copies = backup_set.copies.select_related('target').filter(
        status=CopyStatusChoices.VERIFIED).exclude(target__name__in=exclude)
    names = [c.target.name for c in sorted(copies, key=lambda c: not c.is_primary) if c.target.name in config.targets]
    if not names and policy and policy.target not in exclude:
        names = [policy.target]
    for name in names:
        try:
            backend = catalog.configured_backend(config.target(name))
            if backend.stat(layout.object_key(backup_set.instance_id, backup_set.set_id, layout.COMPLETE)):
                return backend, name
        except StorageError:
            continue
    raise ReplicationError(f"Set {backup_set.set_id}: no verified copy to replicate from")


def due_retries(now=None):
    """
    Replica copies the scheduler should retry: failed ones below MAX_ATTEMPTS whose last attempt is an hour old,
    and pending ones whose job never ran. Returns {set_id: [target names]}.
    """
    from ..models import BackupCopy
    now = now or timezone.now()
    due = {}
    copies = BackupCopy.objects.filter(
        is_primary=False, set__status__in=SetStatusChoices.USABLE, set__finished__gte=now - timedelta(days=7),
        status__in=(CopyStatusChoices.FAILED, CopyStatusChoices.PENDING, CopyStatusChoices.UPLOADING),
        attempts__lt=MAX_ATTEMPTS,
    ).select_related('set', 'target')
    for copy in copies:
        last = copy.last_attempt or copy.set.finished
        # An uploading copy may still be in flight (its job holds the set's lock); give it a day
        wait = timedelta(days=1) if copy.status == CopyStatusChoices.UPLOADING else RETRY_AFTER
        if last and now - last >= wait:
            due.setdefault(copy.set.set_id, []).append(copy.target.name)
    return due
