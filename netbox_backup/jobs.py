from core.exceptions import JobFailed
from netbox.jobs import JobRunner, system_job

from . import conf
from .choices import BackupKindChoices, BackupTriggerChoices, VerificationLevelChoices
from .engine import catalog, retention
from .engine.backup import BackupInProgress, create_backup
from .engine.verify import verify_backup


class BackupJob(JobRunner):
    """
    Create a backup on demand (UI, REST API).
    """
    class Meta:
        name = 'Backup'

    def run(self, kind=BackupKindChoices.KIND_DATABASE, trigger=BackupTriggerChoices.TRIGGER_MANUAL,
            requested_by='', verify_level=None, **kwargs):
        try:
            backup = create_backup(kind=kind, trigger=trigger, requested_by=requested_by, job=self.job,
                                   log=self.logger)
        except BackupInProgress as e:
            self.logger.error(str(e))
            raise JobFailed()
        self.job.data = {'backup': backup.name, 'backup_id': backup.pk, 'status': backup.status, 'size': backup.size}
        if verify_level:
            passed, _ = verify_backup(backup, level=verify_level, log=self.logger)
            if not passed:
                raise JobFailed()
        retention.prune(log=self.logger)


class VerifyBackupJob(JobRunner):
    class Meta:
        name = 'Backup verification'

    def run(self, backup_id, level=VerificationLevelChoices.LEVEL_DEEP, **kwargs):
        from .models import Backup
        backup = Backup.objects.get(pk=backup_id)
        passed, results = verify_backup(backup, level=level, log=self.logger)
        self.job.data = {'backup': backup.name, 'backup_id': backup.pk, 'level': level, 'passed': passed,
                         'results': results}
        if not passed:
            raise JobFailed()


class _ScheduledBackupJob(JobRunner):
    kind = None

    def run(self, *args, **kwargs):
        configured = conf.interval(self.kind)
        if configured != self.job.interval:
            # The schedule was changed or disabled since this job was queued; stop this recurrence. The worker
            # schedules the new interval (if any) when it next starts.
            self.logger.warning(f"{self.kind} backup schedule changed to {configured}; not rescheduling")
            self.job.interval = None
            return
        try:
            create_backup(kind=self.kind, trigger=BackupTriggerChoices.TRIGGER_SCHEDULED, job=self.job,
                          log=self.logger)
        except BackupInProgress as e:
            self.logger.warning(f"Skipped: {e}")
            return
        retention.prune(log=self.logger)


class ScheduledDatabaseBackupJob(_ScheduledBackupJob):
    kind = BackupKindChoices.KIND_DATABASE

    class Meta:
        name = 'Scheduled database backup'


class ScheduledFullBackupJob(_ScheduledBackupJob):
    kind = BackupKindChoices.KIND_FULL

    class Meta:
        name = 'Scheduled full backup'


class BackupHousekeepingJob(JobRunner):
    """
    Reconcile the catalog with storage, apply retention, remove scratch databases from interrupted test restores.
    """
    class Meta:
        name = 'Backup housekeeping'

    def run(self, *args, **kwargs):
        from .engine.verify import cleanup_scratch_databases
        catalog.sync(log=self.logger)
        retention.prune(log=self.logger)
        try:
            cleanup_scratch_databases()
        except Exception as e:
            self.logger.warning(f"Could not clean up scratch databases: {e}")


def register_system_jobs():
    for kind, cls in ((BackupKindChoices.KIND_DATABASE, ScheduledDatabaseBackupJob),
                      (BackupKindChoices.KIND_FULL, ScheduledFullBackupJob)):
        if interval := conf.interval(kind):
            system_job(interval=interval)(cls)
    if interval := conf.get('housekeeping_interval'):
        system_job(interval=int(interval))(BackupHousekeepingJob)
