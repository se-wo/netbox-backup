"""
The backup catalog: manifests in storage are authoritative; Backup rows are a synchronised cache of them.
"""
import contextlib
import json
import logging
import os

from django.db import connection
from django.utils.dateparse import parse_datetime

from .. import conf
from ..choices import BackupStatusChoices
from ..storage import StorageError, get_storage
from . import crypto
from .backup import (
    ADVISORY_LOCK_KEY,
    MANIFEST_FORMAT,
    MANIFEST_NAME,
    BackupError,
    location_of,
    sha256_file,
    storage_key,
)

logger = logging.getLogger('netbox_backup')


def read_manifest(storage, instance_id, name):
    try:
        data = storage.get_bytes(storage_key(instance_id, name, MANIFEST_NAME))
    except StorageError:
        return None
    try:
        manifest = json.loads(data)
    except ValueError:
        return None
    if not str(manifest.get('format', '')).startswith('netbox-backup/'):
        return None
    return manifest


def list_instances(storage=None):
    storage = storage or get_storage()
    return storage.list_children('')


def list_manifests(instance_id=None, storage=None):
    """
    Every complete backup in storage for an instance (default: this one), oldest first.
    """
    storage = storage or get_storage()
    instance_id = instance_id or conf.instance_id()
    manifests = []
    for name in storage.list_children(instance_id):
        if manifest := read_manifest(storage, instance_id, name):
            manifests.append(manifest)
    return sorted(manifests, key=lambda m: m.get('started_at', ''))


def incomplete_backups(instance_id=None, storage=None):
    """
    Directories in storage without a manifest (interrupted uploads).
    """
    storage = storage or get_storage()
    instance_id = instance_id or conf.instance_id()
    return [
        name for name in storage.list_children(instance_id)
        if not storage.exists(storage_key(instance_id, name, MANIFEST_NAME))
    ]


def resolve(identifier, instance_id=None, storage=None):
    """
    Find a manifest by backup name, by unambiguous name prefix, or the keyword "latest" / "latest-full".
    """
    manifests = [
        m for m in list_manifests(instance_id, storage) if m.get('status') != BackupStatusChoices.STATUS_FAILED
    ]
    if not manifests:
        raise BackupError(f"No backups found for instance {instance_id or conf.instance_id()}")
    if identifier in ('latest', 'latest-full', 'latest-database'):
        kind = identifier.partition('-')[2]
        candidates = [m for m in manifests if not kind or m['kind'] == kind]
        if not candidates:
            raise BackupError(f"No {kind} backups found")
        return candidates[-1]
    exact = [m for m in manifests if m['name'] == identifier]
    if exact:
        return exact[0]
    matches = [m for m in manifests if m['name'].startswith(identifier)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise BackupError(f"Backup {identifier!r} not found")
    raise BackupError(f"{identifier!r} is ambiguous: {', '.join(m['name'] for m in matches)}")


def _fields_from_manifest(manifest, storage):
    return {
        'instance_id': manifest['instance_id'],
        'kind': manifest['kind'],
        'status': manifest.get('status', BackupStatusChoices.STATUS_COMPLETED),
        'trigger': manifest.get('trigger', ''),
        'requested_by': manifest.get('requested_by', ''),
        'started_at': parse_datetime(manifest['started_at']) if manifest.get('started_at') else None,
        'completed_at': parse_datetime(manifest['completed_at']) if manifest.get('completed_at') else None,
        'size': manifest.get('size'),
        'encrypted': bool(manifest.get('encryption', {}).get('enabled')),
        'netbox_version': manifest.get('netbox', {}).get('version', ''),
        'location': location_of(storage, manifest['instance_id'], manifest['name']),
        'manifest': manifest,
        'error': '',
    }


def sync(log=None, storage=None):
    """
    Reconcile Backup rows with storage. Returns a dict of counts.
    """
    from ..models import Backup
    log = log or logger
    storage = storage or get_storage()
    instance_id = conf.instance_id()
    stats = {'created': 0, 'updated': 0, 'missing': 0, 'interrupted': 0}

    manifests = {m['name']: m for m in list_manifests(instance_id, storage)}
    for name, manifest in manifests.items():
        fields = _fields_from_manifest(manifest, storage)
        backup = Backup.objects.filter(name=name).first()
        if backup is None:
            Backup.objects.create(name=name, **fields)
            stats['created'] += 1
        elif any(getattr(backup, k) != v for k, v in fields.items() if k != 'manifest') or backup.manifest != manifest:
            for k, v in fields.items():
                setattr(backup, k, v)
            backup.save()
            stats['updated'] += 1

    # Rows whose backup has vanished from storage
    for backup in Backup.objects.filter(instance_id=instance_id, status__in=BackupStatusChoices.SUCCESSFUL):
        if backup.name not in manifests:
            backup.status = BackupStatusChoices.STATUS_MISSING
            backup.save()
            stats['missing'] += 1

    # Rows left "running" by a process that died, or captured mid-backup in a dump that was later restored. Only
    # safe to judge when no backup currently holds the lock.
    with _lock_if_free() as free:
        if free:
            stale = Backup.objects.filter(
                status__in=(BackupStatusChoices.STATUS_RUNNING, BackupStatusChoices.STATUS_PENDING)
            ).exclude(name__in=manifests.keys())
            for backup in stale:
                if backup.job and backup.job.status in ('pending', 'scheduled', 'running'):
                    continue
                backup.status = BackupStatusChoices.STATUS_FAILED
                backup.error = backup.error or (
                    'Interrupted: the process creating this backup stopped before it finished'
                )
                backup.save()
                stats['interrupted'] += 1

    log.info(
        f"Catalog sync: {stats['created']} imported, {stats['updated']} updated, "
        f"{stats['missing']} missing from storage, {stats['interrupted']} interrupted"
    )
    return stats


@contextlib.contextmanager
def _lock_if_free():
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s)', [ADVISORY_LOCK_KEY])
        acquired = cursor.fetchone()[0]
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [ADVISORY_LOCK_KEY])


