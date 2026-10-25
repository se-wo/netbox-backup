from django import forms
from django.utils.translation import gettext_lazy as _
from netbox.forms import NetBoxModelFilterSetForm
from utilities.forms import FilterForm
from utilities.forms.fields import DynamicModelMultipleChoiceField
from utilities.forms.rendering import FieldSet
from utilities.forms.widgets import DateTimePicker

from .choices import RestoreStatusChoices, RestoreStrategyChoices, SetStatusChoices, TriggerChoices, VerifyLevelChoices
from .models import BackupPolicy, BackupSet, RestoreRun, StorageTarget

__all__ = ('BackupPolicyFilterForm', 'BackupSetFilterForm', 'RestoreRunFilterForm', 'StorageTargetFilterForm')


class StorageTargetFilterForm(NetBoxModelFilterSetForm):
    model = StorageTarget
    fieldsets = (FieldSet('q', 'filter_id'),)


class BackupPolicyFilterForm(NetBoxModelFilterSetForm):
    model = BackupPolicy
    fieldsets = (FieldSet('q', 'filter_id'),)


class BackupSetFilterForm(FilterForm):
    model = BackupSet
    fieldsets = (
        FieldSet('q', 'filter_id'),
        FieldSet('policy_id', 'target_id', 'status', 'trigger', 'verify_level', name=_('Attributes')),
        FieldSet('started__after', 'started__before', name=_('Time')),
    )
    policy_id = DynamicModelMultipleChoiceField(queryset=BackupPolicy.objects.all(), required=False,
                                                label=_('Policy'))
    target_id = DynamicModelMultipleChoiceField(queryset=StorageTarget.objects.all(), required=False,
                                                label=_('Target'))
    status = forms.MultipleChoiceField(label=_('Status'), choices=SetStatusChoices, required=False)
    trigger = forms.MultipleChoiceField(label=_('Trigger'), choices=TriggerChoices, required=False)
    verify_level = forms.MultipleChoiceField(label=_('Verified level'), choices=VerifyLevelChoices, required=False)
    started__after = forms.DateTimeField(label=_('Started after'), required=False, widget=DateTimePicker())
    started__before = forms.DateTimeField(label=_('Started before'), required=False, widget=DateTimePicker())


class RestoreRunFilterForm(FilterForm):
    model = RestoreRun
    fieldsets = (
        FieldSet('q', 'filter_id'),
        FieldSet('status', 'strategy', name=_('Attributes')),
    )
    status = forms.MultipleChoiceField(label=_('Status'), choices=RestoreStatusChoices, required=False)
    strategy = forms.MultipleChoiceField(label=_('Strategy'), choices=RestoreStrategyChoices, required=False)
