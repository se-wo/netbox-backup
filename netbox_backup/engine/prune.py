"""
Pruning. Runs on the maintenance runner with the target's prune credential; without that credential it only
reports what it would delete. Deletion removes _COMPLETE first, so a half-deleted set is invisible rather than
corrupt, and deletes by version where the target is versioned. Objects under an active lock are skipped and
retried at the next prune, once the lock has expired.
"""
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .. import conf
from ..choices import CopyStatusChoices, SetStatusChoices, VerifyLevelChoices
from ..core import layout, sets
from ..core.config import RetentionConfig
from ..storage import StorageError, get_backend
from ..storage.base import PROBE_DIR
from . import retention

logger = logging.getLogger('netbox_backup')

# An incomplete set (no _COMPLETE) older than this is garbage once its lock has expired
INCOMPLETE_GRACE = timedelta(hours=24)
# On targets without locks an incomplete set may still be being written: wait for the longest job to be over
RUNTIME_MARGIN = timedelta(hours=1)


def incomplete_grace(config):
    return max([INCOMPLETE_GRACE, *[p.max_runtime + RUNTIME_MARGIN for p in config.policies.values()]])


@dataclass
class TargetReport:
    target: str
    can_delete: bool
    keep: dict = field(default_factory=dict)
    delete: list = field(default_factory=list)
    deleted: list = field(default_factory=list)
    locked: dict = field(default_factory=dict)
    waiting_for_replicas: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    aborted_uploads: int = 0
    held: dict = field(default_factory=dict)
    probes_due: list = field(default_factory=list)
    probes_deleted: int = 0

    def as_dict(self):
        return dict(self.__dict__)


def can_prune(target):
    if target.backend == 'local':
        return bool(target.options.get('allow_prune'))
    if target.backend in ('sftp', 'rclone'):
        # No server-side separation of rights to rely on: a separate login, or an explicit opt-in
        return bool(target.prune_credentials or target.options.get('allow_prune'))
    return bool(target.prune_credentials)


def _retention_for(config, target, policy_name):
    policy = config.policies.get(policy_name)
    if target.retention is not None:
        return target.retention, policy.time_zone if policy else None
    if policy is not None:
        return policy.retention, policy.time_zone
    return RetentionConfig(), None