class ArtifactError(BackupError):
    pass


def fetch_artifacts(manifest, workdir, types=None, storage=None, key=None, log=None):
    """
    Download, integrity-check and decrypt a backup's artifacts into workdir. Returns {type: plaintext path}.
    """
    log = log or logger
    storage = storage or get_storage()
    encryption = manifest.get('encryption') or {}
    if encryption.get('enabled'):
        key = key or conf.encryption_key()
        if not key:
            raise ArtifactError("This backup is encrypted but no encryption_key is configured")
        expected = encryption.get('key_fingerprint')
        if expected and crypto.key_fingerprint(key) != expected:
            raise ArtifactError(
                f"The configured encryption_key does not match the key this backup was made with "
                f"(fingerprint {crypto.key_fingerprint(key)}, expected {expected})"
            )
    result = {}
    for artifact in manifest.get('artifacts', []):
        if types is not None and artifact['type'] not in types:
            continue
        stored = os.path.join(workdir, artifact['name'])
        log.info(f"Downloading {artifact['name']} ({artifact['size']} bytes)")
        storage.get_file(storage_key(manifest['instance_id'], manifest['name'], artifact['name']), stored)
        if os.path.getsize(stored) != artifact['size']:
            raise ArtifactError(f"{artifact['name']}: size {os.path.getsize(stored)} != {artifact['size']}")
        if sha256_file(stored) != artifact['sha256']:
            raise ArtifactError(f"{artifact['name']}: checksum mismatch (corrupted in storage?)")
        plaintext = stored
        if artifact.get('encrypted'):
            plaintext = stored.removesuffix('.enc')
            crypto.decrypt_file(stored, plaintext, key)
            os.unlink(stored)
            if sha256_file(plaintext) != artifact['plaintext_sha256']:
                raise ArtifactError(f"{artifact['name']}: decrypted content checksum mismatch")
        result[artifact['type']] = plaintext
    if types is not None and (absent := set(types) - set(result)):
        raise ArtifactError(f"Backup has no {', '.join(sorted(absent))} artifact")
    return result


def check_format(manifest):
    if manifest.get('format') != MANIFEST_FORMAT:
        raise BackupError(f"Unsupported backup format {manifest.get('format')!r} (expected {MANIFEST_FORMAT})")
