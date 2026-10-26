"""
Checks that run long before a restore needs them. Ownership: a schema or extension in the live database or the
restore slot that the NetBox role doesn't own (e.g. an ltree created by the server admin) makes every restore abort
at preflight, so slot init, target probe and a daily scheduler run look for it and health alerts on it.
"""
from django.core.cache import cache
from django.utils import timezone

from .. import conf
from ..core import pg as pgcore

OWNERSHIP_CACHE_KEY = 'netbox_backup:ownership'


def ownership_problems(config=None):
    """
    [problem, ...] for the live database and, when configured, the restore slot.
    """
    config = config or conf.get_config()
    from .restore import live_database_name, slot_for
    live = live_database_name()
    names = [live] + [d for d in [slot_for(config, live)] if d]
    problems = []
    for name in names:
        try:
            with conf.database(dbname=name).connect() as conn, conn.cursor() as cursor:
                foreign = pgcore.foreign_owned(cursor)
        except Exception as e:
            problems.append(f"{name}: cannot be inspected ({e})")
            continue
        if foreign['extensions']:
            problems.append(f"{name}: extension(s) {', '.join(foreign['extensions'])} are owned by another role; "
                            f"drop them and let the NetBox role create them, or every restore aborts")
        if foreign['schemas']:
            problems.append(f"{name}: schema(s) {', '.join(foreign['schemas'])} are owned by another role")
    return problems


def check_ownership(config=None):
    result = {'checked': timezone.now().isoformat(), 'problems': ownership_problems(config)}
    cache.set(OWNERSHIP_CACHE_KEY, result, None)
    return result


def last_ownership_check():
    return cache.get(OWNERSHIP_CACHE_KEY)
