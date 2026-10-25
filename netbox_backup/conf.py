"""
Typed access to the plugin's configuration (PLUGINS_CONFIG['netbox_backup']).
"""
import hashlib
import os
import re
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

PLUGIN_NAME = 'netbox_backup'

_UNITS = {
    'm': 'minutes', 'min': 'minutes', 'mins': 'minutes', 'minute': 'minutes', 'minutes': 'minutes',
    'h': 'hours', 'hr': 'hours', 'hour': 'hours', 'hours': 'hours',
    'd': 'days', 'day': 'days', 'days': 'days',
    'w': 'weeks', 'week': 'weeks', 'weeks': 'weeks',
    'mo': 'months', 'month': 'months', 'months': 'months',
    'y': 'years', 'year': 'years', 'years': 'years',
}


def get(key, default=None):
    from . import NetBoxBackupConfig
    plugin_config = getattr(settings, 'PLUGINS_CONFIG', {}).get(PLUGIN_NAME, {})
    if key in plugin_config:
        return plugin_config[key]
    return NetBoxBackupConfig.default_settings.get(key, default)


def instance_id():
    """
    Identifier under which this installation's backups are stored. Two installations that share a bucket or
    directory are kept apart by this value, and a restore refuses another instance's backups by default.
    """
    value = get('instance_id')
    if value:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,62}', str(value)):
            raise ImproperlyConfigured(
                "netbox_backup: instance_id may contain only letters, digits, '.', '_' and '-' (max 63 chars)."
            )
        return str(value)
    digest = hashlib.sha256(f'netbox-backup:{settings.SECRET_KEY}'.encode()).hexdigest()
    return f'netbox-{digest[:12]}'


def parse_duration(value):
    """
    Parse a retention or interval value: an int (days), a timedelta, or a string such as "30 days", "2w", "1 month".
    Months are treated as 30 days and years as 365 days.
    """
    if value is None or value == '':
        return None
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(days=value)
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)\s*', str(value))
    if not match:
        raise ImproperlyConfigured(f"netbox_backup: cannot parse duration {value!r} (try e.g. '30 days').")
    number, unit = float(match.group(1)), _UNITS.get(match.group(2).lower())
    if unit is None:
        raise ImproperlyConfigured(f"netbox_backup: unknown duration unit in {value!r}.")
    if unit == 'months':
        return timedelta(days=30 * number)
    if unit == 'years':
        return timedelta(days=365 * number)
    return timedelta(**{unit: number})


def retention():
    return parse_duration(get('retention'))


def interval(kind):
    """
    Schedule interval (minutes) for the given backup kind, or None if scheduling is disabled.
    """
    key = {'database': 'database_backup_interval', 'full': 'full_backup_interval'}[kind]
    value = get(key)
    if not value:
        return None
    if type(value) is not int or value < 1:
        raise ImproperlyConfigured(f"netbox_backup: {key} must be a positive integer (minutes) or None.")
    return value


def stale_after():
    """
    Age after which the newest successful backup is considered stale.
    """
    value = get('stale_after')
    if value:
        return timedelta(minutes=value)
    intervals = [i for i in (interval('database'), interval('full')) if i]
    if not intervals:
        return None
    return timedelta(minutes=2 * min(intervals))


def encryption_key():
    return get('encryption_key') or None


def include_configuration():
    value = get('include_configuration')
    if value == 'auto':
        return bool(encryption_key())
    return bool(value)


def work_dir():
    path = get('work_dir')
    if path:
        os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def storage_config():
    cfg = dict(get('storage') or {})
    cfg.setdefault('backend', 'local')
    if cfg['backend'] == 'local' and not cfg.get('path'):
        cfg['path'] = os.path.join(os.path.dirname(settings.BASE_DIR), 'backups')
    return cfg
