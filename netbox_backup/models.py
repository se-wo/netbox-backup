from django.db import models, transaction
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from netbox.models import NetBoxModel

from .choices import (
    BackupKindChoices,
    BackupStatusChoices,
    BackupTriggerChoices,
    RestoreStatusChoices,
    VerificationStatusChoices,
)

__all__ = ('Backup', 'RestoreEvent')


class Backup(NetBoxModel):
    """
    A catalog entry for one backup. The authoritative copy of a backup's metadata is its manifest.json in storage;
    these rows are a cache of that (rebuilt by "sync"), so the catalog survives a database restore.
    """
    name = models.CharField(verbose_name=_('name'), max_length=100, unique=True)
    instance_id = models.CharField(verbose_name=_('instance'), max_length=64)
    kind = models.CharField(verbose_name=_('kind'), max_length=20, choices=BackupKindChoices)
    status = models.CharField(
        verbose_name=_('status'), max_length=20, choices=BackupStatusChoices,
        default=BackupStatusChoices.STATUS_PENDING,
    )
    trigger = models.CharField(verbose_name=_('trigger'), max_length=20, choices=BackupTriggerChoices)
    requested_by = models.CharField(verbose_name=_('requested by'), max_length=150, blank=True)
    started_at = models.DateTimeField(verbose_name=_('started'), null=True, blank=True)
    completed_at = models.DateTimeField(verbose_name=_('completed'), null=True, blank=True)
    size = models.PositiveBigIntegerField(verbose_name=_('size (bytes)'), null=True, blank=True)
    encrypted = models.BooleanField(verbose_name=_('encrypted'), default=False)
    netbox_version = models.CharField(verbose_name=_('NetBox version'), max_length=50, blank=True)
    location = models.CharField(verbose_name=_('location'), max_length=500, blank=True)
    manifest = models.JSONField(verbose_name=_('manifest'), default=dict, blank=True)
    error = models.TextField(verbose_name=_('error'), blank=True)
    verification_status = models.CharField(
        verbose_name=_('verification'), max_length=20, choices=VerificationStatusChoices,
        default=VerificationStatusChoices.STATUS_UNVERIFIED,
    )
    verification_level = models.CharField(verbose_name=_('verification level'), max_length=20, blank=True)
    verified_at = models.DateTimeField(verbose_name=_('verified'), null=True, blank=True)
    verification_log = models.JSONField(verbose_name=_('verification log'), default=list, blank=True)
    job = models.ForeignKey(
        to='core.Job', on_delete=models.SET_NULL, related_name='+', null=True, blank=True, editable=False,
    )

    clone_fields = ()

    class Meta:
        ordering = ('-created', '-pk')
        verbose_name = _('backup')
        verbose_name_plural = _('backups')

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_backup:backup', args=[self.pk])

    def get_status_color(self):
        return BackupStatusChoices.colors.get(self.status)

    def get_kind_color(self):
        return BackupKindChoices.colors.get(self.kind)

    def get_verification_status_color(self):
        return VerificationStatusChoices.colors.get(self.verification_status)

    def delete(self, *args, delete_from_storage=True, **kwargs):
        """
        Deleting a catalog entry also deletes the backup from storage, once the surrounding transaction commits.
        """
        instance_id, name = self.instance_id, self.name
        result = super().delete(*args, **kwargs)
        if delete_from_storage:
            def _remove():
                from .engine.backup import storage_key
                from .storage import get_storage
                get_storage().delete_prefix(storage_key(instance_id, name))
            transaction.on_commit(_remove)
        return result

    @property
    def is_successful(self):
        return self.status in BackupStatusChoices.SUCCESSFUL

    @property
    def duration(self):
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None

    @property
    def artifacts(self):
        return self.manifest.get('artifacts', [])

    @property
    def row_counts(self):
        return self.manifest.get('database', {}).get('row_counts', {})

    @property
    def total_rows(self):
        return sum(self.row_counts.values())

    @property
    def warnings(self):
        return self.manifest.get('warnings', [])


class RestoreEvent(NetBoxModel):
    """
    A record of a restore. Written into the restored database once the restore finishes, so the history of
    restores is visible on the recovered instance.
    """
    backup_name = models.CharField(verbose_name=_('backup'), max_length=100)
    backup_instance_id = models.CharField(verbose_name=_('backup instance'), max_length=64)
    backup_created = models.DateTimeField(verbose_name=_('backup created'), null=True, blank=True)
    status = models.CharField(verbose_name=_('status'), max_length=20, choices=RestoreStatusChoices)
    started_at = models.DateTimeField(verbose_name=_('started'))
    completed_at = models.DateTimeField(verbose_name=_('completed'), null=True, blank=True)
    restored_by = models.CharField(verbose_name=_('restored by'), max_length=150, blank=True)
    hostname = models.CharField(verbose_name=_('host'), max_length=255, blank=True)
    components = models.JSONField(verbose_name=_('components'), default=list, blank=True)
    netbox_version_backup = models.CharField(verbose_name=_('backup NetBox version'), max_length=50, blank=True)
    netbox_version_running = models.CharField(verbose_name=_('running NetBox version'), max_length=50, blank=True)
    safety_backup_name = models.CharField(verbose_name=_('safety backup'), max_length=100, blank=True)
    row_count_mismatches = models.JSONField(verbose_name=_('row count mismatches'), default=dict, blank=True)
    log = models.TextField(verbose_name=_('log'), blank=True)

    clone_fields = ()

    class Meta:
        ordering = ('-started_at', '-pk')
        verbose_name = _('restore')
        verbose_name_plural = _('restores')

    def __str__(self):
        return f'{self.backup_name} @ {self.started_at:%Y-%m-%d %H:%M}'

    def get_absolute_url(self):
        return reverse('plugins:netbox_backup:restoreevent', args=[self.pk])

    def get_status_color(self):
        return RestoreStatusChoices.colors.get(self.status)
