import django_filters
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from netbox.filtersets import NetBoxModelFilterSet

from .choices import (
    BackupKindChoices,
    BackupStatusChoices,
    BackupTriggerChoices,
    RestoreStatusChoices,
    VerificationStatusChoices,
)
from .models import Backup, RestoreEvent

__all__ = ('BackupFilterSet', 'RestoreEventFilterSet')


class BackupFilterSet(NetBoxModelFilterSet):
    kind = django_filters.MultipleChoiceFilter(choices=BackupKindChoices)
    status = django_filters.MultipleChoiceFilter(choices=BackupStatusChoices)
    trigger = django_filters.MultipleChoiceFilter(choices=BackupTriggerChoices)
    verification_status = django_filters.MultipleChoiceFilter(choices=VerificationStatusChoices)
    started_at = django_filters.DateTimeFilter()
    started_at__before = django_filters.DateTimeFilter(field_name='started_at', lookup_expr='lte',
                                                       label=_('Started before'))
    started_at__after = django_filters.DateTimeFilter(field_name='started_at', lookup_expr='gte',
                                                      label=_('Started after'))

    class Meta:
        model = Backup
        fields = ('id', 'name', 'instance_id', 'encrypted', 'netbox_version', 'requested_by')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) | Q(instance_id__icontains=value) | Q(error__icontains=value) |
            Q(requested_by__icontains=value)
        )


class RestoreEventFilterSet(NetBoxModelFilterSet):
    status = django_filters.MultipleChoiceFilter(choices=RestoreStatusChoices)

    class Meta:
        model = RestoreEvent
        fields = ('id', 'backup_name', 'backup_instance_id', 'restored_by', 'hostname')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(backup_name__icontains=value) | Q(restored_by__icontains=value) | Q(hostname__icontains=value)
        )