def prune_target(config, target, dry_run=False, log=None, now=None):
    from ..models import BackupCopy, BackupSet
    log = log or logger
    now = now or timezone.now()
    report = TargetReport(target=target.name, can_delete=can_prune(target) and not dry_run)
    if config.instance_id is None:
        return report  # unpinned and no backup taken yet: nothing of ours in storage
    backend = get_backend(target)
    listings = sets.list_sets(backend, config.instance_id)
    rows = {s.set_id: s for s in BackupSet.objects.filter(set_id__in=[x.set_id for x in listings])}
    any_qa = BackupSet.objects.filter(instance_id=config.instance_id, qa_passed_at__isnull=False).exists()

    by_policy = {}
    garbage = []
    for listing in listings:
        row = rows.get(listing.set_id)
        if not listing.complete:
            if now - listing.created > incomplete_grace(config):
                garbage.append(listing)
            continue
        policy = row.policy_name if row else ''
        if not row:
            try:
                policy = sets.load_manifest(backend, config.instance_id, listing.set_id).header.get('policy', '')
            except Exception:
                policy = ''
        verified = bool(row and row.verify_level >= VerifyLevelChoices.INTEGRITY)
        by_policy.setdefault(policy, []).append((listing, retention.Candidate(
            set_id=listing.set_id, created=listing.created, verified=verified,
            qa_passed=bool(row and row.qa_passed_at) and any_qa,
        )))

    to_delete = []
    for policy, items in by_policy.items():
        rules, tz = _retention_for(config, target, policy)
        newest = sorted((c for _, c in items), key=lambda c: c.created, reverse=True)[:max(rules.keep_last, 1)]
        if not any(c.verified for c in newest):
            # GFS counts only verified sets: with the newest all unverified (e.g. checksums failing after a
            # bucket switched to SSE-KMS), retention would let older history go. Hold until a set verifies.
            report.held[policy or '(unknown)'] = f"the newest {len(newest)} set(s) are unverified"
            continue
        keep, prune = retention.plan([c for _, c in items], rules, now, tz or settings.TIME_ZONE)
        report.keep.update(keep)
        listing_by_id = {listing.set_id: listing for listing, _ in items}
        to_delete += [listing_by_id[set_id] for set_id in prune]
    to_delete += garbage

    for listing in to_delete:
        row = rows.get(listing.set_id)
        locked = [o.retain_until for o in listing.objects.values() if o.retain_until and o.retain_until > now]
        if locked:
            report.locked[listing.set_id] = max(locked).isoformat()
            continue
        policy = config.policies.get(row.policy_name) if row else None
        on_primary = bool(policy and policy.target == target.name)
        if listing.complete and on_primary and row.copies.filter(is_primary=False).exclude(
                status__in=(CopyStatusChoices.UPLOADED, CopyStatusChoices.VERIFIED,
                            CopyStatusChoices.DELETED)).exists():
            # The primary does not prune a set until all its required replicas hold it (an incomplete set never
            # reached them)
            report.waiting_for_replicas.append(listing.set_id)
            continue
        report.delete.append(listing.set_id)

    report.probes_due = _expired_probes(backend, config.instance_id, now)

    if not report.can_delete:
        if not dry_run and report.delete:
            log.info(f"{target.name}: {len(report.delete)} set(s) due for pruning; no prune credential here, "
                     f"so nothing was deleted")
        return report

    pruner = get_backend(target, prune=True)
    for set_id in report.delete:
        listing = next(x for x in to_delete if x.set_id == set_id)
        names = sorted(listing.objects, key=lambda n: (n != layout.COMPLETE, n == layout.MANIFEST, n))
        errors = []
        for name in names:
            key = layout.object_key(config.instance_id, set_id, name)
            errors += pruner.delete([key])
            if errors:
                break
        if errors:
            report.errors += [f'{e.key}: {e.reason}' for e in errors]
            continue
        report.deleted.append(set_id)
        log.info(f"{target.name}: pruned set {set_id}")
        BackupCopy.objects.filter(set__set_id=set_id, target__name=target.name).update(
            status=CopyStatusChoices.DELETED)
        row = rows.get(set_id)
        if row and not row.copies.exclude(status=CopyStatusChoices.DELETED).exists():
            row.status = SetStatusChoices.EXPIRED
            row.expired_at = now
            row.save(update_fields=('status', 'expired_at'))
    for key in report.probes_due:
        errors = pruner.delete([key])
        report.errors += [f'{e.key}: {e.reason}' for e in errors]
        report.probes_deleted += not errors
    try:
        report.aborted_uploads = pruner.abort_incomplete(timedelta(days=1))
    except StorageError as e:
        report.errors.append(f'abort incomplete uploads: {e}')
    return report


def _expired_probes(backend, instance_id, now):
    """
    Probe objects ("target probe" writes them below <instance_id>/.probe/) older than a day whose lock has expired.
    Listings don't always carry the lock date (S3), so each candidate is looked up.
    """
    due = []
    for info in backend.list(f'{instance_id}/{PROBE_DIR}/'):
        if info.last_modified and now - info.last_modified < INCOMPLETE_GRACE:
            continue
        retain_until = info.retain_until
        if retain_until is None and backend.capabilities.default_retention:
            stat = backend.stat(info.key)
            retain_until = stat.retain_until if stat else None
        if retain_until is None or retain_until <= now:
            due.append(info.key)
    return due


def prune(dry_run=False, log=None, targets=None):
    config = conf.get_config()
    reports = []
    for name, target in config.targets.items():
        if targets and name not in targets:
            continue
        try:
            reports.append(prune_target(config, target, dry_run=dry_run, log=log))
        except StorageError as e:
            report = TargetReport(target=name, can_delete=False)
            report.errors.append(str(e))
            reports.append(report)
    return reports
