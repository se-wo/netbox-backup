"""
Scheduling. NetBox's enqueue_once(interval=...) only supports fixed intervals, so one scheduler system job runs
every 5 minutes and evaluates each policy's cron expression with croniter; schedules resolve to 5-minute slots.
A missed run (worker down) runs once on recovery, not once per missed slot.
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .. import conf
from ..choices import SetStatusChoices, TriggerChoices
from . import catalog

logger = logging.getLogger('netbox_backup')

TICK_CACHE_KEY = 'netbox_backup:scheduler_tick'
QUEUE = 'netbox_backup.netbox_backup'


def policy_tz(policy):
    return ZoneInfo(policy.time_zone or settings.TIME_ZONE or 'UTC')


def previous_slot(policy, now):
    return croniter(policy.schedule, now.astimezone(policy_tz(policy))).get_prev(datetime)


def next_runs(policy, count=5, now=None):
    if not policy.schedule:
        return []
    it = croniter(policy.schedule, (now or timezone.now()).astimezone(policy_tz(policy)))
    return [it.get_next(datetime) for _ in range(count)]


def worst_case_loss(policy, now=None):
    """
    The longest gap between two runs over the next few weeks: how much change a failure right before a run loses.
    """
    if not policy.schedule:
        return None
    it = croniter(policy.schedule, (now or timezone.now()).astimezone(policy_tz(policy)))
    runs = [it.get_next(datetime) for _ in range(60)]
    return max(b - a for a, b in zip(runs, runs[1:]))


def last_tick():
    value = cache.get(TICK_CACHE_KEY)
    return datetime.fromtimestamp(value, tz=timezone.get_current_timezone()) if value else None


def queue_workers():
    """
    Number of RQ workers serving the plugin queue.
    """
    import django_rq
    from rq import Worker
    connection = django_rq.get_connection(QUEUE)
    return sum(1 for w in Worker.all(connection=connection) if QUEUE in w.queue_names())


def fail_overruns(now=None, log=None):
    """
    Sets still running past max_runtime + 10 minutes. A set is declared failed only once its policy's lock is free,
    i.e. the job is really gone; a hung job still holding it has its lock sessions terminated, so the policy can run
    again (the job itself refuses to write the manifest after max_runtime). Returns the number marked failed.
    """
    from ..models import BackupSet
    from .locks import reclaim
    log = log or logger
    now = now or timezone.now()
    failed = 0
    for backup_set in BackupSet.objects.filter(status__in=SetStatusChoices.ACTIVE).select_related('policy'):
        limit = backup_set.policy.max_runtime if backup_set.policy else timedelta(hours=4)
        if not backup_set.started or now - backup_set.started <= limit + timedelta(minutes=10):
            continue
        if not reclaim(f'policy:{backup_set.policy_name}'):
            log.warning(f"Set {backup_set.set_id} overran max_runtime; its lock could not be freed yet")
            continue
        backup_set.status = SetStatusChoices.FAILED
        backup_set.error = f'Exceeded max_runtime ({limit}); the job was killed, died or was terminated'
        backup_set.finished = now
        backup_set.save()
        backup_set.copies.filter(status__in=('pending', 'uploading')).update(status='failed')
        failed += 1
    return failed


def record_skipped(row, reason, now):
    """
    A scheduled run that could not start is recorded as a failed set, so health and alerts see it.
    """
    from ..core import layout
    from ..models import BackupSet
    return BackupSet.objects.create(
        set_id=layout.new_ulid(), instance_id=conf.instance_id(), policy=row, policy_name=row.name,
        status=SetStatusChoices.FAILED, trigger=TriggerChoices.SCHEDULED, started=now, finished=now,
        error=f'Skipped: {reason}',
    )


def tick(now=None, log=None):
    """
    One scheduler run: mirror static config, fail overruns, enqueue due backups and the daily prune.
    """
    from core.choices import JobStatusChoices
    from core.models import Job

    from ..jobs import BackupJob, PruneJob, enqueue_backup, enqueue_prune
    from ..models import BackupPolicy
    log = log or logger
    now = now or timezone.now()
    cache.set(TICK_CACHE_KEY, now.timestamp(), None)
    config = conf.get_config()
    catalog.sync_static(config)
    fail_overruns(now, log=log)
    enqueued = []
    for name, policy in config.policies.items():
        if not policy.enabled or not policy.schedule:
            continue
        row = BackupPolicy.objects.get(name=name)
        slot = previous_slot(policy, now)
        if row.last_scheduled is None:
            # First tick after install: start from the current slot rather than backing up immediately
            row.last_scheduled = slot
            row.save(update_fields=('last_scheduled',))
            continue
        if slot <= row.last_scheduled:
            continue
        active = BackupJob.get_jobs(row).filter(status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES).exists()
        if active:
            log.warning(f"Policy {name}: previous backup still queued or running; skipping slot {slot:%Y-%m-%d %H:%M}")
            record_skipped(row, 'the previous run of this policy was still queued or running', now)
        else:
            enqueue_backup(row, trigger=TriggerChoices.SCHEDULED)
            enqueued.append(name)
            log.info(f"Policy {name}: enqueued backup for slot {slot:%Y-%m-%d %H:%M %Z}")
        row.last_scheduled = slot
        row.save(update_fields=('last_scheduled',))
    recent_prune = Job.objects.filter(name=PruneJob.name, created__gte=now - timedelta(hours=24)).exists()
    if config.targets and not recent_prune:
        enqueue_prune()
    return enqueued
