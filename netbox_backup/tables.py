import django_tables2 as tables
from django.utils.translation import gettext_lazy as _
from netbox.tables import NetBoxTable, columns

from .models import BackupCopy, BackupPolicy, BackupSet, RestoreRun, StorageTarget

__all__ = ('BackupCopyTable', 'BackupPolicyTable', 'BackupSetTable', 'RestoreRunTable', 'StorageTargetTable')

SIZE_TEMPLATE = '{% if value is not None %}{{ value|filesizeformat }}{% else %}&mdash;{% endif %}'
DURATION_TEMPLATE = (
    '{% if record.duration %}{{ record.duration.total_seconds|floatformat:0 }}s{% else %}&mdash;{% endif %}'
)


class StorageTargetTable(NetBoxTable):
    name = tables.Column(verbose_name=_('Name'), linkify=True)
    location = tables.Column(verbose_name=_('Location'))
    retention_days = tables.Column(verbose_name=_('Retention (days)'))
    quota_bytes = columns.TemplateColumn(template_code=SIZE_TEMPLATE, verbose_name=_('Quota'))
    offsite = columns.BooleanColumn(verbose_name=_('Off-site'))
    enabled = columns.BooleanColumn(verbose_name=_('Enabled'))
    probe_ok = columns.BooleanColumn(verbose_name=_('Probe OK'), orderable=False)
    last_probe_at = columns.DateTimeColumn(verbose_name=_('Probed'))
    actions = columns.ActionsColumn(actions=())

    class Meta(NetBoxTable.Meta):
        model = StorageTarget
        fields = ('pk', 'id', 'name', 'backend', 'location', 'prefix', 'retention_days', 'quota_bytes', 'offsite',
                  'enabled', 'probe_ok', 'last_probe_at')
        default_columns = ('pk', 'name', 'backend', 'location', 'retention_days', 'offsite', 'enabled', 'probe_ok')


class BackupPolicyTable(NetBoxTable):
    name = tables.Column(verbose_name=_('Name'), linkify=True)
    primary_target = tables.Column(verbose_name=_('Target'), linkify=True)
    components = columns.ArrayColumn(verbose_name=_('Components'))
    schedule = tables.Column(verbose_name=_('Schedule'))
    enabled = columns.BooleanColumn(verbose_name=_('Enabled'))
    actions = columns.ActionsColumn(actions=())

    class Meta(NetBoxTable.Meta):
        model = BackupPolicy
        fields = ('pk', 'id', 'name', 'primary_target', 'components', 'schedule', 'time_zone', 'max_runtime',
                  'enabled', 'last_scheduled')
        default_columns = ('pk', 'name', 'primary_target', 'components', 'schedule', 'enabled')


class BackupSetTable(NetBoxTable):
    set_id = tables.Column(verbose_name=_('Set'), linkify=True)
    policy = tables.Column(verbose_name=_('Policy'), linkify=True)
    status = columns.ChoiceFieldColumn(verbose_name=_('Status'))
    trigger = columns.ChoiceFieldColumn(verbose_name=_('Trigger'))
    started = columns.DateTimeColumn(verbose_name=_('Started'))
    finished = columns.DateTimeColumn(verbose_name=_('Finished'))
    duration = columns.TemplateColumn(template_code=DURATION_TEMPLATE, verbose_name=_('Duration'), orderable=False)
    size_bytes = columns.TemplateColumn(template_code=SIZE_TEMPLATE, verbose_name=_('Size'))
    verify_level = columns.ChoiceFieldColumn(verbose_name=_('Verified'))
    copies = columns.TemplateColumn(
        template_code='{{ record.copies.count }}', verbose_name=_('Copies'), orderable=False,
    )
    actions = columns.ActionsColumn(actions=())

    class Meta(NetBoxTable.Meta):
        model = BackupSet
        fields = ('pk', 'id', 'set_id', 'policy', 'status', 'trigger', 'created_by', 'started', 'finished',
                  'duration', 'size_bytes', 'netbox_version', 'verify_level', 'verified_at', 'qa_passed_at',
                  'copies', 'instance_id', 'error')
        default_columns = ('pk', 'set_id', 'policy', 'status', 'trigger', 'started', 'duration', 'size_bytes',
                           'netbox_version', 'verify_level', 'copies')


class BackupCopyTable(NetBoxTable):
    target = tables.Column(verbose_name=_('Target'), linkify=True)
    status = columns.ChoiceFieldColumn(verbose_name=_('Status'))
    is_primary = columns.BooleanColumn(verbose_name=_('Primary'))
    verify_level = columns.ChoiceFieldColumn(verbose_name=_('Verified'))
    locked_until = columns.DateTimeColumn(verbose_name=_('Locked until'))
    actions = columns.ActionsColumn(actions=())

    class Meta(NetBoxTable.Meta):
        model = BackupCopy
        fields = ('pk', 'target', 'status', 'is_primary', 'uploaded', 'verified', 'verify_level', 'checksum_mode',
                  'locked_until', 'error')
        default_columns = ('target', 'status', 'is_primary', 'verify_level', 'checksum_mode', 'locked_until')


class RestoreRunTable(NetBoxTable):
    restore_id = tables.Column(verbose_name=_('Restore'), linkify=True)
    set_id = tables.Column(verbose_name=_('Set'))
    strategy = columns.ChoiceFieldColumn(verbose_name=_('Strategy'))
    status = columns.ChoiceFieldColumn(verbose_name=_('Status'))
    started = columns.DateTimeColumn(verbose_name=_('Started'))
    finished = columns.DateTimeColumn(verbose_name=_('Finished'))
    rollback_until = columns.DateTimeColumn(verbose_name=_('Rollback until'))
    actions = columns.ActionsColumn(actions=())

    class Meta(NetBoxTable.Meta):
        model = RestoreRun
        fields = ('pk', 'id', 'restore_id', 'set_id', 'strategy', 'status', 'user', 'slot_database',
                  'previous_database', 'safety_set_id', 'started', 'finished', 'rollback_until')
        default_columns = ('pk', 'restore_id', 'set_id', 'strategy', 'status', 'user', 'started', 'rollback_until')
