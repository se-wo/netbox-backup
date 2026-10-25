"""
Five models. Targets and policies are read-only mirrors of static config; sets, copies and restore runs are a
catalog of what storage holds. Storage is the source of truth: "nbbackup catalog sync" rebuilds the catalog from
manifests.
"""
from datetime import timedelta

from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from netbox.models import NetBoxModel
from netbox.models.features import JobsMixin
from utilities.querysets import RestrictedQuerySet

from .choices import (
    CopyStatusChoices,
    RestoreStatusChoices,
    RestoreStrategyChoices,
    SetStatusChoices,
    TriggerChoices,
    VerifyLevelChoices,
)

__all__ = (
    'BackupCopy', 'BackupPolicy', 'BackupSet', 'InstanceIdentity', 'RestoreRun', 'StorageTarget', 'register_actions',
)


class InstanceIdentity(models.Model):
    """
    The random instance_id an install without a pinned one chose on its first backup, with the host details
    recorded beside it in <instance_id>/instance.json.
    """
    instance_id = models.CharField(verbose_name=_('instance'), max_length=36, unique=True)
    hostname = models.CharField(verbose_name=_('hostname'), max_length=255, blank=True)
    allowed_hosts = models.JSONField(verbose_name=_('allowed hosts'), default=list, blank=True)
    created = models.DateTimeField(verbose_name=_('created'), auto_now_add=True)

    class Meta:
        verbose_name = _('instance identity')
        verbose_name_plural = _('instance identities')

    def __str__(self):
        return self.instance_id


class StorageTarget(NetBoxModel):
    name = models.CharField(verbose_name=_('name'), max_length=64, unique=True)
    backend = models.CharField(verbose_name=_('backend'), max_length=32)
    config = models.JSONField(verbose_name=_('configuration'), default=dict, blank=True)
    secret_refs = models.JSONField(verbose_name=_('secret references'), default=dict, blank=True)
    prefix = models.CharField(verbose_name=_('prefix'), max_length=255, blank=True)
    location = models.CharField(verbose_name=_('location'), max_length=500, blank=True)
    retention_days = models.PositiveIntegerField(verbose_name=_('retention (days)'), default=0)
    quota_bytes = models.PositiveBigIntegerField(verbose_name=_('quota (bytes)'), null=True, blank=True)
    offsite = models.BooleanField(verbose_name=_('off-site'), default=False)
    is_static = models.BooleanField(verbose_name=_('static'), default=True)
    enabled = models.BooleanField(verbose_name=_('enabled'), default=True)
    last_probe = models.JSONField(verbose_name=_('last probe'), null=True, blank=True)
    last_probe_at = models.DateTimeField(verbose_name=_('probed'), null=True, blank=True)

    clone_fields = ()

    class Meta:
        ordering = ('name',)
        verbose_name = _('storage target')
        verbose_name_plural = _('storage targets')

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_backup:storagetarget', args=[self.pk])

    @property
    def probe_ok(self):
        return bool(self.last_probe and self.last_probe.get('ok'))


class BackupPolicy(JobsMixin, NetBoxModel):
    name = models.CharField(verbose_name=_('name'), max_length=64, unique=True)
    components = models.JSONField(verbose_name=_('components'), default=list)
    schedule = models.CharField(verbose_name=_('schedule'), max_length=100, blank=True)
    time_zone = models.CharField(verbose_name=_('time zone'), max_length=64, blank=True)
    primary_target = models.ForeignKey(
        to=StorageTarget, on_delete=models.PROTECT, related_name='policies', verbose_name=_('primary target'),
    )
    replica_targets = models.ManyToManyField(
        to=StorageTarget, related_name='replica_policies', blank=True, verbose_name=_('replica targets'),
    )
    retention = models.JSONField(verbose_name=_('retention'), default=dict, blank=True)
    recipients = models.JSONField(verbose_name=_('recipients'), default=list, blank=True)
    zstd_level = models.PositiveSmallIntegerField(verbose_name=_('zstd level'), default=3)
    verify_schedule = models.CharField(verbose_name=_('verify schedule'), max_length=100, blank=True)
    max_runtime = models.DurationField(verbose_name=_('max runtime'), default=timedelta(hours=4))
    enabled = models.BooleanField(verbose_name=_('enabled'), default=True)
    is_static = models.BooleanField(verbose_name=_('static'), default=True)
    # The last schedule slot the scheduler enqueued; a missed run is caught up once, not once per missed slot
    last_scheduled = models.DateTimeField(verbose_name=_('last scheduled'), null=True, blank=True, editable=False)

    clone_fields = ()

    class Meta:
        ordering = ('name',)
        verbose_name = _('backup policy')
        verbose_name_plural = _('backup policies')

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_backup:backuppolicy', args=[self.pk])


