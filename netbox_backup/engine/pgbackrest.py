"""
Read-only integration with pgBackRest, for installations that add WAL archiving and point-in-time recovery on
the PostgreSQL server. The plugin does not configure pgBackRest (that happens on the database host); it reports
whether the stanza's backups and WAL archive are healthy and recent.
"""
import json
import shutil
import subprocess
from datetime import datetime

from django.utils import timezone

from .. import conf


class PgBackRestError(Exception):
    pass


def settings():
    value = conf.get('pgbackrest')
    if not value or not value.get('stanza'):
        return None
    return {'binary': 'pgbackrest', 'max_age': '2 days', **value}


def info(cfg):
    binary = shutil.which(cfg['binary'])
    if not binary:
        raise PgBackRestError(f"pgbackrest binary '{cfg['binary']}' not found on this host")
    args = [binary, f"--stanza={cfg['stanza']}", '--output=json', 'info']
    if cfg.get('config'):
        args.insert(1, f"--config={cfg['config']}")
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise PgBackRestError(result.stderr.strip() or f'exit status {result.returncode}')
    data = json.loads(result.stdout or '[]')
    if not data:
        raise PgBackRestError(f"stanza '{cfg['stanza']}' not found")
    return data[0]


def summarize(stanza_info, max_age, now=None):
    """
    Return (status, message) for a stanza entry from `pgbackrest info --output=json`.
    """
    now = now or timezone.now()
    status = stanza_info.get('status', {})
    if status.get('code', 0) != 0:
        return 'error', f"pgBackRest stanza '{stanza_info.get('name')}': {status.get('message', 'error')}"
    backups = stanza_info.get('backup', [])
    if not backups:
        return 'error', f"pgBackRest stanza '{stanza_info.get('name')}' has no backups"
    last = max(backups, key=lambda b: b['timestamp']['stop'])
    stopped = datetime.fromtimestamp(last['timestamp']['stop'], tz=timezone.get_current_timezone())
    archives = [a for a in stanza_info.get('archive', []) if a.get('max')]
    wal = f", WAL archived up to {archives[-1]['max']}" if archives else ', no WAL archive'
    message = f"Last pgBackRest {last['type']} backup {last['label']} finished {stopped:%Y-%m-%d %H:%M}{wal}"
    if not archives:
        return 'warning', message + ' (point-in-time recovery unavailable)'
    if max_age and now - stopped > max_age:
        return 'warning', message + ' (older than expected)'
    return 'ok', message


def condition():
    from .health import Condition
    cfg = settings()
    if cfg is None:
        return None
    try:
        status, message = summarize(info(cfg), conf.parse_duration(cfg.get('max_age')))
    except (PgBackRestError, subprocess.TimeoutExpired, ValueError, KeyError) as e:
        status, message = 'error', f"pgBackRest check failed: {e}"
    return Condition('PointInTimeRecovery', status, message)
