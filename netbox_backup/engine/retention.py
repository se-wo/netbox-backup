"""
Retention: delete backups older than the configured retention period, always keeping the newest `keep_last`
successful backups of each kind so that a stopped schedule never prunes an instance down to nothing.
"""
import logging
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .. import conf
from ..choices import BackupKindChoices, BackupStatusChoices
from ..storage import get_storage
from . import catalog
from .backup import storage_key

logger = logging.getLogger('netbox_backup')

# Incomplete uploads younger than this may still be in progress
INCOMPLETE_GRACE = timedelta(hours=24)


def plan(now=None):
    """
    Return the Backup rows that retention would delete.
    """
    from ..models import Backup
    retention = conf.retention()
    if not retention:
        return []
    now = now or timezone.now()
    cutoff = now - retention
    keep_last = max(int(conf.get('keep_last') or 0), 0)
    own = Backup.objects.filter(instance_id=conf.instance_id())

    doomed = []
    for kind in BackupKindChoices.values():
        successful = own.filter(kind=kind, status__in=BackupStatusChoices.SUCCESSFUL).order_by('-started_at', '-pk')
        for index, backup in enumerate(successful):
            if index >= keep_last and backup.started_at and backup.started_at < cutoff:
                doomed.append(backup)
    unsuccessful = own.exclude(status__in=BackupStatusChoices.SUCCESSFUL).exclude(
        status__in=(BackupStatusChoices.STATUS_RUNNING, BackupStatusChoices.STATUS_PENDING)
    )
    doomed += [b for b in unsuccessful if (b.started_at or b.created) < cutoff]
    return doomed


def delete_backup(backup, log=None):
    log = log or logger
    backup.delete()
    log.info(f"Deleted backup {backup.name}")


def prune(dry_run=False, log=None, now=None):
    log = log or logger
    storage = get_storage()
    doomed = plan(now=now)
    for backup in doomed:
        if dry_run:
            log.info(f"Would delete {backup.name} ({backup.get_status_display()}, started {backup.started_at})")
        else:
            delete_backup(backup, log=log)

    # Directories without a manifest left by a crashed process
    now = now or timezone.now()
    removed_incomplete = []
    for name in catalog.incomplete_backups(storage=storage):
        when = parse_datetime_from_name(name)
        if when and now - when > INCOMPLETE_GRACE:
            removed_incomplete.append(name)
            if dry_run:
                log.info(f"Would delete incomplete upload {name}")
            else:
                storage.delete_prefix(storage_key(conf.instance_id(), name))
                log.info(f"Deleted incomplete upload {name}")
    if not doomed and not removed_incomplete:
        log.info(f"Retention: nothing to prune (retention {conf.get('retention')}, keep_last {conf.get('keep_last')})")
    return [b.name for b in doomed] + removed_incomplete


def parse_datetime_from_name(name):
    stamp = name.split('-', 1)[0]
    try:
        return parse_datetime(f'{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z')
    except (ValueError, IndexError):
        return None
