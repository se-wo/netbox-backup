"""
The plugin's view of the static configuration in PLUGINS_CONFIG['netbox_backup'], plus the NetBox database as a
core.pg.Database.
"""
import os
import tempfile

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .core.config import Config, ConfigError
from .core.pg import LIBPQ_OPTIONS, Database

PLUGIN_NAME = 'netbox_backup'

# Sample secrets published in netbox-docker's env/netbox.env. An install running with them has forgeable sessions
# and tokens, and so does every set taken from it.
SAMPLE_SECRET_KEYS = {
    'r(m)9nLGnz$(_q3N4z1k(EFsMCjjjzx08x9VhNVcfd%6RF#r!6DE@+V5Zk2X',
}
SAMPLE_PEPPERS = {
    'Qy+F=OTeGskWQ(wTMgjc+NPPlz6YwFXY=KHIIg=wpYXT&e(6u8',
}

_cache = {}


def raw():
    return dict(getattr(settings, 'PLUGINS_CONFIG', {}).get(PLUGIN_NAME, {}) or {})


def get_config():
    """
    The parsed Config. Re-parsed whenever PLUGINS_CONFIG changes (tests override it).
    """
    data = raw()
    key = repr(sorted(data.items(), key=lambda kv: kv[0])) + settings.SECRET_KEY
    if _cache.get('key') != key:
        try:
            _cache['config'] = Config.from_dict(data, fallback_instance_seed=settings.SECRET_KEY)
        except ConfigError as e:
            raise ImproperlyConfigured(f"netbox_backup: {e}") from e
        _cache['key'] = key
    return _cache['config']


def instance_id():
    return get_config().instance_id


def database(alias='default', dbname=None):
    """
    The NetBox database (or another database on the same server) as a core.pg.Database.
    """
    db = settings.DATABASES[alias]
    params = {
        'dbname': dbname or db.get('NAME'),
        'user': db.get('USER') or None,
        'host': db.get('HOST') or None,
        'port': db.get('PORT') or None,
        'application_name': 'netbox-backup',
    }
    for key, value in (db.get('OPTIONS') or {}).items():
        if key in LIBPQ_OPTIONS and value is not None:
            params[key] = value
    config = get_config()
    return Database(
        params, password=db.get('PASSWORD') or None, pg_dump=config.pg_dump, pg_restore=config.pg_restore,
        psql=config.psql,
    )


def spool_dir(*parts):
    """
    Local spool for restores: an ephemeral volume. Created 0700.
    """
    base = get_config().spool_dir or os.path.join(tempfile.gettempdir(), 'netbox-backup')
    path = os.path.join(base, *parts)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def startup_problems():
    """
    Configuration mistakes reported by health checks: sample secrets and unparsable configuration.
    """
    problems = []
    if settings.SECRET_KEY in SAMPLE_SECRET_KEYS:
        problems.append("SECRET_KEY is netbox-docker's published sample; generate a new one")
    peppers = getattr(settings, 'API_TOKEN_PEPPERS', None) or {}
    if any(str(p) in SAMPLE_PEPPERS for p in peppers.values()):
        problems.append("API_TOKEN_PEPPERS contains netbox-docker's published sample; generate a new one")
    try:
        get_config()
    except ImproperlyConfigured as e:
        problems.append(str(e))
    return problems
