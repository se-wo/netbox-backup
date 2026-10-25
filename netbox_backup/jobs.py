"""
Background jobs. Long jobs run on the dedicated queue netbox_backup.netbox_backup, served by its own rqworker, so
they never block scripts or webhooks. Each BackupJob uses its BackupPolicy as the job's object and stores the set
status in the job data, so an event rule on "Job completed" for BackupPolicy can alert on it.
"""
from datetime import timedelta

from core.exceptions import JobFailed
from netbox.jobs import JobRunner, system_job

from . import conf
from .choices import TriggerChoices

QUEUE = 'netbox_backup.netbox_backup'

# RQ's default timeout (RQ_DEFAULT_TIMEOUT, 300 s) would kill long jobs; every job here sets its own
VERIFY_TIMEOUT = timedelta(hours=1)
MIN_PRUNE_TIMEOUT = timedelta(hours=1)


def enqueue_backup(policy, trigger=TriggerChoices.SCHEDULED, user=None, created_by='', components=None):
    """
    Enqueue a backup of a BackupPolicy row on the plugin queue, with the policy's max_runtime as the RQ timeout.
    """
    return BackupJob.enqueue(
        instance=policy, user=user, policy=policy.name, trigger=trigger, created_by=created_by,
        components=components, queue_name=QUEUE, job_timeout=int(policy.max_runtime.total_seconds()),
    )


def enqueue_replicate(set_id, targets=None, timeout=None):
    """
    Copy a set to its replicas. A copy may take as long as the backup did, so it gets the policy's max_runtime.
    """
    return ReplicateJob.enqueue(set_id=set_id, targets=targets, queue_name=QUEUE,
                                job_timeout=int((timeout or MIN_PRUNE_TIMEOUT).total_seconds()))


def enqueue_verify(set_id, user=None, policy=None):
    """
    Level 1 of one set, or (policy=...) of every usable set of a policy.
    """
    return VerifyJob.enqueue(user=user, set_id=set_id, policy=policy, queue_name=QUEUE,
                             job_timeout=int(VERIFY_TIMEOUT.total_seconds()))


def enqueue_prune(dry_run=False):
    runtimes = [p.max_runtime for p in conf.get_config().policies.values()]
    timeout = max([MIN_PRUNE_TIMEOUT, *runtimes])
    return PruneJob.enqueue(dry_run=dry_run, queue_name=QUEUE, job_timeout=int(timeout.total_seconds()))


class BackupJob(JobRunner):
    class Meta:
        name = 'Backup'

    def run(self, policy, trigger=TriggerChoices.SCHEDULED, created_by='', components=None, **kwargs):
        from .engine.backup import run_backup
        from .engine.locks import LockHeld
        try:
            backup_set = run_backup(policy, trigger=trigger, created_by=created_by, job=self.job, log=self.logger,
                                    components=components)
        except LockHeld as e:
            self.logger.error(str(e))
            raise JobFailed() from e
        config = conf.get_config()
        if config.policy(policy).replicas:
            enqueue_replicate(backup_set.set_id, timeout=config.policy(policy).max_runtime)
        enqueue_prune()


class ReplicateJob(JobRunner):
    class Meta:
        name = 'Backup replication'

    def run(self, set_id, targets=None, **kwargs):
        from .engine.replicate import replicate
        results = replicate(set_id, targets=targets, log=self.logger)
        self.job.data = {'set_id': set_id, 'copies': results}
        if any(status != 'verified' for status in results.values()):
            raise JobFailed()


class VerifyJob(JobRunner):
    class Meta:
        name = 'Backup verification'

    def run(self, set_id=None, policy=None, **kwargs):
        from .engine.verify import verify_policy, verify_set
        if policy:
            results = verify_policy(policy, log=self.logger)
            self.job.data = {'policy': policy, 'sets': results}
            ok = all(results.values())
        else:
            ok = verify_set(set_id, log=self.logger)
            self.job.data = {'set_id': set_id, 'passed': ok}
        if not ok:
            raise JobFailed()


class PruneJob(JobRunner):
    class Meta:
        name = 'Backup prune'

    def run(self, dry_run=False, **kwargs):
        from .engine.prune import prune
        reports = prune(dry_run=dry_run, log=self.logger)
        self.job.data = {'targets': [r.as_dict() for r in reports]}
        if any(r.errors for r in reports):
            for report in reports:
                for error in report.errors:
                    self.logger.error(f"{report.target}: {error}")
            raise JobFailed()


@system_job(interval=5)
class SchedulerJob(JobRunner):
    class Meta:
        name = 'Backup scheduler'

    def run(self, *args, **kwargs):
        from .engine.scheduler import tick
        tick(log=self.logger)
