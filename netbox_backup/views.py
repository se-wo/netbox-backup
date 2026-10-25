from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.generic import View
from netbox.object_actions import BulkDelete, BulkExport, DeleteObject
from netbox.views import generic
from utilities.views import ConditionalLoginRequiredMixin, register_model_view

from . import conf, filtersets, forms, tables
from .choices import BackupTriggerChoices, VerificationLevelChoices
from .engine import catalog, health
from .engine.backup import storage_key
from .jobs import BackupJob, VerifyBackupJob
from .models import Backup, RestoreEvent
from .storage import get_storage


def _deny(request, message):
    messages.error(request, message)
    return HttpResponseForbidden(message)


#
# Backups
#

@register_model_view(Backup, 'list', path='', detail=False)
class BackupListView(generic.ObjectListView):
    queryset = Backup.objects.all()
    filterset = filtersets.BackupFilterSet
    filterset_form = forms.BackupFilterForm
    table = tables.BackupTable
    template_name = 'netbox_backup/backup_list.html'
    actions = (BulkExport, BulkDelete)


@register_model_view(Backup)
class BackupView(generic.ObjectView):
    queryset = Backup.objects.all()
    template_name = 'netbox_backup/backup.html'
    actions = (DeleteObject,)

    def get_extra_context(self, request, instance):
        row_counts = sorted(instance.row_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return {
            'top_tables': row_counts[:15],
            'table_count': len(row_counts),
            'verify_form': forms.VerifyBackupForm(),
            'can_run': request.user.has_perm('netbox_backup.run_backup'),
            'can_download': request.user.has_perm('netbox_backup.download_backup'),
            'restore_command': f'manage.py netbox_backup restore {instance.name}',
        }


@register_model_view(Backup, 'delete')
class BackupDeleteView(generic.ObjectDeleteView):
    queryset = Backup.objects.all()


@register_model_view(Backup, 'bulk_delete', path='delete', detail=False)
class BackupBulkDeleteView(generic.BulkDeleteView):
    queryset = Backup.objects.all()
    filterset = filtersets.BackupFilterSet
    table = tables.BackupTable


@register_model_view(Backup, 'run', path='run', detail=False)
class BackupRunView(ConditionalLoginRequiredMixin, View):
    """
    Queue a backup as a background job.
    """
    def get(self, request):
        if not request.user.has_perm('netbox_backup.run_backup'):
            return _deny(request, _('You do not have permission to run backups.'))
        return render(request, 'netbox_backup/backup_run.html', {
            'form': forms.RunBackupForm(),
            'storage': _storage_description(),
            'encrypted': bool(conf.encryption_key()),
            'include_configuration': conf.include_configuration(),
        })

    def post(self, request):
        if not request.user.has_perm('netbox_backup.run_backup'):
            return _deny(request, _('You do not have permission to run backups.'))
        form = forms.RunBackupForm(request.POST)
        if not form.is_valid():
            return render(request, 'netbox_backup/backup_run.html', {'form': form})
        job = BackupJob.enqueue(
            user=request.user,
            kind=form.cleaned_data['kind'],
            trigger=BackupTriggerChoices.TRIGGER_MANUAL,
            requested_by=request.user.username,
            verify_level=form.cleaned_data['verify_level'] or None,
        )
        messages.success(request, _('Backup queued as job {id}.').format(id=job.pk))
        return redirect('core:job', pk=job.pk)


@register_model_view(Backup, 'verify')
class BackupVerifyView(ConditionalLoginRequiredMixin, View):
    def post(self, request, pk):
        if not request.user.has_perm('netbox_backup.run_backup'):
            return _deny(request, _('You do not have permission to verify backups.'))
        backup = get_object_or_404(Backup.objects.restrict(request.user, 'view'), pk=pk)
        form = forms.VerifyBackupForm(request.POST)
        level = form.cleaned_data['level'] if form.is_valid() else VerificationLevelChoices.LEVEL_DEEP
        job = VerifyBackupJob.enqueue(user=request.user, backup_id=backup.pk, level=level)
        messages.success(request, _('Verification of {name} queued as job {id}.').format(name=backup, id=job.pk))
        return redirect('core:job', pk=job.pk)


@register_model_view(Backup, 'download', path='download/<str:artifact>')
class BackupDownloadView(ConditionalLoginRequiredMixin, View):
    """
    Stream an artifact exactly as stored (encrypted artifacts stay encrypted; decrypt with the CLI).
    """
    def get(self, request, pk, artifact):
        if not request.user.has_perm('netbox_backup.download_backup'):
            return _deny(request, _('You do not have permission to download backups.'))
        backup = get_object_or_404(Backup.objects.restrict(request.user, 'view'), pk=pk)
        names = {a['name'] for a in backup.artifacts} | {'manifest.json'}
        if artifact not in names:
            raise Http404
        stream = get_storage().open_read(storage_key(backup.instance_id, backup.name, artifact))
        return FileResponse(stream, as_attachment=True, filename=f'{backup.name}-{artifact}')


@register_model_view(Backup, 'sync', path='sync', detail=False)
class BackupSyncView(ConditionalLoginRequiredMixin, View):
    def post(self, request):
        if not request.user.has_perm('netbox_backup.run_backup'):
            return _deny(request, _('You do not have permission to sync the backup catalog.'))
        try:
            stats = catalog.sync()
            messages.success(request, _(
                'Catalog synchronised: {created} imported, {updated} updated, {missing} missing from storage.'
            ).format(**stats))
        except Exception as e:
            messages.error(request, _('Catalog sync failed: {error}').format(error=e))
        return redirect('plugins:netbox_backup:backup_list')


class BackupStatusView(ConditionalLoginRequiredMixin, View):
    """
    Recoverability overview: health conditions, configuration and recent activity.
    """
    def get(self, request):
        if not request.user.has_perm('netbox_backup.view_backup'):
            return _deny(request, _('You do not have permission to view backups.'))
        conds = health.conditions()
        recent = tables.BackupTable(Backup.objects.restrict(request.user, 'view')[:10], orderable=False)
        shown = ('name', 'kind', 'status', 'trigger', 'started_at', 'duration', 'size', 'verification_status')
        for column in recent.columns:
            if column.name not in shown:
                recent.columns.hide(column.name)
        return render(request, 'netbox_backup/status.html', {
            'conditions': conds,
            'overall': health.overall(conds),
            'overall_color': health.COLORS[health.overall(conds)],
            'instance_id': conf.instance_id(),
            'storage': _storage_description(),
            'schedules': {
                _('Database backups'): conf.interval('database'),
                _('Full backups'): conf.interval('full'),
                _('Housekeeping'): conf.get('housekeeping_interval'),
            },
            'retention': conf.get('retention'),
            'keep_last': conf.get('keep_last'),
            'encrypted': bool(conf.encryption_key()),
            'include_configuration': conf.include_configuration(),
            'include_files': conf.get('include_files'),
            'recent_backups': recent,
            'recent_restores': RestoreEvent.objects.restrict(request.user, 'view')[:5],
            'can_run': request.user.has_perm('netbox_backup.run_backup'),
        })


def _storage_description():
    try:
        return get_storage().describe()
    except Exception as e:
        return {'error': str(e)}


#
# Restore events
#

@register_model_view(RestoreEvent, 'list', path='', detail=False)
class RestoreEventListView(generic.ObjectListView):
    queryset = RestoreEvent.objects.all()
    filterset = filtersets.RestoreEventFilterSet
    filterset_form = forms.RestoreEventFilterForm
    table = tables.RestoreEventTable
    actions = (BulkExport, BulkDelete)


@register_model_view(RestoreEvent)
class RestoreEventView(generic.ObjectView):
    queryset = RestoreEvent.objects.all()
    template_name = 'netbox_backup/restoreevent.html'
    actions = (DeleteObject,)

    def get_extra_context(self, request, instance):
        return {
            'backup': Backup.objects.filter(name=instance.backup_name).first(),
            'safety_backup': Backup.objects.filter(name=instance.safety_backup_name).first()
            if instance.safety_backup_name else None,
        }


@register_model_view(RestoreEvent, 'delete')
class RestoreEventDeleteView(generic.ObjectDeleteView):
    queryset = RestoreEvent.objects.all()


@register_model_view(RestoreEvent, 'bulk_delete', path='delete', detail=False)
class RestoreEventBulkDeleteView(generic.BulkDeleteView):
    queryset = RestoreEvent.objects.all()
    filterset = filtersets.RestoreEventFilterSet
    table = tables.RestoreEventTable
