"""
Read-only UI. Targets and policies mirror static config; configuration changes and restores happen outside the
UI. The only actions are a manual backup (run_backuppolicy) and a level-1 check (verify_backupset).
"""
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.generic import View
from netbox.object_actions import BulkExport
from netbox.views import generic
from packaging.version import InvalidVersion, Version
from utilities.views import ConditionalLoginRequiredMixin, register_model_view

from . import conf, filtersets, forms, tables
from .choices import TriggerChoices
from .engine import health, scheduler
from .jobs import QUEUE, enqueue_backup, enqueue_verify
from .models import BackupPolicy, BackupSet, RestoreRun, StorageTarget


def _deny(request, message):
    messages.error(request, message)
    return HttpResponseForbidden(message)


def compatibility(backup_set):
    """
    (ok, reason): can this install restore the set? restore plan does the full check with the manifest body.
    """
    from django.conf import settings

    from .engine.restore import BROKEN_DUMP_VERSIONS
    if backup_set.netbox_version in BROKEN_DUMP_VERSIONS:
        return False, _('Sets from NetBox {v} lose ltree triggers').format(v=backup_set.netbox_version)
    try:
        source, running = Version(backup_set.netbox_version), Version(settings.VERSION.split('-')[0])
    except InvalidVersion:
        return None, _('Unknown NetBox version')
    if source > running:
        return False, _('Taken on NetBox {s}, newer than {r}: upgrade NetBox first').format(s=source, r=running)
    if source < running:
        return True, _('Taken on NetBox {s}; finish applies the migrations to {r}').format(s=source, r=running)
    return True, _('Same NetBox version')


#
# Storage targets
#

@register_model_view(StorageTarget, 'list', path='', detail=False)
class StorageTargetListView(generic.ObjectListView):
    queryset = StorageTarget.objects.all()
    filterset = filtersets.StorageTargetFilterSet
    filterset_form = forms.StorageTargetFilterForm
    table = tables.StorageTargetTable
    actions = (BulkExport,)


@register_model_view(StorageTarget)
class StorageTargetView(generic.ObjectView):
    queryset = StorageTarget.objects.all()
    template_name = 'netbox_backup/storagetarget.html'
    actions = ()

    def get_extra_context(self, request, instance):
        return {'policies': instance.policies.all()}


#
# Policies
#

@register_model_view(BackupPolicy, 'list', path='', detail=False)
class BackupPolicyListView(generic.ObjectListView):
    queryset = BackupPolicy.objects.select_related('primary_target')
    filterset = filtersets.BackupPolicyFilterSet
    filterset_form = forms.BackupPolicyFilterForm
    table = tables.BackupPolicyTable
    actions = (BulkExport,)


@register_model_view(BackupPolicy)
class BackupPolicyView(generic.ObjectView):
    queryset = BackupPolicy.objects.all()
    template_name = 'netbox_backup/backuppolicy.html'
    actions = ()

    def get_extra_context(self, request, instance):
        policy = conf.get_config().policies.get(instance.name)
        try:
            workers = scheduler.queue_workers()
        except Exception:
            workers = None
        recent = tables.BackupSetTable(instance.sets.restrict(request.user, 'view')[:10], orderable=False)
        return {
            'next_runs': scheduler.next_runs(policy) if policy else [],
            'worst_case_loss': scheduler.worst_case_loss(policy) if policy else None,
            'scheduling_active': workers,
            'queue': QUEUE,
            'recent_sets': recent,
            'can_run': request.user.has_perm('netbox_backup.run_backuppolicy'),
        }


@register_model_view(BackupPolicy, 'run')
class BackupPolicyRunView(ConditionalLoginRequiredMixin, View):

    def post(self, request, pk):
        if not request.user.has_perm('netbox_backup.run_backuppolicy'):
            return _deny(request, _('You do not have permission to run backups.'))
        policy = get_object_or_404(BackupPolicy.objects.restrict(request.user, 'view'), pk=pk)
        job = enqueue_backup(policy, trigger=TriggerChoices.MANUAL, user=request.user,
                             created_by=request.user.username)
        messages.success(request, _('Backup of {policy} queued as job {id}.').format(policy=policy, id=job.pk))
        return redirect('core:job', pk=job.pk)


#
# Sets
#

@register_model_view(BackupSet, 'list', path='', detail=False)
class BackupSetListView(generic.ObjectListView):
    queryset = BackupSet.objects.select_related('policy').prefetch_related('copies')
    filterset = filtersets.BackupSetFilterSet
    filterset_form = forms.BackupSetFilterForm
    table = tables.BackupSetTable
    actions = (BulkExport,)


