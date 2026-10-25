"""
Health conditions: a small set of named checks summarising whether this installation is recoverable.
Each condition is reported as ok / info / warning / error. Exposed in the UI, REST API and CLI.
"""
from dataclasses import asdict, dataclass

from django.utils import timezone
from django.utils.timesince import timesince

from .. import conf
from ..choices import (
    BackupKindChoices,
    BackupStatusChoices,
    RestoreStatusChoices,
    VerificationStatusChoices,
)
from ..storage import get_storage

OK, INFO, WARNING, ERROR = 'ok', 'info', 'warning', 'error'
SEVERITY = {OK: 0, INFO: 1, WARNING: 2, ERROR: 3}
COLORS = {OK: 'green', INFO: 'blue', WARNING: 'yellow', ERROR: 'red'}


@dataclass
class Condition:
    name: str
    status: str
    message: str

    @property
    def color(self):
        return COLORS[self.status]

    def as_dict(self):
        return asdict(self)


def conditions(check_storage=True):
    from ..models import Backup, RestoreEvent
    from . import catalog
    result = []
    instance_id = conf.instance_id()
    own = Backup.objects.filter(instance_id=instance_id)

    # Storage
    storage = None
    if check_storage:
        try:
            storage = get_storage()
            storage.check()
            result.append(Condition('StorageReachable', OK, f"{_describe(storage)} is reachable and writable"))
        except Exception as e:
            result.append(Condition('StorageReachable', ERROR, f"Backup storage is not usable: {e}"))
            storage = None

    # Schedules
    schedules = {kind: conf.interval(kind) for kind in BackupKindChoices.values()}
    if not any(schedules.values()):
        result.append(Condition('ScheduleConfigured', WARNING, "No backup schedule is enabled; backups only run "
                                                               "when triggered manually"))
    else:
        enabled = ', '.join(f"{k} every {_minutes(v)}" for k, v in schedules.items() if v)
        inactive = _inactive_schedules(schedules)
        if inactive:
            result.append(Condition(
                'ScheduleConfigured', WARNING,
                f"Scheduled ({enabled}), but no job is queued for: {', '.join(inactive)}. Restart netbox-rq.",
            ))
        else:
            result.append(Condition('ScheduleConfigured', OK, f"Scheduled: {enabled}"))

    # Recency
    latest = own.filter(status__in=BackupStatusChoices.SUCCESSFUL).order_by('-started_at').first()
    if latest is None:
        result.append(Condition('BackupAvailable', ERROR, "No successful backup exists for this instance"))
    else:
        age = timezone.now() - (latest.completed_at or latest.started_at)
        stale_after = conf.stale_after()
        if stale_after and age > stale_after:
            result.append(Condition('BackupAvailable', WARNING,
                                    f"Newest backup {latest.name} is {timesince(latest.started_at)} old "
                                    f"(expected one at least every {_minutes(stale_after.total_seconds() // 60)})"))
        else:
            result.append(Condition('BackupAvailable', OK,
                                    f"Newest backup {latest.name} taken {timesince(latest.started_at)} ago"))

    # Full backup coverage (files + configuration)
    latest_full = own.filter(kind=BackupKindChoices.KIND_FULL, status__in=BackupStatusChoices.SUCCESSFUL).first()
    if latest_full is None:
        result.append(Condition('FullBackupAvailable', WARNING,
                                "No full backup exists: media, scripts and reports are not protected"))
    else:
        result.append(Condition('FullBackupAvailable', OK, f"Newest full backup: {latest_full.name}"))

    # Last run
    last = own.exclude(status__in=(BackupStatusChoices.STATUS_PENDING,)).order_by('-created').first()
    if last and last.status == BackupStatusChoices.STATUS_FAILED:
        result.append(Condition('LastBackupSucceeded', ERROR, f"Most recent backup {last.name} failed: {last.error}"))
    elif last and last.status == BackupStatusChoices.STATUS_WARNING:
        result.append(Condition('LastBackupSucceeded', WARNING,
                                f"Most recent backup {last.name} completed with warnings: {'; '.join(last.warnings)}"))
    elif last:
        result.append(Condition('LastBackupSucceeded', OK, f"Most recent backup {last.name} succeeded"))

    # Verification
    if latest is not None:
        if latest.verification_status == VerificationStatusChoices.STATUS_FAILED:
            result.append(Condition('BackupVerified', ERROR, f"Verification of {latest.name} failed"))
        elif latest.verification_status == VerificationStatusChoices.STATUS_UNVERIFIED:
            result.append(Condition('BackupVerified', INFO,
                                    f"{latest.name} has not been verified beyond the pre-upload check"))
        else:
            result.append(Condition('BackupVerified', OK,
                                    f"{latest.name} passed {latest.verification_level} verification"))
        restore_tested = own.filter(verification_status=VerificationStatusChoices.STATUS_PASSED,
                                    verification_level='restore').order_by('-verified_at').first()
        if restore_tested is None:
            result.append(Condition('RestoreTested', WARNING,
                                    "No backup has been test-restored. Run a 'restore' level verification."))
        else:
            result.append(Condition('RestoreTested', OK,
                                    f"{restore_tested.name} test-restored {timesince(restore_tested.verified_at)} ago"))

    # Encryption & configuration
    if conf.encryption_key():
        result.append(Condition('Encryption', OK, "Backups are encrypted (AES-256-GCM)"))
    else:
        result.append(Condition('Encryption', INFO, "Backups are not encrypted; protect the storage location"))
    if not conf.include_configuration():
        result.append(Condition(
            'ConfigurationProtected', WARNING,
            "configuration.py is not included in backups. Without the original SECRET_KEY and API_TOKEN_PEPPERS, "
            "restored API tokens stop working. Store them separately or enable encryption.",
        ))
    else:
        result.append(Condition('ConfigurationProtected', OK, "Configuration files are included in full backups"))

    # Shared storage
    if storage is not None:
        try:
            others = [i for i in catalog.list_instances(storage) if i != instance_id]
            if others:
                result.append(Condition(
                    'StorageIsolated', WARNING,
                    f"Backups from other instances share this storage location: {', '.join(others[:5])}. "
                    f"Restores refuse them by default; consider a dedicated bucket or prefix per instance.",
                ))
            else:
                result.append(Condition('StorageIsolated', OK, "No other instance writes to this storage location"))
        except Exception as e:
            result.append(Condition('StorageIsolated', WARNING, f"Could not list storage: {e}"))

    # Point-in-time recovery (optional, externally managed pgBackRest)
    from .pgbackrest import condition as pitr_condition
    if (pitr := pitr_condition()) is not None:
        result.append(pitr)

    # Last restore
    last_restore = RestoreEvent.objects.order_by('-started_at').first()
    if last_restore and last_restore.status == RestoreStatusChoices.STATUS_RECOVERY_REQUIRED:
        result.append(Condition(
            'RecoveryRequired', ERROR,
            f"Restore of {last_restore.backup_name} finished with row-count mismatches; the data is not what the "
            f"backup contained. Investigate before using this instance.",
        ))
    elif last_restore and last_restore.status == RestoreStatusChoices.STATUS_FAILED:
        result.append(Condition('RecoveryRequired', ERROR, f"Restore of {last_restore.backup_name} failed"))

    return result


def _inactive_schedules(schedules):
    """
    Enabled schedules with no queued job: netbox-rq has not been (re)started since the schedule was configured,
    or a restore replaced the job table.
    """
    from core.choices import JobStatusChoices
    from core.models import Job

    from ..jobs import ScheduledDatabaseBackupJob, ScheduledFullBackupJob
    runners = {BackupKindChoices.KIND_DATABASE: ScheduledDatabaseBackupJob,
               BackupKindChoices.KIND_FULL: ScheduledFullBackupJob}
    return [
        kind for kind, interval in schedules.items()
        if interval and not Job.objects.filter(
            name=runners[kind].name, interval=interval, status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES,
        ).exists()
    ]


def overall(conds):
    return max((c.status for c in conds), key=SEVERITY.__getitem__, default=OK)


def _minutes(value):
    value = int(value)
    if value % 1440 == 0:
        return f"{value // 1440}d"
    if value % 60 == 0:
        return f"{value // 60}h"
    return f"{value}m"


def _describe(storage):
    info = storage.describe()
    if info['backend'] == 's3':
        return f"s3://{info['bucket']}/{info['prefix'].strip('/')}"
    if info['backend'] == 'local':
        return info['path']
    return info['backend']