class BackupSet(models.Model):
    set_id = models.CharField(verbose_name=_('set ID'), max_length=26, unique=True)
    instance_id = models.CharField(verbose_name=_('instance'), max_length=36, db_index=True)
    policy = models.ForeignKey(
        to=BackupPolicy, on_delete=models.SET_NULL, related_name='sets', null=True, blank=True,
        verbose_name=_('policy'),
    )
    policy_name = models.CharField(verbose_name=_('policy name'), max_length=64, blank=True)
    status = models.CharField(
        verbose_name=_('status'), max_length=20, choices=SetStatusChoices, default=SetStatusChoices.PENDING,
    )
    trigger = models.CharField(verbose_name=_('trigger'), max_length=20, choices=TriggerChoices)
    created_by = models.CharField(verbose_name=_('created by'), max_length=150, blank=True)
    started = models.DateTimeField(verbose_name=_('started'), null=True, blank=True)
    finished = models.DateTimeField(verbose_name=_('finished'), null=True, blank=True)
    netbox_version = models.CharField(verbose_name=_('NetBox version'), max_length=50, blank=True)
    size_bytes = models.PositiveBigIntegerField(verbose_name=_('size (bytes)'), null=True, blank=True)
    manifest_sha384 = models.CharField(verbose_name=_('manifest SHA-384'), max_length=96, blank=True)
    components = models.JSONField(verbose_name=_('components'), default=dict, blank=True)
    baseline = models.JSONField(verbose_name=_('baseline'), default=dict, blank=True)
    warnings = models.JSONField(verbose_name=_('warnings'), default=list, blank=True)
    error = models.TextField(verbose_name=_('error'), blank=True)
    verify_level = models.PositiveSmallIntegerField(
        verbose_name=_('verified level'), choices=VerifyLevelChoices, default=VerifyLevelChoices.NONE,
    )
    verified_at = models.DateTimeField(verbose_name=_('verified'), null=True, blank=True)
    qa_passed_at = models.DateTimeField(verbose_name=_('QA restore passed'), null=True, blank=True)
    recovery_drill_at = models.DateTimeField(
        verbose_name=_('recovery-key drill passed'), null=True, blank=True,
        help_text=_('A drill that decrypted the set with the offline recovery key'),
    )
    expired_at = models.DateTimeField(verbose_name=_('expired'), null=True, blank=True)
    job = models.ForeignKey(
        to='core.Job', on_delete=models.SET_NULL, related_name='+', null=True, blank=True, editable=False,
    )

    objects = RestrictedQuerySet.as_manager()

    class Meta:
        ordering = ('-set_id',)
        verbose_name = _('backup set')
        verbose_name_plural = _('backup sets')

    def __str__(self):
        return self.set_id

    def get_absolute_url(self):
        return reverse('plugins:netbox_backup:backupset', args=[self.pk])

    def get_status_color(self):
        return SetStatusChoices.colors.get(self.status)

    def get_verify_level_color(self):
        return VerifyLevelChoices.colors.get(self.verify_level)

    @property
    def duration(self):
        if self.started and self.finished:
            return self.finished - self.started
        return None

    @property
    def is_usable(self):
        return self.status in SetStatusChoices.USABLE

    @property
    def media_missing(self):
        return self.components.get('media', {}).get('media_missing', [])


