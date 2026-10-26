from netbox.api.fields import ChoiceField
from netbox.api.serializers import BaseModelSerializer, NetBoxModelSerializer
from rest_framework import serializers

from ..choices import (
    CopyStatusChoices,
    RestoreStatusChoices,
    RestoreStrategyChoices,
    SetStatusChoices,
    TriggerChoices,
    VerifyLevelChoices,
)
from ..models import BackupCopy, BackupPolicy, BackupSet, RestoreRun, StorageTarget

__all__ = (
    'BackupCopySerializer', 'BackupPolicySerializer', 'BackupSetSerializer', 'RestoreRunSerializer',
    'StorageTargetSerializer',
)


class StorageTargetSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_backup-api:storagetarget-detail')

    class Meta:
        model = StorageTarget
        fields = (
            'id', 'url', 'display_url', 'display', 'name', 'backend', 'config', 'secret_refs', 'prefix', 'location',
            'retention_days', 'quota_bytes', 'offsite', 'is_static', 'enabled', 'last_probe', 'last_probe_at',
            'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'backend')
        read_only_fields = fields


class BackupPolicySerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_backup-api:backuppolicy-detail')
    primary_target = StorageTargetSerializer(nested=True, read_only=True)
    replica_targets = StorageTargetSerializer(nested=True, many=True, read_only=True)

    class Meta:
        model = BackupPolicy
        fields = (
            'id', 'url', 'display_url', 'display', 'name', 'components', 'schedule', 'time_zone', 'primary_target',
            'replica_targets', 'retention', 'recipients', 'zstd_level', 'verify_schedule', 'max_runtime', 'enabled',
            'is_static', 'last_scheduled', 'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name')
        read_only_fields = fields


class BackupCopySerializer(BaseModelSerializer):
    target = StorageTargetSerializer(nested=True, read_only=True)
    status = ChoiceField(choices=CopyStatusChoices, read_only=True)
    verify_level = ChoiceField(choices=VerifyLevelChoices, read_only=True)

    class Meta:
        model = BackupCopy
        fields = ('id', 'display', 'target', 'status', 'is_primary', 'uploaded', 'verified', 'verify_level',
                  'checksum_mode', 'locked_until', 'copied_objects', 'attempts', 'last_attempt', 'error')
        brief_fields = ('id', 'display', 'target', 'status')


class BackupSetSerializer(BaseModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_backup-api:backupset-detail')
    policy = BackupPolicySerializer(nested=True, read_only=True)
    status = ChoiceField(choices=SetStatusChoices, read_only=True)
    trigger = ChoiceField(choices=TriggerChoices, read_only=True)
    verify_level = ChoiceField(choices=VerifyLevelChoices, read_only=True)
    copies = BackupCopySerializer(many=True, read_only=True)

    class Meta:
        model = BackupSet
        fields = (
            'id', 'url', 'display_url', 'display', 'set_id', 'instance_id', 'policy', 'policy_name', 'status',
            'trigger', 'created_by', 'started', 'finished', 'netbox_version', 'size_bytes', 'manifest_sha384',
            'components', 'baseline', 'warnings', 'error', 'verify_level', 'verified_at', 'qa_passed_at',
            'recovery_drill_at', 'qa_report', 'copies', 'job',
        )
        brief_fields = ('id', 'url', 'display', 'set_id', 'status')


class RestoreRunSerializer(BaseModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_backup-api:restorerun-detail')
    status = ChoiceField(choices=RestoreStatusChoices, read_only=True)
    strategy = ChoiceField(choices=RestoreStrategyChoices, read_only=True)

    class Meta:
        model = RestoreRun
        fields = (
            'id', 'url', 'display_url', 'display', 'restore_id', 'set_id', 'source_instance_id', 'components',
            'strategy', 'status', 'steps', 'plan', 'slot_database', 'previous_database', 'safety_set_id',
            'rollback_until', 'user', 'started', 'finished',
        )
        brief_fields = ('id', 'url', 'display', 'restore_id', 'status')
