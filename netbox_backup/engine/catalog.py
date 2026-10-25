"""
The database catalog is a cache. Static config is mirrored into StorageTarget/BackupPolicy rows, and BackupSet/
BackupCopy rows are rebuilt from the manifests in storage by sync(), which runs after every restore.
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .. import conf
from ..choices import CopyStatusChoices, SetStatusChoices, TriggerChoices, VerifyLevelChoices
from ..core import layout, sets
from ..core.gpg import GPGError, Keyring
from ..core.manifest import ManifestError
from ..core.manifest import verify as manifest_verify
from ..storage import StorageError, get_backend

logger = logging.getLogger('netbox_backup')

EXPIRED_ROW_DAYS = 90


def sync_static(config=None):
    """
    Mirror targets and policies from static config. Rows for entries removed from config are disabled, never
    deleted, so the sets that reference them stay intact.
    """
    from ..models import BackupPolicy, StorageTarget
    config = config or conf.get_config()
    targets = {}
    with transaction.atomic():
        for name, target in config.targets.items():
            try:
                location = get_backend(target).describe()
            except StorageError as e:
                location = f'unavailable: {e}'
            public = target.public_dict()
            obj, _ = StorageTarget.objects.update_or_create(name=name, defaults={
                'backend': target.backend, 'config': public['options'], 'secret_refs': public['credentials'],
                'prefix': target.prefix, 'location': location, 'retention_days': target.retention_days,
                'quota_bytes': target.quota_bytes, 'offsite': target.offsite, 'is_static': True, 'enabled': True,
            })
            targets[name] = obj
        StorageTarget.objects.exclude(name__in=list(config.targets)).update(enabled=False)
        for name, policy in config.policies.items():
            obj, _ = BackupPolicy.objects.update_or_create(name=name, defaults={
                'components': list(policy.components), 'schedule': policy.schedule or '',
                'time_zone': policy.time_zone or '', 'primary_target': targets[policy.target],
                'retention': policy.retention.as_dict(), 'recipients': list(config.crypto.all_recipients),
                'zstd_level': policy.zstd_level, 'verify_schedule': policy.verify_schedule or '',
                'max_runtime': policy.max_runtime, 'enabled': policy.enabled, 'is_static': True,
            })
            obj.replica_targets.set([targets[r] for r in policy.replicas])
        BackupPolicy.objects.exclude(name__in=list(config.policies)).update(enabled=False)
    return targets


def configured_backend(target, prune=False):
    """
    The driver for a TargetConfig, with the checksum capability the last probe observed: where the server's
    checksum differed from the locally computed one (SSE-KMS, SSE-C), level 1 on that target is size only.
    """
    from ..models import StorageTarget
    backend = get_backend(target, prune=prune)
    if backend.capabilities.object_checksum:
        probe = StorageTarget.objects.filter(name=target.name).values_list('last_probe', flat=True).first()
        if probe and probe.get('object_checksum') is None:
            backend.capabilities = backend.capabilities._replace(object_checksum=None)
    return backend


def set_fields_from_header(header):
    """
    Catalog fields recoverable from a signed header alone (no key needed).
    """
    from django.utils.dateparse import parse_datetime
    components = {}
    for name, obj in header.get('objects', {}).items():
        component = layout.COMPONENT_BY_OBJECT.get(name)
        if component:
            components[component] = {
                'object': name, 'size': obj['size'], **header.get('components', {}).get(component, {}),
            }
    return {
        'instance_id': header['instance_id'],
        'policy_name': header.get('policy', ''),
        'trigger': header.get('trigger') or TriggerChoices.SCHEDULED,
        'started': parse_datetime(header['created_at']) if header.get('created_at') else None,
        'finished': parse_datetime(header['finished_at']) if header.get('finished_at') else None,
        'netbox_version': header.get('netbox_version', ''),
        'size_bytes': sum(o['size'] for o in header.get('objects', {}).values()),
        'components': components,
        'warnings': header.get('warnings', []),
    }


def sync(config=None, log=None, keyring=None):
    if keyring is None and (config or conf.get_config()).crypto.configured:
        try:
            with Keyring((config or conf.get_config()).crypto) as own:
                return _sync(config, log, own)
        except GPGError as e:
            (log or logger).warning(f"Catalog sync: signatures cannot be checked ({e}); importing nothing")
            return {'created': 0, 'updated': 0, 'expired': 0, 'errors': 1}
    return _sync(config, log, keyring)


def _sync(config=None, log=None, keyring=None):
    """
    Rebuild BackupSet/BackupCopy rows for this instance from every target. Sets that disappeared from all targets
    are marked expired; expired rows older than 90 days are deleted. Returns counts.
    """
    from ..models import BackupCopy, BackupPolicy, BackupSet
    log = log or logger
    config = config or conf.get_config()
    targets = sync_static(config)
    instance_id = config.instance_id
    if instance_id is None:
        log.info("Catalog sync: no instance_id yet (unpinned, no backup taken)")
        return {'created': 0, 'updated': 0, 'expired': 0, 'errors': 0}
    stats = {'created': 0, 'updated': 0, 'expired': 0, 'errors': 0}
    seen = set()
    policies = {p.name: p for p in BackupPolicy.objects.all()}
    for name, target in config.targets.items():
        try:
            backend = get_backend(target)
            listings = sets.list_sets(backend, instance_id)
        except StorageError as e:
            log.warning(f"Catalog sync: target {name} unavailable: {e}")
            stats['errors'] += 1
            continue
        for listing in listings:
            if not listing.complete:
                continue
            try:
                manifest = sets.load_manifest(backend, instance_id, listing.set_id)
                if keyring is not None:
                    # Only a correctly signed manifest behind a matching _COMPLETE counts; it then overrides a
                    # catalog row that says the set failed
                    manifest_verify(manifest, keyring)
            except (sets.SetError, ManifestError, StorageError, GPGError) as e:
                log.warning(f"Catalog sync: set {listing.set_id} on {name}: {e}")
                stats['errors'] += 1
                continue
            seen.add(listing.set_id)
            fields = set_fields_from_header(manifest.header)
            fields['policy'] = policies.get(fields['policy_name'])
            fields['manifest_sha384'] = manifest.sha384
            backup_set = BackupSet.objects.filter(set_id=listing.set_id).first()
            if backup_set is None:
                backup_set = BackupSet.objects.create(set_id=listing.set_id, status=SetStatusChoices.UPLOADED,
                                                      **fields)
                stats['created'] += 1
            else:
                changed = False
                for key in ('manifest_sha384', 'size_bytes', 'finished'):
                    if getattr(backup_set, key) != fields[key]:
                        setattr(backup_set, key, fields[key])
                        changed = True
                if backup_set.status in (SetStatusChoices.EXPIRED, SetStatusChoices.RUNNING,
                                         SetStatusChoices.PENDING, SetStatusChoices.FAILED):
                    backup_set.status = SetStatusChoices.UPLOADED
                    backup_set.expired_at = None
                    changed = True
                if changed:
                    backup_set.save()
                    stats['updated'] += 1
            retain = [o.retain_until for o in listing.objects.values() if o.retain_until]
            BackupCopy.objects.update_or_create(set=backup_set, target=targets[name], defaults={
                'status': CopyStatusChoices.UPLOADED if not BackupCopy.objects.filter(
                    set=backup_set, target=targets[name], status=CopyStatusChoices.VERIFIED).exists()
                else CopyStatusChoices.VERIFIED,
                'is_primary': bool(fields['policy'] and fields['policy'].primary_target_id == targets[name].pk),
                'locked_until': min(retain) if retain else None,
            })
    # Sets no target holds any more
    now = timezone.now()
    gone = BackupSet.objects.filter(instance_id=instance_id, status__in=SetStatusChoices.USABLE).exclude(
        set_id__in=seen)
    if stats['errors'] == 0:
        for backup_set in gone:
            backup_set.status = SetStatusChoices.EXPIRED
            backup_set.expired_at = now
            backup_set.save(update_fields=('status', 'expired_at'))
            backup_set.copies.update(status=CopyStatusChoices.DELETED)
            stats['expired'] += 1
    BackupSet.objects.filter(status=SetStatusChoices.EXPIRED,
                             expired_at__lt=now - timedelta(days=EXPIRED_ROW_DAYS)).delete()
    if config.crypto.report_signers:
        from . import qa
        stats['qa_reports'] = qa.import_reports(config, log=log)
    log.info(f"Catalog sync: {stats['created']} imported, {stats['updated']} updated, {stats['expired']} expired")
    return stats


def mark_level(backup_set, results, level=VerifyLevelChoices.INTEGRITY, target=None):
    """
    Record a verification result on a set (and its copy on target).
    """
    ok = bool(results) and all(r['ok'] for r in results)
    now = timezone.now()
    if target is not None:
        backup_set.copies.filter(target__name=target).update(
            verify_level=level if ok else VerifyLevelChoices.NONE, verified=now if ok else None,
            verify_detail=results, status=CopyStatusChoices.VERIFIED if ok else CopyStatusChoices.FAILED,
        )
    if ok:
        backup_set.verify_level = max(backup_set.verify_level, level)
        backup_set.verified_at = now
        if backup_set.status == SetStatusChoices.UPLOADED:
            backup_set.status = SetStatusChoices.VERIFIED
    backup_set.save()
    return ok
