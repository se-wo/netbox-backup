import django_filters
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from netbox.filtersets import BaseFilterSet, NetBoxModelFilterSet

from .choices import RestoreStatusChoices, RestoreStrategyChoices, SetStatusChoices, TriggerChoices, VerifyLevelChoices
from .models import BackupPolicy, BackupSet, RestoreRun, StorageTarget

__all__ = ('BackupPolicyFilterSet', 'BackupSetFilterSet', 'RestoreRunFilterSet', 'StorageTargetFilterSet')


class StorageTargetFilterSet(NetBoxModelFilterSet):

    class Meta:
        model = StorageTarget
        fields = ('id', 'name', 'backend', 'offsite', 'enabled')

    def search(self, queryset, name, value):
        return queryset.filter(Q(name__icontains=value) | Q(location__icontains=value))


class BackupPolicyFilterSet(NetBoxModelFilterSet):
    primary_target_id = django_filters.ModelMultipleChoiceFilter(queryset=StorageTarget.objects.all())

    class Meta:
        model = BackupPolicy
        fields = ('id', 'name', 'enabled')

    def search(self, queryset, name, value):
        return queryset.filter(name__icontains=value)


class BackupSetFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(method='search', label=_('Search'))
    policy_id = django_filters.ModelMultipleChoiceFilter(queryset=BackupPolicy.objects.all(), label=_('Policy'))
    status = django_filters.MultipleChoiceFilter(choices=SetStatusChoices)
    trigger = django_filters.MultipleChoiceFilter(choices=TriggerChoices)
    verify_level = django_filters.MultipleChoiceFilter(choices=VerifyLevelChoices)
    started__after = django_filters.DateTimeFilter(field_name='started', lookup_expr='gte', label=_('Started after'))
    started__before = django_filters.DateTimeFilter(field_name='started', lookup_expr='lte',
                                                    label=_('Started before'))
    target_id = django_filters.ModelMultipleChoiceFilter(
        field_name='copies__target', queryset=StorageTarget.objects.all(), label=_('Target'), distinct=True,
    )

    class Meta:
        model = BackupSet
        fields = ('id', 'set_id', 'instance_id', 'policy_name', 'netbox_version')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(set_id__icontains=value) | Q(policy_name__icontains=value)
                               | Q(created_by__icontains=value) | Q(error__icontains=value))


class RestoreRunFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(method='search', label=_('Search'))
    status = django_filters.MultipleChoiceFilter(choices=RestoreStatusChoices)
    strategy = django_filters.MultipleChoiceFilter(choices=RestoreStrategyChoices)

    class Meta:
        model = RestoreRun
        fields = ('id', 'restore_id', 'set_id', 'user', 'slot_database')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(restore_id__icontains=value) | Q(set_id__icontains=value) | Q(user__icontains=value))