@register_model_view(BackupSet)
class BackupSetView(generic.ObjectView):
    queryset = BackupSet.objects.select_related('policy', 'job')
    template_name = 'netbox_backup/backupset.html'
    actions = ()

    def get_extra_context(self, request, instance):
        ok, reason = compatibility(instance)
        tables_ = sorted((instance.baseline or {}).get('tables', {}).items(), key=lambda kv: (-kv[1], kv[0]))
        return {
            'copies': tables.BackupCopyTable(instance.copies.select_related('target'), orderable=False),
            'compatible': ok,
            'compatibility_reason': reason,
            'top_tables': tables_[:15],
            'table_count': len(tables_),
            'schemas': (instance.baseline or {}).get('schemas', {}),
            'can_verify': request.user.has_perm('netbox_backup.verify_backupset'),
            'restore_command': f'manage.py nbbackup restore plan {instance.set_id}',
        }


@register_model_view(BackupSet, 'verify')
class BackupSetVerifyView(ConditionalLoginRequiredMixin, View):

    def post(self, request, pk):
        if not request.user.has_perm('netbox_backup.verify_backupset'):
            return _deny(request, _('You do not have permission to verify backup sets.'))
        backup_set = get_object_or_404(BackupSet.objects.restrict(request.user, 'view'), pk=pk)
        job = enqueue_verify(backup_set.set_id, user=request.user)
        messages.success(request, _('Level-1 check of {set} queued as job {id}.').format(set=backup_set, id=job.pk))
        return redirect('core:job', pk=job.pk)


#
# Restores
#

@register_model_view(RestoreRun, 'list', path='', detail=False)
class RestoreRunListView(generic.ObjectListView):
    queryset = RestoreRun.objects.all()
    filterset = filtersets.RestoreRunFilterSet
    filterset_form = forms.RestoreRunFilterForm
    table = tables.RestoreRunTable
    actions = (BulkExport,)


@register_model_view(RestoreRun)
class RestoreRunView(generic.ObjectView):
    queryset = RestoreRun.objects.all()
    template_name = 'netbox_backup/restorerun.html'
    actions = ()

    def get_extra_context(self, request, instance):
        return {'backup_set': BackupSet.objects.filter(set_id=instance.set_id).first()}


#
# Status
#

class StatusView(ConditionalLoginRequiredMixin, View):
    """
    Last success and next run per policy, copy matrix (set x target), verify freshness, capacity per target.
    """
    def get(self, request):
        if not request.user.has_perm('netbox_backup.view_backupset'):
            return _deny(request, _('You do not have permission to view backups.'))
        conds = health.conditions()
        config = None
        try:
            config = conf.get_config()
        except Exception:
            pass
        policies = []
        for row in BackupPolicy.objects.restrict(request.user, 'view').select_related('primary_target'):
            policy = config.policies.get(row.name) if config else None
            newest = row.sets.filter(verify_level__gte=1).order_by('-finished').first()
            policies.append({
                'row': row, 'newest': newest,
                'next_run': (scheduler.next_runs(policy, 1) or [None])[0] if policy else None,
                'loss': scheduler.worst_case_loss(policy) if policy else None,
            })
        targets = list(StorageTarget.objects.restrict(request.user, 'view').filter(enabled=True))
        recent = list(BackupSet.objects.restrict(request.user, 'view').prefetch_related('copies')[:10])
        matrix = []
        for backup_set in recent:
            by_target = {c.target_id: c for c in backup_set.copies.all()}
            matrix.append({'set': backup_set, 'copies': [by_target.get(t.pk) for t in targets]})
        capacity = []
        for target in targets:
            used = sum(s for s in BackupSet.objects.filter(copies__target=target, status__in=('uploaded', 'verified',
                       'partial')).values_list('size_bytes', flat=True) if s)
            capacity.append({'target': target, 'used': used,
                             'percent': round(100 * used / target.quota_bytes) if target.quota_bytes else None})
        return render(request, 'netbox_backup/status.html', {
            'conditions': conds,
            'overall': health.overall(conds),
            'overall_color': health.COLORS[health.overall(conds)],
            'instance_id': config.instance_id if config else '',
            'policies': policies,
            'targets': targets,
            'matrix': matrix,
            'capacity': capacity,
            'restores': RestoreRun.objects.restrict(request.user, 'view')[:5],
            'last_tick': scheduler.last_tick(),
            'pitr': health.pitr_conditions(),
        })
