from django.utils.translation import gettext_lazy as _
from utilities.choices import ChoiceSet


class ComponentChoices(ChoiceSet):
    DATABASE = 'database'
    MEDIA = 'media'
    SCRIPTS = 'scripts'
    CONFIG = 'config'

    CHOICES = (
        (DATABASE, _('Database')),
        (MEDIA, _('Media')),
        (SCRIPTS, _('Scripts')),
        (CONFIG, _('Configuration')),
    )


class SetStatusChoices(ChoiceSet):
    """
    pending -> running -> uploaded -> verified, with exits to failed, partial (primary ok, a replica failed) and
    expired (pruned; the row is kept 90 days for audit).
    """
    PENDING = 'pending'
    RUNNING = 'running'
    UPLOADED = 'uploaded'
    VERIFIED = 'verified'
    PARTIAL = 'partial'
    FAILED = 'failed'
    EXPIRED = 'expired'

    USABLE = (UPLOADED, VERIFIED, PARTIAL)
    ACTIVE = (PENDING, RUNNING)

    CHOICES = (
        (PENDING, _('Pending'), 'cyan'),
        (RUNNING, _('Running'), 'blue'),
        (UPLOADED, _('Uploaded'), 'teal'),
        (VERIFIED, _('Verified'), 'green'),
        (PARTIAL, _('Partial'), 'yellow'),
        (FAILED, _('Failed'), 'red'),
        (EXPIRED, _('Expired'), 'gray'),
    )


class TriggerChoices(ChoiceSet):
    SCHEDULED = 'scheduled'
    MANUAL = 'manual'
    API = 'api'
    CLI = 'cli'
    PRE_UPGRADE = 'pre-upgrade'
    SAFETY = 'safety'
    POST_RESTORE = 'post-restore'

    CHOICES = (
        (SCHEDULED, _('Scheduled')),
        (MANUAL, _('Manual (UI)')),
        (API, _('REST API')),
        (CLI, _('Command line')),
        (PRE_UPGRADE, _('Pre-upgrade')),
        (SAFETY, _('Restore safety set')),
        (POST_RESTORE, _('After restore')),
    )


class CopyStatusChoices(ChoiceSet):
    PENDING = 'pending'
    UPLOADING = 'uploading'
    UPLOADED = 'uploaded'
    VERIFIED = 'verified'
    FAILED = 'failed'
    DELETED = 'deleted'

    CHOICES = (
        (PENDING, _('Pending'), 'cyan'),
        (UPLOADING, _('Uploading'), 'blue'),
        (UPLOADED, _('Uploaded'), 'teal'),
        (VERIFIED, _('Verified'), 'green'),
        (FAILED, _('Failed'), 'red'),
        (DELETED, _('Deleted'), 'gray'),
    )


class VerifyLevelChoices(ChoiceSet):
    NONE = 0
    INTEGRITY = 1
    QA_RESTORE = 2

    CHOICES = (
        (NONE, _('Not verified'), 'gray'),
        (INTEGRITY, _('Level 1: integrity'), 'green'),
        (QA_RESTORE, _('Level 2: QA restore'), 'purple'),
    )


class RestoreStatusChoices(ChoiceSet):
    PLANNED = 'planned'
    LOADING = 'loading'
    LOADED = 'loaded'
    FINISHING = 'finishing'
    FINISHED = 'finished'
    ROLLED_BACK = 'rolled-back'
    FINALIZED = 'finalized'
    SUPERSEDED = 'superseded'
    FAILED = 'failed'

    CHOICES = (
        (PLANNED, _('Planned'), 'cyan'),
        (LOADING, _('Loading'), 'blue'),
        (LOADED, _('Loaded, awaiting switch'), 'yellow'),
        (FINISHING, _('Finishing'), 'blue'),
        (FINISHED, _('Finished'), 'green'),
        (ROLLED_BACK, _('Rolled back'), 'orange'),
        (FINALIZED, _('Finalized'), 'gray'),
        (SUPERSEDED, _('Superseded by a rerun'), 'gray'),
        (FAILED, _('Failed'), 'red'),
    )


class RestoreStrategyChoices(ChoiceSet):
    SLOT = 'slot'
    IN_PLACE = 'in-place'
    MEDIA = 'media'
    DRILL = 'drill'

    CHOICES = (
        (SLOT, _('Restore slot')),
        (IN_PLACE, _('In place')),
        (MEDIA, _('Media only')),
        (DRILL, _('Drill (QA restore in the slot)')),
    )
