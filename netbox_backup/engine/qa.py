"""
QA restore instances. A separate NetBox install (own database, media, Redis and its own pinned instance_id)
restores production's newest set on a schedule:

    nbbackup qa run --from-instance <production instance_id> --confirm <QA database>

which is "restore load --in-place" of that set followed by "restore finish --qa": the full restore, the baseline
check, migrate --check and an API smoke test, but no post-restore backup. The outcome, passed or failed, becomes a
report signed with the QA instance's own key (never production's) and written create-only to

    <prefix>/<production instance_id>/reports/<report_id>-<set_id>.json

with a credential that can write only there (qa.report_target). Production imports the reports it can verify
against crypto.report_signers: a pass marks the set as level 2 (QA restore), a failure turns health red.
"""
import json
import logging
import socket

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .. import __version__, conf
from ..choices import SetStatusChoices, VerifyLevelChoices
from ..core import layout
from ..core.gpg import GPGError, Keyring
from ..core.manifest import canonical
from ..storage import ObjectExists, StorageError, get_backend

logger = logging.getLogger('netbox_backup')

FORMAT = 'netbox-backup/qa-report'
SCHEMA = 1


class ReportError(Exception):
    pass


def build_report(config, set_id, source_instance, started, passed, checks, keys_used=None, error='',
                 restore_id=None):
    recovery = config.crypto.recovery_key
    return {
        'report_id': layout.new_ulid(),
        'set_id': set_id,
        'source_instance_id': source_instance,
        'qa_instance_id': config.instance_id,
        'qa_hostname': socket.gethostname(),
        'netbox_version': settings.VERSION,
        'plugin_version': __version__,
        'restore_id': restore_id,
        'started': started.isoformat(),
        'finished': timezone.now().isoformat(),
        'passed': bool(passed),
        'checks': checks,
        'keys_used': keys_used or {},
        'recovery_key_only': bool(recovery and keys_used and all(k == recovery for k in keys_used.values())),
        'error': error[:2000],
    }


def store_report(config, report, target_name):
    """
    Sign the report with this (QA) instance's signing key and write it below the source instance's reports/.
    """
    with Keyring(config.crypto, signing=True) as keyring:
        signature = keyring.sign_detached(canonical(report)).decode('ascii')
    document = {'format': FORMAT, 'schema': SCHEMA, 'report': report, 'signature': signature,
                'signer': config.crypto.signing_key}
    key = layout.report_key(report['source_instance_id'], report['report_id'], report['set_id'])
    name = config.qa.get('report_target') or target_name
    backend = get_backend(config.target(name))
    try:
        backend.put_bytes(key, json.dumps(document, indent=1, sort_keys=True).encode())
    except ObjectExists:
        raise ReportError(f"{key} exists already") from None
    return name, key


def parse_report(raw, keyring, signers):
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise ReportError(f"not JSON: {e}") from e
    if not isinstance(document, dict) or document.get('format') != FORMAT or document.get('schema') != SCHEMA:
        raise ReportError("not a netbox-backup QA report")
    report = document.get('report') or {}
    signer = keyring.verify_detached(canonical(report), document.get('signature', '').encode('ascii'),
                                     signers=signers)
    return report, signer


def import_reports(config=None, log=None):
    """
    Read QA reports below this instance on every target, verify them against crypto.report_signers and record
    the newest per set. Returns the number of sets updated.
    """
    config = config or conf.get_config()
    log = log or logger
    signers = set(config.crypto.report_signers)
    if not signers or config.instance_id is None:
        return 0
    updated = 0
    with Keyring(config.crypto) as keyring:
        for name, target in config.targets.items():
            try:
                backend = get_backend(target)
                infos = sorted(backend.list(f'{config.instance_id}/{layout.REPORTS}/'), key=lambda i: i.key)
            except StorageError as e:
                log.warning(f"QA reports on {name}: {e}")
                continue
            for info in infos:
                try:
                    report, signer = parse_report(backend.get_bytes(info.key), keyring, signers)
                except (ReportError, GPGError, StorageError) as e:
                    log.warning(f"QA report {info.key} on {name} ignored: {e}")
                    continue
                if report.get('source_instance_id') != config.instance_id:
                    log.warning(f"QA report {info.key} describes instance {report.get('source_instance_id')}")
                    continue
                if record_report(report, signer, f'{name}:{info.key}'):
                    updated += 1
    return updated


def record_report(report, signer, location):
    from ..models import BackupSet
    backup_set = BackupSet.objects.filter(set_id=report.get('set_id')).first()
    if backup_set is None:
        return False
    current = backup_set.qa_report or {}
    if current.get('report_id', '') >= report['report_id']:
        return False
    backup_set.qa_report = {
        'report_id': report['report_id'], 'passed': report['passed'], 'finished': report['finished'],
        'qa_instance': report.get('qa_instance_id'), 'qa_hostname': report.get('qa_hostname'),
        'netbox_version': report.get('netbox_version'), 'checks': report.get('checks', []),
        'keys_used': report.get('keys_used', {}), 'error': report.get('error', ''), 'signer': signer,
        'location': location,
    }
    fields = ['qa_report']
    if report['passed']:
        finished = parse_datetime(report['finished'])
        backup_set.qa_passed_at = max(filter(None, [backup_set.qa_passed_at, finished]))
        backup_set.verify_level = max(backup_set.verify_level, VerifyLevelChoices.QA_RESTORE)
        fields += ['qa_passed_at', 'verify_level']
        if report.get('recovery_key_only'):
            backup_set.recovery_drill_at = finished
            fields.append('recovery_drill_at')
        if backup_set.status == SetStatusChoices.UPLOADED:
            backup_set.status = SetStatusChoices.VERIFIED
            fields.append('status')
    backup_set.save(update_fields=fields)
    return True


def api_smoke_test():
    """
    GET a few API endpoints of the restored NetBox in-process, as a superuser that exists only in memory.
    Returns [{'path', 'status'}]; every status must be 200.
    """
    from rest_framework.test import APIClient
    from users.models import User
    user = User(username='netbox-backup-qa', is_superuser=True, is_active=True)
    client = APIClient()
    client.force_authenticate(user=user)
    host = next((h for h in settings.ALLOWED_HOSTS if h not in ('*',) and not h.startswith('.')), 'localhost')
    results = []
    for path in ('/api/status/', '/api/dcim/sites/?limit=1', '/api/users/users/?limit=1',
                 '/api/plugins/backup/sets/?limit=1'):
        try:
            response = client.get(path, SERVER_NAME=host, HTTP_HOST=host)
            results.append({'path': path, 'status': response.status_code})
        except Exception as e:
            results.append({'path': path, 'status': None, 'error': f'{type(e).__name__}: {e}'})
    return results
