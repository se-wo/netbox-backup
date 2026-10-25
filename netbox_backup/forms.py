from django import forms
from django.utils.translation import gettext_lazy as _
from netbox.forms import NetBoxModelFilterSetForm
from utilities.forms.fields import TagFilterField
from utilities.forms.rendering import FieldSet
from utilities.forms.utils import add_blank_choice
from utilities.forms.widgets import DateTimePicker

from .choices import (
    BackupKindChoices,
    BackupStatusChoices,
    BackupTriggerChoices,
    RestoreStatusChoices,
    VerificationLevelChoices,
    VerificationStatusChoices,
)
from .models import Backup, RestoreEvent

__all__ = ('BackupFilterForm', 'RestoreEventFilterForm', 'RunBackupForm', 'VerifyBackupForm')


class BackupFilterForm(NetBoxModelFilterSetForm):
    model = Backup
    fieldsets = (
        FieldSet('q', 'filter_id', 'tag'),
        FieldSet('kind', 'status', 'trigger', 'verification_status', 'encrypted', name=_('Attributes')),
        FieldSet('started_at__after', 'started_at__before', 'instance_id', name=_('Origin')),
    )
    kind = forms.MultipleChoiceField(label=_('Kind'), choices=BackupKindChoices, required=False)
    status = forms.MultipleChoiceField(label=_('Status'), choices=BackupStatusChoices, required=False)
    trigger = forms.MultipleChoiceField(label=_('Trigger'), choices=BackupTriggerChoices, required=False)
    verification_status = forms.MultipleChoiceField(
        label=_('Verification'), choices=VerificationStatusChoices, required=False,
    )
    encrypted = forms.NullBooleanField(
        label=_('Encrypted'), required=False,
        widget=forms.Select(choices=[('', '---------'), ('true', _('Yes')), ('false', _('No'))]),
    )
    started_at__after = forms.DateTimeField(label=_('Started after'), required=False, widget=DateTimePicker())
    started_at__before = forms.DateTimeField(label=_('Started before'), required=False, widget=DateTimePicker())
    instance_id = forms.CharField(label=_('Instance'), required=False)
    tag = TagFilterField(model)


class RestoreEventFilterForm(NetBoxModelFilterSetForm):
    model = RestoreEvent
    fieldsets = (
        FieldSet('q', 'filter_id', 'tag'),
        FieldSet('status', 'restored_by', name=_('Attributes')),
    )
    status = forms.MultipleChoiceField(label=_('Status'), choices=RestoreStatusChoices, required=False)
    restored_by = forms.CharField(label=_('Restored by'), required=False)
    tag = TagFilterField(model)


class RunBackupForm(forms.Form):
    kind = forms.ChoiceField(
        label=_('Kind'), choices=BackupKindChoices, initial=BackupKindChoices.KIND_FULL,
        help_text=_('Full backups also include media, scripts, reports and (if enabled) configuration.'),
    )
    verify_level = forms.ChoiceField(
        label=_('Verify afterwards'), choices=add_blank_choice(VerificationLevelChoices), required=False,
    )


class VerifyBackupForm(forms.Form):
    level = forms.ChoiceField(
        label=_('Level'), choices=VerificationLevelChoices, initial=VerificationLevelChoices.LEVEL_DEEP,
    )