class BackupCopy(models.Model):
    set = models.ForeignKey(to=BackupSet, on_delete=models.CASCADE, related_name='copies', verbose_name=_('set'))
    target = models.ForeignKey(
        to=StorageTarget, on_delete=models.PROTECT, related_name='copies', verbose_name=_('target'),
    )
    status = models.CharField(
        verbose_name=_('status'), max_length=20, choices=CopyStatusChoices, default=CopyStatusChoices.PENDING,
    )
    is_primary = models.BooleanField(verbose_name=_('primary'), default=False)
    uploaded = models.DateTimeField(verbose_name=_('uploaded'), null=True, blank=True)
    verified = models.DateTimeField(verbose_name=_('verified'), null=True, blank=True)
    verify_level = models.PositiveSmallIntegerField(
        verbose_name=_('verified level'), choices=VerifyLevelChoices, default=VerifyLevelChoices.NONE,
    )
    verify_detail = models.JSONField(verbose_name=_('verification'), default=list, blank=True)
    checksum_mode = models.CharField(verbose_name=_('checksum'), max_length=20, blank=True)
    locked_until = models.DateTimeField(verbose_name=_('locked until'), null=True, blank=True)
    error = models.TextField(verbose_name=_('error'), blank=True)

    objects = RestrictedQuerySet.as_manager()

    class Meta:
        ordering = ('set', '-is_primary', 'target')
        constraints = (
            models.UniqueConstraint(fields=('set', 'target'), name='netbox_backup_backupcopy_unique_set_target'),
        )
        verbose_name = _('backup copy')
        verbose_name_plural = _('backup copies')

    def __str__(self):
        return f'{self.set} @ {self.target}'

    def get_absolute_url(self):
        return self.set.get_absolute_url()

    def get_status_color(self):
        return CopyStatusChoices.colors.get(self.status)


class RestoreRun(models.Model):
    """
    A restore's journal. Written by the CLI to a local file and to storage after every step, and mirrored here when
    a database is available; restore runs survive the restore that replaces the database.
    """
    restore_id = models.CharField(verbose_name=_('restore ID'), max_length=26, unique=True)
    set_id = models.CharField(verbose_name=_('set'), max_length=26)
    source_instance_id = models.CharField(verbose_name=_('source instance'), max_length=36, blank=True)
    components = models.JSONField(verbose_name=_('components'), default=list, blank=True)
    strategy = models.CharField(verbose_name=_('strategy'), max_length=20, choices=RestoreStrategyChoices)
    status = models.CharField(verbose_name=_('status'), max_length=20, choices=RestoreStatusChoices)
    steps = models.JSONField(verbose_name=_('steps'), default=list, blank=True)
    plan = models.JSONField(verbose_name=_('plan'), default=dict, blank=True)
    slot_database = models.CharField(verbose_name=_('restored into'), max_length=63, blank=True)
    previous_database = models.CharField(verbose_name=_('previous database'), max_length=63, blank=True)
    safety_set_id = models.CharField(verbose_name=_('safety set'), max_length=26, blank=True)
    rollback_until = models.DateTimeField(verbose_name=_('rollback until'), null=True, blank=True)
    user = models.CharField(verbose_name=_('operator'), max_length=150, blank=True)
    started = models.DateTimeField(verbose_name=_('started'))
    finished = models.DateTimeField(verbose_name=_('finished'), null=True, blank=True)

    objects = RestrictedQuerySet.as_manager()

    class Meta:
        ordering = ('-restore_id',)
        verbose_name = _('restore run')
        verbose_name_plural = _('restore runs')

    def __str__(self):
        return self.restore_id

    def get_absolute_url(self):
        return reverse('plugins:netbox_backup:restorerun', args=[self.pk])

    def get_status_color(self):
        return RestoreStatusChoices.colors.get(self.status)


def register_actions():
    """
    Custom permission actions (NetBox 4.6 model-action API): run_backuppolicy and verify_backupset. There is no
    restore permission: restores need infrastructure access, not a NetBox login.
    """
    from utilities.permissions import ModelAction, register_model_actions
    register_model_actions(BackupPolicy, [ModelAction('run', help_text=_('Start a manual backup'))])
    register_model_actions(BackupSet, [ModelAction('verify', help_text=_('Run a level-1 integrity check'))])
