"""
Health: a small set of named conditions answering "can this install be recovered right now?". Each is ok, info,
warning or error; the overall status is green only if every enabled policy succeeded within its interval +
max_runtime + 1 h and at least one locked off-site copy exists. Exposed in the UI, the REST API, the dashboard
widget and "nbbackup status --json".
"""
from dataclasses import asdict, dataclass
from datetime import timedelta

from django.utils import timezone

from .. import conf
from ..choices import SetStatusChoices, VerifyLevelChoices
from . import pgbackrest, scheduler

OK, INFO, WARNING, ERROR = 'ok', 'info', 'warning', 'error'
SEVERITY = {OK: 0, INFO: 1, WARNING: 2, ERROR: 3}
COLORS = {OK: 'green', INFO: 'blue', WARNING: 'yellow', ERROR: 'red'}
QA_MAX_AGE = timedelta(days=35)
CAPACITY_ALERT = 0.7


@dataclass
class Condition:
    name: str
    status: str
    message: str
    subject: str = ''

    @property
    def color(self):
        return COLORS[self.status]

    def as_dict(self):
        return asdict(self)


def policy_deadline(policy):
    """
    How old the newest verified set of a policy may be before the policy counts as failing.
    """
    interval = scheduler.worst_case_loss(policy) or timedelta(days=1)
    return interval + policy.max_runtime + timedelta(hours=1)


def conditions(now=None, check_workers=True):
    from ..models import BackupCopy, BackupSet, StorageTarget
    now = now or timezone.now()
    result = []
    problems = conf.startup_problems()
    for problem in problems:
        result.append(Condition('Configuration', ERROR, problem))
    if problems:
        return result
    config = conf.get_config()
    if not config.instance_id_pinned:
        result.append(Condition('InstancePinned', WARNING, f"instance_id is derived from SECRET_KEY; pin "
                                                           f"'instance_id': '{config.instance_id}' for disaster "
                                                           f"recovery"))
    if not config.crypto.configured:
        result.append(Condition('Encryption', ERROR, "crypto is not configured; no backup can run"))
    if not config.targets:
        result.append(Condition('Targets', ERROR, "No storage target is configured"))
        return result

    for target in StorageTarget.objects.filter(enabled=True):
        if not target.last_probe_at:
            result.append(Condition('TargetProbe', WARNING, "never probed (nbbackup target probe)", target.name))
        elif not target.probe_ok:
            failed = [c['check'] for c in target.last_probe.get('checks', []) if not c['ok'] and c.get('required')]
            result.append(Condition('TargetProbe', ERROR, f"last probe failed: {', '.join(failed)}", target.name))
        if target.quota_bytes:
            used = sum(BackupSet.objects.filter(copies__target=target, status__in=SetStatusChoices.USABLE)
                       .values_list('size_bytes', flat=True)) or 0
            if used > CAPACITY_ALERT * target.quota_bytes:
                result.append(Condition('Capacity', WARNING, f"{used / target.quota_bytes:.0%} of the quota used",
                                        target.name))

    if check_workers:
        tick = scheduler.last_tick()
        if tick is None or now - tick > timedelta(minutes=15):
            result.append(Condition('Scheduler', ERROR, 'Scheduling not active: no scheduler run for 15 minutes '
                                                        '(is an rqworker running?)'))
        try:
            workers = scheduler.queue_workers()
        except Exception as e:
            workers = None
            result.append(Condition('Workers', WARNING, f"cannot inspect RQ workers: {e}"))
        if workers == 0:
            result.append(Condition('Workers', ERROR, f"Scheduling not active: no worker serves {scheduler.QUEUE} "
                                                      f"(run: manage.py rqworker {scheduler.QUEUE})"))

    for name, policy in config.policies.items():
        if not policy.enabled:
            continue
        newest = BackupSet.objects.filter(
            policy_name=name, instance_id=config.instance_id, verify_level__gte=VerifyLevelChoices.INTEGRITY,
            status__in=SetStatusChoices.USABLE,
        ).order_by('-finished').first()
        deadline = policy_deadline(policy)
        if newest is None:
            result.append(Condition('LastSuccess', ERROR, 'no verified set yet', name))
        elif now - newest.finished > deadline:
            result.append(Condition('LastSuccess', ERROR, f"newest verified set {newest.set_id} is older than "
                                                          f"{deadline}", name))
        else:
            result.append(Condition('LastSuccess', OK, f"newest verified set {newest.set_id} at "
                                                       f"{newest.finished:%Y-%m-%d %H:%M}", name))
            if newest.media_missing or newest.components.get('media', {}).get('missing'):
                result.append(Condition('MediaMissing', WARNING, f"{newest.components['media']['missing']} "
                                                                 f"referenced file(s) missing from media", name))
        last_failed = BackupSet.objects.filter(policy_name=name, status=SetStatusChoices.FAILED,
                                               started__gte=now - deadline).order_by('-started').first()
        if last_failed and (newest is None or last_failed.started > newest.started):
            result.append(Condition('LastRun', WARNING, f"last run {last_failed.set_id} failed: "
                                                        f"{last_failed.error[:200]}", name))

    locked_offsite = BackupCopy.objects.filter(
        target__offsite=True, set__instance_id=config.instance_id, locked_until__gt=now,
        verify_level__gte=VerifyLevelChoices.INTEGRITY,
    ).exists()
    if locked_offsite:
        result.append(Condition('LockedOffsiteCopy', OK, 'at least one verified, locked off-site copy exists'))
    else:
        result.append(Condition('LockedOffsiteCopy', ERROR, 'no verified copy on an off-site target is under a '
                                                            'retention lock'))

    qa_sets = BackupSet.objects.filter(instance_id=config.instance_id, qa_passed_at__isnull=False)
    if qa_sets.exists():
        newest_qa = qa_sets.order_by('-qa_passed_at').first()
        if now - newest_qa.qa_passed_at > QA_MAX_AGE:
            result.append(Condition('QARestore', WARNING, f"no passed QA restore in {QA_MAX_AGE.days} days"))
        else:
            result.append(Condition('QARestore', OK, f"QA restore of {newest_qa.set_id} passed "
                                                     f"{newest_qa.qa_passed_at:%Y-%m-%d}"))

    from .journal import list_journals
    try:
        for j in list_journals()[:5]:
            if j['status'] == 'loaded':
                result.append(Condition('Restore', WARNING, f"restore {j.restore_id} is loaded and waits for the "
                                                            f"switch and 'restore finish'"))
            elif j['status'] in ('failed', 'finishing'):
                result.append(Condition('Restore', ERROR, f"restore {j.restore_id} stopped at {j['status']}; see "
                                                          f"nbbackup restore status"))
    except Exception:
        pass

    pitr = pgbackrest.condition()
    if pitr is not None:
        result.append(pitr)
    return result


def overall(conds):
    return max((c.status for c in conds), key=SEVERITY.get, default=OK)


def summary(now=None, check_workers=True):
    conds = conditions(now=now, check_workers=check_workers)
    return {'status': overall(conds), 'conditions': [c.as_dict() for c in conds]}
