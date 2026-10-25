"""
Prometheus metrics through NetBox's METRICS_ENABLED. Worker processes can't publish to the web process's /metrics,
so these gauges are computed from the catalog at scrape time.
"""
import logging

from prometheus_client.core import GaugeMetricFamily

logger = logging.getLogger('netbox_backup')


class BackupCollector:

    def describe(self):
        return []

    def collect(self):
        try:
            yield from self._collect()
        except Exception as e:  # A scrape must never fail because the catalog is unavailable (e.g. mid-restore)
            logger.debug(f"netbox_backup metrics unavailable: {e}")

    def _collect(self):
        from django.db.models import Max
        from django.utils import timezone

        from . import conf
        from .choices import SetStatusChoices
        from .engine import scheduler
        from .models import BackupCopy, BackupPolicy, BackupSet, StorageTarget

        config = conf.get_config()
        now = timezone.now()
        usable = BackupSet.objects.filter(instance_id=config.instance_id, status__in=SetStatusChoices.USABLE)

        success = GaugeMetricFamily('netbox_backup_last_success_timestamp_seconds',
                                    'Finish time of the newest verified set', labels=['policy', 'target'])
        rows = BackupCopy.objects.filter(set__in=usable, verify_level__gte=1).values(
            'set__policy_name', 'target__name').annotate(last=Max('set__finished'))
        seen = set()
        for row in rows:
            if row['last']:
                success.add_metric([row['set__policy_name'], row['target__name']], row['last'].timestamp())
                seen.add(row['set__policy_name'])
        # A policy without a verified set yet reports its creation time, so "no success within the interval"
        # fires on a fresh install whose first backup never succeeds
        for policy in BackupPolicy.objects.filter(enabled=True).exclude(name__in=seen).select_related(
                'primary_target'):
            success.add_metric([policy.name, policy.primary_target.name], policy.created.timestamp())
        yield success

        verified = GaugeMetricFamily('netbox_backup_last_verified_timestamp_seconds',
                                     'Newest verification per policy and level', labels=['policy', 'level'])
        for level in (1, 2):
            field = 'verified_at' if level == 1 else 'qa_passed_at'
            for row in usable.filter(verify_level__gte=level).values('policy_name').annotate(last=Max(field)):
                if row['last']:
                    verified.add_metric([row['policy_name'], str(level)], row['last'].timestamp())
        yield verified

        tick = scheduler.last_tick()
        yield GaugeMetricFamily('netbox_backup_scheduler_last_tick_timestamp_seconds', 'Last scheduler run',
                                value=tick.timestamp() if tick else 0)
        yield GaugeMetricFamily('netbox_backup_enabled_policies', 'Enabled policies',
                                value=sum(1 for p in config.policies.values() if p.enabled))

        duration = GaugeMetricFamily('netbox_backup_duration_seconds', 'Duration of the newest set',
                                     labels=['policy'])
        size = GaugeMetricFamily('netbox_backup_size_bytes', 'Stored size of the newest set per component',
                                 labels=['policy', 'component'])
        missing = GaugeMetricFamily('netbox_backup_media_missing', 'Referenced media files missing from the newest '
                                                                   'set', labels=['policy'])
        locked = GaugeMetricFamily('netbox_backup_locked_offsite_copies', 'Verified off-site copies under lock',
                                   labels=['policy'])
        for name in config.policies:
            newest = usable.filter(policy_name=name).order_by('-finished').first()
            if newest is None:
                continue
            if newest.duration:
                duration.add_metric([name], newest.duration.total_seconds())
            for component, info in newest.components.items():
                size.add_metric([name, component], info.get('size', 0))
            missing.add_metric([name], newest.components.get('media', {}).get('missing', 0))
            locked.add_metric([name], BackupCopy.objects.filter(
                set__in=usable.filter(policy_name=name), target__offsite=True, locked_until__gt=now,
                verify_level__gte=1).count())
        yield duration
        yield size
        yield missing
        yield locked

        up = GaugeMetricFamily('netbox_backup_target_up', 'Last probe of the target passed', labels=['target'])
        for target in StorageTarget.objects.filter(enabled=True):
            up.add_metric([target.name], 1 if target.probe_ok else 0)
        yield up


def register():
    from prometheus_client import REGISTRY
    if not getattr(register, 'done', False):
        REGISTRY.register(BackupCollector())
        register.done = True
