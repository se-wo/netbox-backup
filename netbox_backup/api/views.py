from core.api.serializers import JobSerializer
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from netbox.api.authentication import IsAuthenticatedOrLoginNotRequired
from netbox.api.viewsets import NetBoxReadOnlyModelViewSet
from rest_framework import mixins, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import conf, filtersets
from ..choices import BackupTriggerChoices
from ..engine import catalog, health
from ..jobs import BackupJob, VerifyBackupJob
from ..models import Backup, RestoreEvent
from . import serializers


class BackupViewSet(mixins.DestroyModelMixin, NetBoxReadOnlyModelViewSet):
    """
    Backups in the catalog. Deleting one also deletes it from storage.
    """
    queryset = Backup.objects.all()
    serializer_class = serializers.BackupSerializer
    filterset_class = filtersets.BackupFilterSet


class RestoreEventViewSet(mixins.DestroyModelMixin, NetBoxReadOnlyModelViewSet):
    queryset = RestoreEvent.objects.all()
    serializer_class = serializers.RestoreEventSerializer
    filterset_class = filtersets.RestoreEventFilterSet


class TokenWriteEnabled(BasePermission):
    """
    Unsafe methods authenticated by an API token require the token to be write-enabled.
    """
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS or not hasattr(request.auth, 'write_enabled'):
            return True
        return bool(request.auth.write_enabled)


class _ActionView(APIView):
    permission_classes = [IsAuthenticatedOrLoginNotRequired, TokenWriteEnabled]
    required_permission = 'netbox_backup.run_backup'

    def check_permissions(self, request):
        super().check_permissions(request)
        if not request.user.has_perm(self.required_permission):
            raise PermissionDenied(f"Missing permission {self.required_permission}")


class RunBackupView(_ActionView):
    """
    Queue a backup job.
    """
    @extend_schema(request=serializers.RunBackupSerializer, responses={202: JobSerializer})
    def post(self, request):
        data = serializers.RunBackupSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        job = BackupJob.enqueue(
            user=request.user,
            kind=data.validated_data['kind'],
            trigger=BackupTriggerChoices.TRIGGER_API,
            requested_by=request.user.username,
            verify_level=data.validated_data.get('verify_level'),
        )
        return Response(JobSerializer(job, context={'request': request}).data, status=status.HTTP_202_ACCEPTED)


class VerifyBackupView(_ActionView):
    """
    Queue verification of a backup.
    """
    @extend_schema(request=serializers.VerifyBackupSerializer, responses={202: JobSerializer})
    def post(self, request, pk):
        backup = get_object_or_404(Backup.objects.restrict(request.user, 'view'), pk=pk)
        data = serializers.VerifyBackupSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        job = VerifyBackupJob.enqueue(user=request.user, backup_id=backup.pk, level=data.validated_data['level'])
        return Response(JobSerializer(job, context={'request': request}).data, status=status.HTTP_202_ACCEPTED)


class SyncCatalogView(_ActionView):
    """
    Reconcile the catalog with the manifests in storage.
    """
    @extend_schema(request=None, responses={200: dict})
    def post(self, request):
        return Response(catalog.sync())


class StatusView(APIView):
    """
    Health conditions summarising whether this installation is recoverable.
    """
    permission_classes = [IsAuthenticatedOrLoginNotRequired]

    @extend_schema(responses={200: dict})
    def get(self, request):
        if not request.user.has_perm('netbox_backup.view_backup'):
            raise PermissionDenied("Missing permission netbox_backup.view_backup")
        conds = health.conditions()
        return Response({
            'instance_id': conf.instance_id(),
            'status': health.overall(conds),
            'conditions': [c.as_dict() for c in conds],
        })
