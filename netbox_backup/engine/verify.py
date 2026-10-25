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
from ..storage import StorageError, get_backend
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
    copies = list(backup_set.copies.select_related('target')) or []
    target_names = [c.target.name for c in copies] or list(config.targets)
    passed = True
    with Keyring(config.crypto) as keyring:
        for name in target_names:
            target = config.targets.get(name)
            if target is None:
                continue
            backend = get_backend(target)
            try:
                manifest = sets.load_manifest(backend, backup_set.instance_id, set_id)
                results = sets.level1(backend, manifest, keyring, target=target)
            except (sets.SetError, ManifestError, StorageError) as e:
                results = [{'check': 'manifest', 'ok': False, 'detail': str(e)}]
            ok = catalog.mark_level(backup_set, results, VerifyLevelChoices.INTEGRITY, target=name)
            for r in results:
                (log.info if r['ok'] else log.error)(f"[{'PASS' if r['ok'] else 'FAIL'}] {name}: {r['check']} "
                                                     f"{r['detail']}")
            passed = passed and ok
    return passed
