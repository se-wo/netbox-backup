from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from netbox.api.viewsets import NetBoxReadOnlyModelViewSet
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import conf, filtersets
from ..choices import TriggerChoices
from ..core import layout, sets
from ..engine import health
from ..jobs import enqueue_backup
from ..models import BackupPolicy, BackupSet, RestoreRun, StorageTarget
from ..storage import StorageError, get_backend
from . import serializers


class StorageTargetViewSet(NetBoxReadOnlyModelViewSet):
    queryset = StorageTarget.objects.all()
    serializer_class = serializers.StorageTargetSerializer
    filterset_class = filtersets.StorageTargetFilterSet


class BackupPolicyViewSet(NetBoxReadOnlyModelViewSet):
    queryset = BackupPolicy.objects.select_related('primary_target').prefetch_related('replica_targets')
    serializer_class = serializers.BackupPolicySerializer
    filterset_class = filtersets.BackupPolicyFilterSet

    @extend_schema(request=None, responses={202: None})
    @action(detail=True, methods=['post'])
    def run(self, request, pk):
        """
        Start a manual backup of this policy. Needs netbox_backup.run_backuppolicy.
        """
        if not request.user.has_perm('netbox_backup.run_backuppolicy'):
            raise PermissionDenied('This user lacks the run_backuppolicy permission')
        policy = get_object_or_404(self.queryset.restrict(request.user, 'view'), pk=pk)
        job = enqueue_backup(policy, trigger=TriggerChoices.API, user=request.user, created_by=request.user.username)
        return Response({'job': job.pk, 'job_id': str(job.job_id)}, status=status.HTTP_202_ACCEPTED)


class BackupSetViewSet(NetBoxReadOnlyModelViewSet):
    queryset = BackupSet.objects.select_related('policy').prefetch_related('copies__target')
    serializer_class = serializers.BackupSetSerializer
    filterset_class = filtersets.BackupSetFilterSet

    @extend_schema(request=None, responses={200: None})
    @action(detail=True, methods=['post'])
    def download(self, request, pk):
        """
        Presigned URLs (valid five minutes or less) for the set's objects, ciphertext only. Off unless
        allow_download is set; superusers only, because a set contains every password hash and token.
        """
        config = conf.get_config()
        if not config.allow_download:
            raise PermissionDenied('Downloads are disabled (allow_download)')
        if not request.user.is_superuser:
            raise PermissionDenied('Downloads are restricted to superusers')
        backup_set = get_object_or_404(self.queryset.restrict(request.user, 'view'), pk=pk)
        for copy in backup_set.copies.all():
            target = config.targets.get(copy.target.name)
            if target is None:
                continue
            backend = get_backend(target)
            if not hasattr(backend, 'presign'):
                continue
            try:
                listing = sets.load_manifest(backend, backup_set.instance_id, backup_set.set_id)
            except (sets.SetError, StorageError):
                continue
            names = [layout.MANIFEST, layout.COMPLETE, *listing.objects]
            urls = {n: backend.presign(layout.object_key(backup_set.instance_id, backup_set.set_id, n))
                    for n in names}
            return Response({'target': copy.target.name, 'expires_in': 300, 'urls': urls})
        return Response({'detail': 'No copy on a target that supports presigned URLs'},
                        status=status.HTTP_501_NOT_IMPLEMENTED)


class RestoreRunViewSet(NetBoxReadOnlyModelViewSet):
    queryset = RestoreRun.objects.all()
    serializer_class = serializers.RestoreRunSerializer
    filterset_class = filtersets.RestoreRunFilterSet


class StatusView(APIView):
    """
    One JSON health summary for monitoring. Needs netbox_backup.view_backupset.
    """
    queryset = BackupSet.objects.all()

    def get_view_name(self):
        return 'Backup status'

    @extend_schema(responses={200: None})
    def get(self, request):
        return Response(health.summary())
