"""
Verification levels. Level 1 (integrity) runs on the production worker with public keys only: sizes and
locally computed checksums against what each backend reports, the manifest signature and _COMPLETE hash, and lock
status. Level 2 (QA restore) runs on a separate QA instance, or on demand with "verify --drill" in the slot.
"""
import logging

from .. import conf
from ..choices import VerifyLevelChoices
from ..core import sets
from ..core.gpg import Keyring
from ..core.manifest import ManifestError
from ..storage import StorageError
from . import catalog

logger = logging.getLogger('netbox_backup')


def verify_set(set_id, log=None):
    """
    Level-1 check of every copy of a set. Returns True if every copy passed.
    """
    from ..models import BackupSet
    log = log or logger
    config = conf.get_config()
    backup_set = BackupSet.objects.get(set_id=set_id)
    copies = {c.target.name: c for c in backup_set.copies.select_related('target')}
    target_names = list(copies) or list(config.targets)
    passed = True
    with Keyring(config.crypto) as keyring:
        for name in target_names:
            target = config.targets.get(name)
            if target is None:
                continue
            backend = catalog.configured_backend(target)
            copy = copies.get(name)
            # A replica is checked against the checksums its copy job recorded, not the primary's
            expected = (copy.copied_objects or {}) if copy is not None and not copy.is_primary else None
            try:
                manifest = sets.load_manifest(backend, backup_set.instance_id, set_id)
                results = sets.level1(backend, manifest, keyring, target=target, expected=expected)
            except (sets.SetError, ManifestError, StorageError) as e:
                results = [{'check': 'manifest', 'ok': False, 'detail': str(e)}]
            ok = catalog.mark_level(backup_set, results, VerifyLevelChoices.INTEGRITY, target=name)
            for r in results:
                (log.info if r['ok'] else log.error)(f"[{'PASS' if r['ok'] else 'FAIL'}] {name}: {r['check']} "
                                                     f"{r['detail']}")
            passed = passed and ok
    from .replicate import update_set_status
    update_set_status(backup_set, config)
    return passed


def verify_policy(policy_name, log=None):
    """
    Level 1 of every usable set of a policy (its verify_schedule). Returns {set_id: passed}.
    """
    from ..choices import SetStatusChoices
    from ..models import BackupSet
    config = conf.get_config()
    set_ids = BackupSet.objects.filter(policy_name=policy_name, instance_id=config.instance_id,
                                       status__in=SetStatusChoices.USABLE).values_list('set_id', flat=True)
    return {set_id: verify_set(set_id, log=log) for set_id in set_ids}
