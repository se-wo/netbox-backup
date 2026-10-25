from netbox.api.fields import ChoiceField
from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from ..choices import (
    BackupKindChoices,
    BackupStatusChoices,
    BackupTriggerChoices,
    RestoreStatusChoices,
    VerificationLevelChoices,
    VerificationStatusChoices,
)
from ..models import Backup, RestoreEvent

__all__ = (
    'BackupSerializer', 'RestoreEventSerializer', 'RunBackupSerializer', 'VerifyBackupSerializer',
)


class BackupSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_backup-api:backup-detail')
    kind = ChoiceField(choices=BackupKindChoices, read_only=True)
    status = ChoiceField(choices=BackupStatusChoices, read_only=True)
    trigger = ChoiceField(choices=BackupTriggerChoices, read_only=True)
    verification_status = ChoiceField(choices=VerificationStatusChoices, read_only=True)

    class Meta:
        model = Backup
        fields = (
            'id', 'url', 'display_url', 'display', 'name', 'instance_id', 'kind', 'status', 'trigger',
            'requested_by', 'started_at', 'completed_at', 'size', 'encrypted', 'netbox_version', 'location',
            'manifest', 'error', 'verification_status', 'verification_level', 'verified_at', 'verification_log',
            'job', 'tags', 'custom_fields', 'created', 'last_updated',
        )
        read_only_fields = (
            'name', 'instance_id', 'requested_by', 'started_at', 'completed_at', 'size', 'encrypted',
            'netbox_version', 'location', 'manifest', 'error', 'verification_level', 'verified_at',
            'verification_log', 'job',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'kind', 'status', 'started_at')


class RestoreEventSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_backup-api:restoreevent-detail')
    status = ChoiceField(choices=RestoreStatusChoices, read_only=True)

    class Meta:
        model = RestoreEvent
        fields = (
            'id', 'url', 'display_url', 'display', 'backup_name', 'backup_instance_id', 'backup_created', 'status',
            'started_at', 'completed_at', 'restored_by', 'hostname', 'components', 'netbox_version_backup',
            'netbox_version_running', 'safety_backup_name', 'row_count_mismatches', 'log', 'tags', 'custom_fields',
            'created', 'last_updated',
        )
        read_only_fields = fields[4:-4]
        brief_fields = ('id', 'url', 'display', 'backup_name', 'status', 'started_at')


class RunBackupSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=BackupKindChoices.values(), default=BackupKindChoices.KIND_DATABASE)
    verify_level = serializers.ChoiceField(choices=VerificationLevelChoices.values(), required=False,
                                           allow_null=True, default=None)


class VerifyBackupSerializer(serializers.Serializer):
    level = serializers.ChoiceField(choices=VerificationLevelChoices.values(),
                                    default=VerificationLevelChoices.LEVEL_DEEP)
