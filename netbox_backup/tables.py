import django_tables2 as tables
from django.utils.translation import gettext_lazy as _
from netbox.tables import NetBoxTable, columns

from .models import Backup, RestoreEvent

__all__ = ('BackupTable', 'RestoreEventTable')

SIZE_TEMPLATE = '{% if value is not None %}{{ value|filesizeformat }}{% else %}&mdash;{% endif %}'
DURATION_TEMPLATE = (
    '{% if record.duration %}{{ record.duration.total_seconds|floatformat:1 }}s{% else %}&mdash;{% endif %}'
)


class BackupTable(NetBoxTable):
    name = tables.Column(verbose_name=_('Name'), linkify=True)
    kind = columns.ChoiceFieldColumn(verbose_name=_('Kind'))
    status = columns.ChoiceFieldColumn(verbose_name=_('Status'))
    trigger = columns.ChoiceFieldColumn(verbose_name=_('Trigger'))
    started_at = columns.DateTimeColumn(verbose_name=_('Started'))
    completed_at = columns.DateTimeColumn(verbose_name=_('Completed'))
    duration = columns.TemplateColumn(template_code=DURATION_TEMPLATE, verbose_name=_('Duration'), orderable=False)
    size = columns.TemplateColumn(template_code=SIZE_TEMPLATE, verbose_name=_('Size'))
    encrypted = columns.BooleanColumn(verbose_name=_('Encrypted'))
    verification_status = columns.ChoiceFieldColumn(verbose_name=_('Verification'))
    verified_at = columns.DateTimeColumn(verbose_name=_('Verified'))
    tags = columns.TagColumn(url_name='plugins:netbox_backup:backup_list')
    actions = columns.ActionsColumn(actions=('delete',))

    class Meta(NetBoxTable.Meta):
        model = Backup
        fields = (
            'pk', 'id', 'name', 'kind', 'status', 'trigger', 'requested_by', 'instance_id', 'started_at',
            'completed_at', 'duration', 'size', 'encrypted', 'netbox_version', 'verification_status',
            'verification_level', 'verified_at', 'location', 'error', 'tags', 'created', 'last_updated',
        )
        default_columns = (
            'pk', 'name', 'kind', 'status', 'trigger', 'started_at', 'duration', 'size', 'encrypted',
            'netbox_version', 'verification_status',
        )


class RestoreEventTable(NetBoxTable):
    id = tables.Column(verbose_name=_('ID'), linkify=True)
    backup_name = tables.Column(verbose_name=_('Backup'))
    status = columns.ChoiceFieldColumn(verbose_name=_('Status'))
    started_at = columns.DateTimeColumn(verbose_name=_('Started'))
    completed_at = columns.DateTimeColumn(verbose_name=_('Completed'))
    components = columns.ArrayColumn(verbose_name=_('Components'))
    tags = columns.TagColumn(url_name='plugins:netbox_backup:restoreevent_list')
    actions = columns.ActionsColumn(actions=('delete',))

    class Meta(NetBoxTable.Meta):
        model = RestoreEvent
        fields = (
            'pk', 'id', 'backup_name', 'backup_instance_id', 'backup_created', 'status', 'started_at',
            'completed_at', 'restored_by', 'hostname', 'components', 'netbox_version_backup',
            'netbox_version_running', 'safety_backup_name', 'tags', 'created',
        )
        default_columns = (
            'pk', 'id', 'backup_name', 'status', 'started_at', 'restored_by', 'hostname', 'components',
            'safety_backup_name',
        )
