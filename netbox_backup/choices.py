from django.utils.translation import gettext_lazy as _
from utilities.choices import ChoiceSet


class BackupKindChoices(ChoiceSet):
    KIND_DATABASE = 'database'
    KIND_FULL = 'full'

    CHOICES = (
        (KIND_DATABASE, _('Database'), 'blue'),
        (KIND_FULL, _('Full'), 'purple'),
    )


class BackupStatusChoices(ChoiceSet):
    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_COMPLETED = 'completed'
    STATUS_WARNING = 'warning'
    STATUS_FAILED = 'failed'
    STATUS_MISSING = 'missing'

    SUCCESSFUL = (STATUS_COMPLETED, STATUS_WARNING)

    CHOICES = (
        (STATUS_PENDING, _('Pending'), 'cyan'),
        (STATUS_RUNNING, _('Running'), 'blue'),
        (STATUS_COMPLETED, _('Completed'), 'green'),
        (STATUS_WARNING, _('Completed with warnings'), 'yellow'),
        (STATUS_FAILED, _('Failed'), 'red'),
        (STATUS_MISSING, _('Missing from storage'), 'gray'),
    )


class BackupTriggerChoices(ChoiceSet):
    TRIGGER_SCHEDULED = 'scheduled'
    TRIGGER_MANUAL = 'manual'
    TRIGGER_API = 'api'
    TRIGGER_CLI = 'cli'
    TRIGGER_PRE_RESTORE = 'pre-restore'

    CHOICES = (
        (TRIGGER_SCHEDULED, _('Scheduled')),
        (TRIGGER_MANUAL, _('Manual (UI)')),
        (TRIGGER_API, _('REST API')),
        (TRIGGER_CLI, _('Command line')),
        (TRIGGER_PRE_RESTORE, _('Pre-restore safety backup')),
    )


class VerificationStatusChoices(ChoiceSet):
    STATUS_UNVERIFIED = 'unverified'
    STATUS_PASSED = 'passed'
    STATUS_FAILED = 'failed'

    CHOICES = (
        (STATUS_UNVERIFIED, _('Unverified'), 'gray'),
        (STATUS_PASSED, _('Passed'), 'green'),
        (STATUS_FAILED, _('Failed'), 'red'),
    )


class VerificationLevelChoices(ChoiceSet):
    LEVEL_QUICK = 'quick'
    LEVEL_DEEP = 'deep'
    LEVEL_RESTORE = 'restore'

    CHOICES = (
        (LEVEL_QUICK, _('Quick (storage presence and sizes)')),
        (LEVEL_DEEP, _('Deep (download, decrypt, checksums, archive contents)')),
        (LEVEL_RESTORE, _('Test restore (deep + restore into a scratch database and compare row counts)')),
    )


class RestoreStatusChoices(ChoiceSet):
    STATUS_COMPLETED = 'completed'
    STATUS_WARNING = 'warning'
    STATUS_RECOVERY_REQUIRED = 'recovery-required'
    STATUS_FAILED = 'failed'

    CHOICES = (
        (STATUS_COMPLETED, _('Completed'), 'green'),
        (STATUS_WARNING, _('Completed with warnings'), 'yellow'),
        (STATUS_RECOVERY_REQUIRED, _('Recovery required'), 'orange'),
        (STATUS_FAILED, _('Failed'), 'red'),
    )
