"""
Restores. CLI only, from an ops shell or a one-shot container; the web UI shows plans and journals but never
restores.

A restore loads a set into a second, empty NetBox database (the restore slot), then NetBox is switched to it by
configuration. The live database is never modified, so a failed restore costs nothing and rollback means switching
back. Without a second database, --in-place takes a verified safety set first, then empties and reloads the live
database. The plugin does no schema surgery: it empties a database and loads a dump into it, exactly NetBox's own
documented procedure, with a safety net.

    plan      preflight only: recommends slot or in-place, says why
    load      fetch, verify, decrypt; empty the target; pg_restore; compare the baseline
    finish    after the switch: move the slot marker, migrate, reset jobs/queues/cache/sessions, write media
    rollback  switch back (slot) or reload the safety set (in-place)
    finalize  empty the old database after rollback_days
"""
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import timedelta

import psycopg
from django.conf import settings
from django.db import connection
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from packaging.version import InvalidVersion, Version

from .. import conf
from ..choices import RestoreStrategyChoices, SetStatusChoices, TriggerChoices, VerifyLevelChoices
from ..core import layout, sets
from ..core import manifest as manifest_mod
from ..core import pg as pgcore
from ..core import secrets as secrets_mod
from ..core.gpg import GPGError, Keyring
from ..storage import StorageError, get_backend
from . import journal as journal_mod
from . import media as media_mod

logger = logging.getLogger('netbox_backup')

ABORT, WARN, OK = 'abort', 'warn', 'ok'

# Dumps taken on NetBox 4.7.0 lose the triggers of the ltree-based models
BROKEN_DUMP_VERSIONS = {'4.7.0'}

# Historical migration labels that are not apps (social-auth-app-django < 6)
LEGACY_MIGRATION_LABELS = {'default', 'social_auth'}

FILE_COMPONENTS = ('media', 'scripts')


class RestoreError(Exception):
    pass


@dataclass
class Plan:
    set_id: str
    source_instance_id: str
    target: str
    live_database: str
    slot_database: str | None
    strategy: str
    reason: str
    components: list
    size_bytes: int
    checks: list = field(default_factory=list)
    header: dict = field(default_factory=dict)
    body: dict | None = None
    run_migrations: bool = False
    data_loss: str = ''

    def check(self, name, severity, detail):
        self.checks.append({'check': name, 'result': severity, 'detail': detail})

    @property
    def aborts(self):
        return [c for c in self.checks if c['result'] == ABORT]

    @property
    def ok(self):
        return not self.aborts

    def as_dict(self):
        return {
            'set_id': self.set_id, 'source_instance_id': self.source_instance_id, 'target': self.target,
            'live_database': self.live_database, 'slot_database': self.slot_database, 'strategy': self.strategy,
            'reason': self.reason, 'components': self.components, 'size_bytes': self.size_bytes,
            'checks': self.checks, 'run_migrations': self.run_migrations, 'data_loss': self.data_loss,
            'netbox_version': self.header.get('netbox_version'), 'created_at': self.header.get('created_at'),
        }


def _version(value):
    try:
        return Version(str(value).split('-')[0])
    except (InvalidVersion, TypeError):
        return None


def live_database_name():
    return settings.DATABASES['default']['NAME']


def slot_for(config, live):
    if len(config.databases) == 2 and live in config.databases:
        return next(d for d in config.databases if d != live)
    return None


def locate(config, set_ref, target=None, from_instance=None):
    """
    Find a complete set on the given target, or on the first target that holds it.
    """
    instance_id = from_instance or config.instance_id
    if instance_id is None:
        raise RestoreError("instance_id is not pinned and this database records none: pin instance_id in "
                           "PLUGINS_CONFIG, or pass --from-instance (list instances with: nbbackup list "
                           "--all-instances)")
    names = [target] if target else list(config.targets)
    errors = []
    for name in names:
        try:
            backend = get_backend(config.target(name))
            listing = sets.resolve(backend, instance_id, set_ref)
            return name, backend, sets.load_manifest(backend, instance_id, listing.set_id)
        except (sets.SetError, StorageError, manifest_mod.ManifestError) as e:
            errors.append(f'{name}: {e}')
    raise RestoreError('Set not found: ' + '; '.join(errors))


def migration_state(applied):
    """
    (unknown, pending, known apps): migrations in the set this code lacks, and migrations this code has that the
    set lacks. Squashed migrations count as applied when everything they replace is applied.
    """
    from django.db.migrations.loader import MigrationLoader
    loader = MigrationLoader(None, ignore_no_migrations=True)
    known, replaces = set(), {}
    for (app, name), migration in loader.disk_migrations.items():
        known.add(f'{app}.{name}')
        if migration.replaces:
            replaced = {f'{a}.{n}' for a, n in migration.replaces}
            replaces[f'{app}.{name}'] = replaced
            known |= replaced
    applied = set(applied)
    unknown = {m for m in applied - known if m.split('.', 1)[0] not in LEGACY_MIGRATION_LABELS}
    pending = {f'{a}.{n}' for a, n in loader.graph.nodes
               if f'{a}.{n}' not in applied and not (f'{a}.{n}' in replaces and replaces[f'{a}.{n}'] <= applied)}
    return unknown, pending, {k.split('.', 1)[0] for k in known}


UNFINISHED = ('planned', 'loading', 'loaded', 'finishing', 'failed')


def unfinished_journals(database):
    """
    Restores that loaded (or started loading) into this database and were neither finished, rolled back nor
    superseded. Newest first.
    """
    return [j for j in journal_mod.list_journals()
            if j['status'] in UNFINISHED and j.get('slot_database') == database
            and j['strategy'] in (RestoreStrategyChoices.IN_PLACE, RestoreStrategyChoices.SLOT)]


def dr_state(config, live=None):
    """
    Whether the live database counts as empty, which allows an in-place load without --confirm and without a
    safety set (disaster-recovery mode). Empty means all of: no relations in any non-system schema, no extension but
    plpgsql, no slot marker on it, no unfinished restore journal for it, and the other configured database (if a
    slot pair is configured) empty as well. Returns (empty, reasons it isn't, {database: newest change}).
    """
    live = live or live_database_name()
    reasons, newest = [], {}
    for name in [live] + [d for d in config.databases if d != live]:
        try:
            with conf.database(dbname=name).connect() as conn, conn.cursor() as cursor:
                if not pgcore.is_empty(conn):
                    reasons.append(f"{name} holds relations or extensions")
                    newest[name] = pgcore.newest_change(cursor)
                else:
                    newest[name] = None
                if name == live and pgcore.has_slot_marker(conn, name):
                    reasons.append(f"{name} carries the slot marker: it is a restore slot, not a live database")
        except Exception as e:
            reasons.append(f"{name} cannot be inspected ({e})")
            newest[name] = None
    for j in unfinished_journals(live):
        reasons.append(f"restore {j.restore_id} into {live} is unfinished ({j['status']})")
    return not reasons, reasons, newest


def _live_state(db):
    """
    (is empty, newest last_updated, owned relations, lock capacity) of a database; None values when unreachable.
    """
    try:
        with db.connect() as conn, conn.cursor() as cursor:
            empty = pgcore.is_empty(conn)
            newest = None
            if not empty:
                newest = pgcore.baseline(cursor)['newest_last_updated']
            owned, capacity = pgcore.lock_capacity(cursor)
            return empty, newest, owned, capacity
    except Exception:
        return None, None, None, None


def _foreign_lock_holders(db):
    try:
        with db.connect() as conn, conn.cursor() as cursor:
            return pgcore.foreign_lock_holders(cursor)
    except Exception:
        return []


def build_plan(set_ref='latest', target=None, from_instance=None, components=None, headroom_confirmed=False,
               keyring=None):
    """
    Preflight only; changes nothing. Needs a decryption identity (crypto.identities) for the manifest body.
    """
    config = conf.get_config()
    target_name, backend, manifest = locate(config, set_ref, target=target, from_instance=from_instance)
    header = manifest.header
    available = [layout.COMPONENT_BY_OBJECT[n] for n in header['objects'] if n in layout.COMPONENT_BY_OBJECT]
    wanted = list(components or [c for c in ('database', 'media', 'scripts') if c in available])
    live = live_database_name()
    slot = slot_for(config, live)
    plan = Plan(
        set_id=manifest.set_id, source_instance_id=manifest.instance_id, target=target_name, live_database=live,
        slot_database=slot, strategy=RestoreStrategyChoices.IN_PLACE, reason='', components=wanted,
        size_bytes=sum(o['size'] for o in header['objects'].values()), header=header,
    )
    for component in wanted:
        if component not in available:
            plan.check('Components', ABORT, f"Set has no {component} component (has: {', '.join(available)})")

    own_keyring = keyring is None
    try:
        keyring = keyring or Keyring(config.crypto, identities=True)
    except GPGError as e:
        plan.check('Key', ABORT, str(e))
        return plan
    try:
        _plan_crypto(plan, backend, manifest, keyring, config)
        if plan.body is not None:
            _plan_compatibility(plan, config)
        _plan_strategy(plan, config, headroom_confirmed)
    finally:
        if own_keyring:
            keyring.close()
    return plan


def _plan_crypto(plan, backend, manifest, keyring, config):
    try:
        manifest_mod.verify(manifest, keyring)
        problems = [r for r in sets.level1(backend, manifest, keyring) if not r['ok']]
        if problems:
            plan.check('Signature and hashes', ABORT, '; '.join(f"{r['check']}: {r['detail']}" for r in problems))
        else:
            plan.check('Signature and hashes', OK, 'signature valid, sizes match; SHA-384 is checked while fetching')
    except (GPGError, manifest_mod.ManifestError) as e:
        plan.check('Signature and hashes', ABORT, str(e))
        return
    recipients = set(manifest.header.get('recipients', []))
    identities = {fpr for fpr, key in keyring.keys.items() if key['secret']}
    if not identities & recipients:
        plan.check('Key', ABORT, f"No mounted identity matches a recipient: {', '.join(sorted(recipients))}")
        return
    plan.check('Key', OK, f"identity {sorted(identities & recipients)[0]}")
    try:
        plan.body = manifest_mod.decrypt_body(manifest, keyring)
    except (GPGError, manifest_mod.ManifestError) as e:
        plan.check('Key', ABORT, f"Cannot decrypt the manifest body: {e}")
    if plan.source_instance_id != config.instance_id:
        # Only reachable with --from-instance: locate() looks below that instance alone
        plan.check('Instance', WARN, f"Set belongs to instance {plan.source_instance_id}; this install is "
                                     f"{config.instance_id} (--from-instance)")


def _plan_compatibility(plan, config):
    body = plan.body
    versions = body.get('versions', {})
    backup_version, running = _version(versions.get('netbox')), _version(settings.VERSION)
    if versions.get('netbox') in BROKEN_DUMP_VERSIONS:
        plan.check('NetBox version', ABORT, f"Sets from NetBox {versions['netbox']} lose ltree triggers; "
                                             f"use a set from another release")
    elif backup_version and running and backup_version > running:
        plan.check('NetBox version', ABORT, f"Set is from NetBox {backup_version}, newer than {running}; upgrade "
                                             f"NetBox first")
    else:
        plan.check('NetBox version', OK, f"set {backup_version}, running {running}")

    unknown, pending, known_apps = migration_state(body.get('migrations', []))
    missing_apps = sorted({m.split('.', 1)[0] for m in unknown} - known_apps)
    newer = sorted(m for m in unknown if m.split('.', 1)[0] in known_apps)
    if missing_apps:
        plugins = versions.get('plugins', {})
        listed = ', '.join(f"{app} ({next((v for p, v in plugins.items() if p.endswith(app)), '?')})"
                           for app in missing_apps)
        plan.check('Plugins', ABORT, f"Set has migrations for apps not installed here: {listed}")
    elif newer:
        plan.check('Plugins', ABORT, f"{len(newer)} migration(s) unknown to this code, e.g. {newer[0]}; install "
                                     f"the same or newer versions")
    else:
        plan.check('Plugins', OK, 'every app with migrations in the set is installed')
    plan.run_migrations = bool(pending)
    if pending:
        plan.check('Migrations', WARN, f"{len(pending)} migration(s) will be applied by finish")

    db = conf.database()
    try:
        with db.connect() as conn:
            server = pgcore.server_major(conn)
        restore_major = db.client_major('pg_restore')
        source_server = versions.get('postgresql_server_major')
        dump_major = versions.get('pg_dump_major')
        if source_server and server < source_server:
            plan.check('PostgreSQL', ABORT, f"Server {server} is older than the set's server {source_server}")
        elif dump_major and restore_major and restore_major < dump_major:
            plan.check('PostgreSQL', ABORT, f"pg_restore {restore_major} cannot read a pg_dump {dump_major} archive")
        else:
            plan.check('PostgreSQL', OK, f"server {server}, pg_restore {restore_major}")
    except (pgcore.PostgresError, Exception) as e:
        plan.check('PostgreSQL', ABORT, str(e))

    peppers = set(body.get('token_peppers', []))
    configured = set(getattr(settings, 'API_TOKEN_PEPPERS', {}) or {})
    if peppers - configured:
        plan.check('Peppers', WARN, f"Restored v2 tokens use pepper ID(s) {sorted(peppers - configured)} missing "
                                    f"from API_TOKEN_PEPPERS; those tokens stop validating")


def _plan_strategy(plan, config, headroom_confirmed):
    live_db = conf.database()
    _, newest, owned, capacity = _live_state(live_db)
    empty, not_empty, _ = dr_state(config, plan.live_database)
    rerun = next(iter(unfinished_journals(plan.live_database)), None)
    if rerun is not None and rerun['strategy'] == RestoreStrategyChoices.IN_PLACE:
        plan.check('Journal', WARN, f"restore {rerun.restore_id} into {plan.live_database} is unfinished "
                                    f"({rerun['status']}); an in-place load reuses its safety set "
                                    f"{rerun.get('safety_set_id') or '(it had none; none will be taken)'}")
    set_newest = (plan.body or {}).get('baseline', {}).get('newest_last_updated')
    if newest and set_newest:
        delta = parse_datetime(newest) - parse_datetime(set_newest)
        plan.data_loss = (f"changes made after {set_newest} ({_human(delta)} of activity) will be lost"
                          if delta.total_seconds() > 0 else 'no changes since the set')

    slot_usable, slot_reason = False, 'no restore slot configured ("databases")'
    if plan.slot_database:
        slot_usable, slot_reason = _check_slot(plan, config, report=False)
    if empty:
        plan.strategy = RestoreStrategyChoices.IN_PLACE
        plan.reason = 'the live database is empty (disaster-recovery mode): nothing to protect'
    elif rerun is not None and rerun['strategy'] == RestoreStrategyChoices.IN_PLACE:
        plan.strategy = RestoreStrategyChoices.IN_PLACE
        plan.reason = f"restore {rerun.restore_id} left {plan.live_database} unfinished; rerun it in place"
    elif slot_usable:
        plan.strategy = RestoreStrategyChoices.SLOT
        plan.reason = f"restore slot {plan.slot_database} is ready; the live database stays untouched"
    else:
        plan.strategy = RestoreStrategyChoices.IN_PLACE
        plan.reason = f"{slot_reason}; in-place takes a verified safety set first"

    if plan.strategy == RestoreStrategyChoices.SLOT:
        _check_slot(plan, config, report=True)
        target_db = conf.database(dbname=plan.slot_database)
    else:
        target_db = live_db
    _check_ownership(plan, target_db)
    if owned is not None and capacity:
        relations = owned + len((plan.body or {}).get('baseline', {}).get('tables', {})) * 3
        blockers = _foreign_lock_holders(target_db)
        if blockers:
            plan.check('Locks', ABORT, f"role(s) {', '.join(blockers)} hold locks on {target_db.name}'s relations; "
                                       f"emptying it would wait for them (ask them to disconnect)")
        else:
            plan.check('Locks', ABORT if relations > capacity / 2 else OK,
                       f"~{relations} relations against a lock capacity of {capacity}")
    _check_redis(plan)
    _check_space(plan, headroom_confirmed)
    try:
        from netbox.config import get_config as netbox_config
        maintenance = bool(netbox_config().MAINTENANCE_MODE)
    except Exception:
        maintenance = None
    if not maintenance and not empty:
        plan.check('Maintenance mode', WARN, 'off: writes made between load and the switch are lost by design')


def _check_slot(plan, config, report):
    slot = plan.slot_database
    try:
        with conf.database().connect() as conn:
            comment, owner = pgcore.database_comment(conn, slot)
            sessions = pgcore.other_sessions(conn, slot)
            cur_user = conn.info.user
    except Exception as e:
        reason = f"slot {slot} is not reachable: {e}"
        if report:
            plan.check('Slot', ABORT, reason)
        return False, reason
    problems = []
    if (comment or '').strip() != pgcore.SLOT_MARKER:
        problems.append(f"missing {pgcore.SLOT_MARKER} marker (run: nbbackup slot init {slot})")
    if owner != cur_user:
        problems.append(f"owned by {owner}, not {cur_user} (ALTER DATABASE {slot} OWNER TO {cur_user})")
    if sessions:
        problems.append(f"{len(sessions)} other session(s) connected")
    for j in journal_mod.list_journals():
        if j['status'] in ('finished',) and j.get('previous_database') == slot:
            problems.append(f"holds the rollback copy of restore {j.restore_id} (run: nbbackup restore finalize)")
            break
    reason = f"slot {slot}: " + '; '.join(problems) if problems else ''
    if report:
        plan.check('Slot', ABORT if problems else OK, reason or f"{slot} is marked, owned and idle")
    return not problems, reason


def _check_ownership(plan, db):
    try:
        with db.connect() as conn, conn.cursor() as cursor:
            foreign = pgcore.foreign_owned(cursor)
    except Exception as e:
        plan.check('Ownership', ABORT, f"cannot inspect {db.name}: {e}")
        return
    archive_schemas = set((plan.body or {}).get('database', {}).get('schemas', {}))
    clash = sorted(set(foreign['schemas']) & archive_schemas)
    problems = []
    if clash:
        problems.append(f"schema(s) {', '.join(clash)} exist in {db.name} but are not owned by the NetBox role")
    if foreign['extensions']:
        problems.append(f"extension(s) {', '.join(foreign['extensions'])} are owned by another role and would "
                        f"survive emptying")
    plan.check('Ownership', ABORT if problems else OK, '; '.join(problems) or 'every schema and extension is ours')


def _check_redis(plan):
    tasks = settings.REDIS.get('tasks', {})
    caching = settings.REDIS.get('caching', {})
    same = all(tasks.get(k) == caching.get(k) for k in ('HOST', 'PORT', 'DATABASE'))
    if same:
        plan.check('Redis', WARN, "tasks and caching share one Redis database; finish deletes only RQ keys and the "
                                  "config cache keys")


def _safety_size_estimate():
    """
    Size of the safety set an in-place load keeps on the spool: the newest set of this install, else the live
    database's size on disk.
    """
    try:
        from ..models import BackupSet
        if BackupSet._meta.db_table not in connection.introspection.table_names():
            raise LookupError('no catalog in this database')  # an empty one: don't make the server log an error
        newest = BackupSet.objects.filter(status__in=SetStatusChoices.USABLE, size_bytes__isnull=False) \
            .order_by('-finished').values_list('size_bytes', flat=True).first()
        if newest:
            return newest
    except Exception:
        pass
    try:
        with conf.database().connect() as conn, conn.cursor() as cursor:
            cursor.execute('SELECT pg_database_size(current_database())')
            return cursor.fetchone()[0]
    except Exception:
        return 0


def _check_space(plan, headroom_confirmed):
    free = shutil.disk_usage(conf.spool_dir()).free
    needed = int(plan.size_bytes * 3)
    if plan.strategy == RestoreStrategyChoices.IN_PLACE and not plan.reason.startswith('the live database is empty'):
        needed += _safety_size_estimate()  # the local copy of the safety set stays until finalize
    headroom = conf.get_config().headroom
    if free < needed:
        plan.check('Space', ABORT, f"spool {conf.spool_dir()} has {free} bytes free, needs about {needed}")
    elif plan.strategy == RestoreStrategyChoices.SLOT and not headroom_confirmed and headroom:
        ok, detail = server_headroom(headroom)
        plan.check('Space', OK if ok else ABORT, f"spool has {free} bytes free; {detail}")
    elif plan.strategy == RestoreStrategyChoices.SLOT and not headroom_confirmed:
        plan.check('Space', ABORT, "server headroom not confirmed: loading the slot roughly doubles NetBox's "
                                   "footprint, and Azure Flexible Server turns read-only server-wide at 95% used. "
                                   "Configure 'headroom' or pass --headroom-confirmed once checked")
    else:
        plan.check('Space', OK, f"spool has {free} bytes free")


def server_headroom(headroom):
    """
    (ok, detail): whether loading the slot keeps the server below headroom['max_used_percent'] of its storage.
    The load is assumed to need twice the live database (the copy plus the WAL it writes).
    """
    try:
        with conf.database().connect() as conn, conn.cursor() as cursor:
            used, unknown = pgcore.server_usage(cursor, ignore=headroom['ignore_databases'])
            cursor.execute('SELECT pg_database_size(current_database())')
            live = cursor.fetchone()[0]
    except Exception as e:
        return False, f"server usage cannot be measured: {e}"
    if unknown:
        return False, (f"server usage cannot be measured: no CONNECT privilege on {', '.join(unknown)} (list them "
                       f"in headroom.ignore_databases if their size is known to be negligible)")
    projected = used + 2 * live
    limit = headroom['storage_bytes'] * headroom['max_used_percent'] / 100
    detail = (f"server: {used} bytes in use, ~{projected} after the load, limit {int(limit)} "
              f"({headroom['max_used_percent']}% of {headroom['storage_bytes']})")
    return projected <= limit, detail


def _human(delta):
    hours = delta.total_seconds() / 3600
    return f'{hours:.1f} h' if hours < 48 else f'{hours / 24:.1f} days'


# Identity snapshots: who could log in before the restore

def identity_snapshot(db):
    try:
        with db.connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT to_regclass('users_user') IS NOT NULL, to_regclass('users_token') IS NOT NULL")
            has_users, has_tokens = cursor.fetchone()
            users, tokens = {}, {}
            if has_users:
                cursor.execute('SELECT id, username, is_active FROM users_user')
                users = {str(i): {'username': u, 'active': a} for i, u, a in cursor.fetchall()}
            if has_tokens:
                cursor.execute('SELECT id, user_id, enabled FROM users_token')
                tokens = {str(i): {'user': u, 'enabled': e} for i, u, e in cursor.fetchall()}
            return {'users': users, 'tokens': tokens}
    except Exception:
        return {'users': {}, 'tokens': {}}


def resurrected(before, revoke=False):
    """
    Users and tokens active after the restore that were deleted or disabled before it.
    """
    from users.models import Token, User
    result = {'users': [], 'tokens': []}
    if not before or not (before.get('users') or before.get('tokens')):
        return result
    for user in User.objects.filter(is_active=True):
        prior = before['users'].get(str(user.pk))
        if prior is None or not prior['active']:
            result['users'].append(user.username)
            if revoke:
                User.objects.filter(pk=user.pk).update(is_active=False)
    for token in Token.objects.filter(enabled=True).select_related('user'):
        prior = before['tokens'].get(str(token.pk))
        if prior is None or not prior['enabled']:
            result['tokens'].append(f'{token.pk} ({token.user.username})')
            if revoke:
                Token.objects.filter(pk=token.pk).update(enabled=False)
    return result


# Load

class Restorer:

    def __init__(self, log=None, user=''):
        self.log = log or logger
        self.user = user
        self.config = conf.get_config()

    def _keyring(self):
        return Keyring(self.config.crypto, identities=True)

    def load(self, set_ref='latest', strategy=None, target=None, from_instance=None, components=None,
             confirm=None, skip_safety=False, headroom_confirmed=False, revoke_resurrected=False, qa=False):
        """
        qa=True (QA instances, in place only): finish runs as "finish --qa" right after the load.
        """
        if strategy not in (RestoreStrategyChoices.SLOT, RestoreStrategyChoices.IN_PLACE):
            raise RestoreError("load needs --slot or --in-place")
        if qa:
            self._check_qa_instance(from_instance)
            if not from_instance:
                raise RestoreError("A QA restore restores production's set: pass --from-instance <production "
                                   "instance_id>")
            if strategy != RestoreStrategyChoices.IN_PLACE:
                raise RestoreError("A QA restore loads in place: the QA instance replaces its own database")
        connection.close()
        with self._keyring() as keyring:
            plan = build_plan(set_ref, target=target, from_instance=from_instance, components=components,
                              headroom_confirmed=headroom_confirmed, keyring=keyring)
            for c in plan.checks:
                (self.log.error if c['result'] == ABORT else self.log.warning if c['result'] == WARN
                 else self.log.info)(f"[{c['result'].upper()}] {c['check']}: {c['detail']}")
            if plan.strategy != strategy and not qa:
                raise RestoreError(f"plan recommends {plan.strategy} ({plan.reason}); rerun plan or pass the matching "
                                   f"flag")
            if not plan.ok:
                raise RestoreError('Preflight failed: ' + '; '.join(f"{c['check']}: {c['detail']}"
                                                                    for c in plan.aborts))
            empty, not_empty, newest = dr_state(self.config, plan.live_database)
            rerun = next(iter(unfinished_journals(plan.live_database)), None)
            if strategy == RestoreStrategyChoices.IN_PLACE and confirm != plan.live_database and not empty:
                changes = '; '.join(f"{db}: newest change {ts.isoformat() if ts else 'none'}"
                                    for db, ts in newest.items())
                raise RestoreError(f"In-place restore replaces {plan.live_database} ({'; '.join(not_empty)}). "
                                   f"{changes}. Pass --confirm {plan.live_database}")
            self._dr_empty = empty
            self._rerun = rerun if rerun is not None and rerun['strategy'] == RestoreStrategyChoices.IN_PLACE \
                else None
            journal = journal_mod.Journal.create(
                plan.set_id, strategy, plan.target, source_instance_id=plan.source_instance_id,
                components=plan.components, plan=plan.as_dict(), user=self.user, live_database=plan.live_database,
                slot_database=plan.slot_database if strategy == RestoreStrategyChoices.SLOT else plan.live_database,
                previous_database=plan.live_database if strategy == RestoreStrategyChoices.SLOT else None,
                run_migrations=plan.run_migrations, revoke_resurrected=revoke_resurrected, qa=qa,
            )
            journal.update(status='loading')
            journal.save_identities(keyring, identity_snapshot(conf.database()))
            journal.step('plan', detail=plan.reason)
            self.log.info(f"Restore {journal.restore_id}: {strategy} load of set {plan.set_id}")
            try:
                return self._load(journal, plan, keyring, skip_safety)
            except BaseException as e:
                journal.update(status='failed', error=f'{type(e).__name__}: {e}')
                journal.step('failed', status='failed', error=str(e))
                if strategy == RestoreStrategyChoices.IN_PLACE and journal.get('emptied'):
                    self.log.error(f"The live database was emptied. Run: nbbackup restore rollback "
                                   f"{journal.restore_id}")
                raise

    def _load(self, journal, plan, keyring, skip_safety):
        backend = get_backend(self.config.target(plan.target))
        instance = plan.source_instance_id
        manifest = sets.load_manifest(backend, instance, plan.set_id)
        spool = conf.spool_dir('restore', journal.restore_id)
        in_place = journal['strategy'] == RestoreStrategyChoices.IN_PLACE

        if in_place:
            self._safety(journal, keyring, skip_safety)

        keys_used = {}
        paths = sets.fetch_and_decrypt(backend, keyring, manifest, plan.components, spool, keys_used=keys_used)
        journal.update(spool=spool, files={k: v for k, v in paths.items() if k != 'database'}, keys_used=keys_used)
        journal.step('fetch', detail=f"{len(paths)} component(s) verified and decrypted")

        if 'database' in plan.components:
            target_db = conf.database(dbname=journal['slot_database'])
            self._load_database(journal, plan, target_db, paths['database'])
            os.unlink(paths['database'])
        journal.update(status='loaded')
        journal.step('loaded')
        if in_place:
            return self.finish(journal=journal, qa=bool(journal.get('qa')))
        self.log.info(
            f"Set {plan.set_id} is loaded into {journal['slot_database']}. Now stop NetBox web and all workers, point "
            f"DATABASES['default']['NAME'] (DB_NAME in netbox-docker) at {journal['slot_database']}, start one ops "
            f"container and run: nbbackup restore finish {journal.restore_id}"
        )
        return journal

    def _safety(self, journal, keyring, skip_safety):
        from .backup import run_backup
        rerun = getattr(self, '_rerun', None)
        if rerun is not None:
            # The live database may hold half a load: never take a new safety set of that, reuse the first one
            journal.update(rerun_of=rerun.restore_id, safety_set_id=rerun.get('safety_set_id'),
                           safety_target=rerun.get('safety_target'), safety_local=rerun.get('safety_local'))
            rerun.update(status='superseded', superseded_by=journal.restore_id)
            rerun.step('superseded', detail=f'rerun as {journal.restore_id}')
            journal.step('safety', status='reused',
                         detail=f"safety set {rerun.get('safety_set_id') or '(none)'} of restore {rerun.restore_id}")
            return
        if getattr(self, '_dr_empty', False):
            journal.step('safety', status='skipped', detail='live database is empty (disaster-recovery mode)')
            return
        if skip_safety:
            journal.warn('--skip-safety: no safety set was taken')
            journal.step('safety', status='skipped', detail='--skip-safety')
            return
        policy = journal['plan'].get('policy') or next(iter(self.config.policies), None)
        if policy is None:
            raise RestoreError("No policy configured for the safety set; pass --skip-safety to proceed without one")
        self.log.info("Taking a safety set of the live database and media")
        connection.close()
        safety = run_backup(policy, trigger=TriggerChoices.SAFETY, created_by=self.user, log=self.log,
                            components=[c for c in ('database', 'media', 'scripts', 'config')
                                        if c in self.config.policies[policy].components or c == 'database'])
        connection.close()
        if safety.verify_level < VerifyLevelChoices.INTEGRITY:
            raise RestoreError(f"Safety set {safety.set_id} did not pass level 1; nothing was deleted")
        # Local copy as a fast path for rollback, kept until finalize
        backend = get_backend(self.config.target(self.config.policies[policy].target))
        safety_manifest = sets.load_manifest(backend, self.config.instance_id, safety.set_id)
        manifest_mod.verify(safety_manifest, keyring)
        local = conf.spool_dir('safety', journal.restore_id)
        sets.fetch(backend, safety_manifest.objects[layout.OBJECT_NAMES['database']],
                   os.path.join(local, layout.OBJECT_NAMES['database']))
        journal.update(safety_set_id=safety.set_id, safety_target=self.config.policies[policy].target,
                       safety_local=local)
        journal.step('safety', detail=f"safety set {safety.set_id} uploaded and verified")

    def _load_database(self, journal, plan, target_db, dump_path):
        connection.close()
        journal.mirror = target_db.name != live_database_name()
        try:
            self._empty_and_load(journal, plan, target_db, dump_path)
        finally:
            journal.mirror = True

    def _empty_and_load(self, journal, plan, target_db, dump_path):
        with target_db.connect() as conn:
            sessions = pgcore.other_sessions(conn)
            if sessions:
                raise RestoreError(f"{len(sessions)} other session(s) on {target_db.name}; stop NetBox first")
            if journal['strategy'] == RestoreStrategyChoices.SLOT and not pgcore.has_slot_marker(conn, target_db.name):
                raise RestoreError(f"{target_db.name} lost its slot marker; refusing to empty it")
            with conn.cursor() as cursor:
                missing = pgcore.missing_roles(cursor, plan.body.get('database', {}).get('grantees', []))
            pgcore.empty_database(conn)
        journal.update(emptied=True)
        journal.step('empty', detail=target_db.name)
        if missing:
            dropped = pgcore.acl_statements(target_db, dump_path, missing)
            journal.update(dropped_grants=dropped)
            journal.warn(f"Grantee role(s) {', '.join(missing)} missing on this server: loaded without privileges; "
                         f"the {len(dropped)} dropped GRANT/REVOKE/ALTER DEFAULT PRIVILEGES statement(s) are in the "
                         f"journal (dropped_grants) to re-apply once the roles exist")
        self.log.info(f"Loading the dump into {target_db.name} ({self.config.restore_jobs} jobs)")
        pgcore.load_dump(target_db, dump_path, jobs=self.config.restore_jobs, no_privileges=bool(missing),
                         log=self.log)
        journal.step('pg_restore')
        with target_db.connect() as conn, conn.cursor() as cursor:
            actual = pgcore.baseline(cursor)
        diff = pgcore.compare_baseline(plan.body['baseline'], actual)
        if diff:
            raise RestoreError(f"Baseline mismatch after load: {pgcore.format_differences(diff)}")
        journal.step('baseline', detail=f"{len(actual['tables'])} tables, {actual['rows']} rows match")

    # Finish

    def finish(self, restore_id=None, journal=None, qa=False, revoke_resurrected=None):
        journal = journal or journal_mod.load(restore_id, statuses=('loaded', 'finishing'))
        if journal is None:
            raise RestoreError("No loaded restore to finish (see: nbbackup restore status)")
        if qa:
            self._check_qa_instance(journal.get('source_instance_id'))
        live = live_database_name()
        slot = journal['strategy'] == RestoreStrategyChoices.SLOT
        connection.close()
        if live != journal['slot_database']:
            raise RestoreError(f"NetBox is connected to {live}, not the restored database {journal['slot_database']}. "
                               f"Point DATABASES['default']['NAME'] at it first.")
        db = conf.database()
        with db.connect() as conn:
            if slot:
                old = journal['previous_database']
                sessions = pgcore.other_sessions(conn, old)
                if sessions:
                    raise RestoreError(f"{len(sessions)} session(s) still use {old}; stop all web and worker processes")
                pgcore.set_slot_marker(conn, live, False)
                try:
                    pgcore.set_slot_marker(conn, old, True)
                except psycopg.Error:
                    # A disaster may have taken the old database with it; there is then nothing to roll back to
                    journal.warn(f"{old} could not be marked as the rollback copy (does it still exist?)")
        journal.update(status='finishing')
        journal.step('switch', detail=f"connected to {live}")

        spool = conf.spool_dir('restore', journal.restore_id)
        files = self._file_components(journal, spool)
        safety_copies = {}
        with self._keyring() as keyring:
            for component in FILE_COMPONENTS:
                if component in files:
                    path = os.path.join(spool, f'{component}-before.tar.zst.enc')
                    media_mod.snapshot_storage(component, path, keyring)
                    safety_copies[component] = path
        journal.update(media_safety=safety_copies)
        journal.step('media safety copy', detail=', '.join(safety_copies) or 'no file components')

        import io

        from django.core.management import call_command
        call_command('migrate', interactive=False, verbosity=0, stdout=io.StringIO())
        conf.forget_recorded_instance()  # the restored database brings its own InstanceIdentity row
        journal.step('migrate')
        self._reset_runtime_state(journal)
        call_command('reindex', lazy=True, verbosity=0, stdout=io.StringIO())
        with db.connect() as conn:
            pgcore.analyze(conn)
        journal.step('reindex and analyze')

        # Media and scripts, every path: through STORAGES["default"] and STORAGES["scripts"] with the same guards
        for component, path in files.items():
            if component in FILE_COMPONENTS and os.path.exists(path):
                result = media_mod.restore_archive(component, path, journal.restore_id, log=self.log)
                os.unlink(path)  # plaintext; storage still holds the set if it is needed again
                journal.step(f'restore {component}', detail=f"{result['written']} written, "
                                                            f"{result['quarantined']} quarantined")

        if qa:
            self._qa_checks(journal)

        revoke = journal.get('revoke_resurrected') if revoke_resurrected is None else revoke_resurrected
        with self._keyring() as keyring:
            before = journal.load_identities(keyring)
        report = resurrected(before, revoke=revoke)
        if report['users'] or report['tokens']:
            verb = 'disabled' if revoke else 'active again'
            message = (f"{len(report['users'])} user(s) and {len(report['tokens'])} token(s) that were deleted or "
                       f"disabled before the restore are {verb}: {', '.join(report['users'][:20])}")
            journal.warn(message)
            self.log.warning(message)
        journal.update(resurrected=report)
        self._check_peppers(journal)
        self._compare_secrets(journal)

        from . import catalog
        try:
            catalog.sync(log=self.log)
        except Exception as e:
            journal.warn(f"catalog sync failed: {e}")
        now = timezone.now()
        journal.update(status='finished', finished=now.isoformat(),
                       rollback_until=(now + timedelta(days=self.config.rollback_days)).isoformat())
        journal.step('finished')
        if not qa:
            self._enqueue_full_backup(journal)
        self.log.info(f"Restore {journal.restore_id} finished. Start web and workers. Rollback is possible until "
                      f"{journal['rollback_until']}; then run: nbbackup restore finalize {journal.restore_id}")
        return journal

    def _check_qa_instance(self, source_instance):
        if not self.config.instance_id_pinned:
            raise RestoreError("A QA instance needs its own pinned instance_id; otherwise it would write below "
                               "production's")
        if source_instance and source_instance == self.config.instance_id:
            raise RestoreError("A QA restore restores another instance's set: pass --from-instance <production "
                               "instance_id>; the QA instance has its own instance_id")

    def _qa_checks(self, journal):
        """
        Level 2 beyond the load's baseline check: no migration left to apply, and the API answers.
        """
        import io

        from django.core.management import call_command

        from . import qa as qa_mod
        checks = [{'check': 'baseline', 'ok': any(s['step'] == 'baseline' for s in journal['steps']),
                   'detail': next((s.get('detail', '') for s in journal['steps'] if s['step'] == 'baseline'), '')}]
        try:
            call_command('migrate', check=True, plan=True, interactive=False, stdout=io.StringIO())
            checks.append({'check': 'migrate --check', 'ok': True, 'detail': 'no unapplied migration'})
        except SystemExit:
            checks.append({'check': 'migrate --check', 'ok': False, 'detail': 'unapplied migrations remain'})
        api = qa_mod.api_smoke_test()
        checks.append({'check': 'API smoke test', 'ok': all(r['status'] == 200 for r in api),
                       'detail': ', '.join(f"{r['path']} {r['status']}" for r in api)})
        journal.update(qa_checks=checks)
        journal.step('qa checks', status='done' if all(c['ok'] for c in checks) else 'failed',
                     detail='; '.join(f"{c['check']}: {'ok' if c['ok'] else 'FAILED'}" for c in checks))
        failed = [c for c in checks if not c['ok']]
        if failed:
            raise RestoreError('QA checks failed: ' + '; '.join(f"{c['check']}: {c['detail']}" for c in failed))

    def qa_run(self, set_ref='latest', from_instance=None, target=None, confirm=None):
        """
        One scheduled QA restore: load production's set in place, finish --qa, and write a signed report of the
        outcome, passed or failed, below the production instance.
        """
        from . import qa as qa_mod
        self._check_qa_instance(from_instance)
        if not from_instance:
            raise RestoreError("qa run needs --from-instance <production instance_id>")
        started = timezone.now()
        target_name, _, manifest = locate(self.config, set_ref, target=target, from_instance=from_instance)
        journal, error = None, ''
        try:
            journal = self.load(manifest.set_id, strategy=RestoreStrategyChoices.IN_PLACE, target=target_name,
                                from_instance=from_instance, confirm=confirm, skip_safety=True, qa=True)
        except Exception as e:
            error = f'{type(e).__name__}: {e}'
            journal = journal_mod.load(statuses=('failed', 'loading', 'loaded', 'finishing'))
            if journal is not None and journal['set_id'] != manifest.set_id:
                journal = None
        checks = (journal.get('qa_checks') if journal else None) or []
        report = qa_mod.build_report(
            self.config, manifest.set_id, from_instance, started, passed=not error, checks=checks,
            keys_used=(journal.get('keys_used') if journal else None), error=error,
            restore_id=journal.restore_id if journal else None,
        )
        where = qa_mod.store_report(self.config, report, target_name)
        self.log.info(f"QA report {report['report_id']} ({'passed' if report['passed'] else 'FAILED'}) written to "
                      f"{where[0]}: {where[1]}")
        if error:
            raise RestoreError(f"QA restore of {manifest.set_id} failed: {error}")
        return journal, report

    def _file_components(self, journal, spool):
        """
        Decrypted media/scripts archives for finish. finish often runs in a fresh one-shot container whose spool is
        empty, so missing archives are fetched and verified again.
        """
        files = {c: p for c, p in (journal.get('files') or {}).items() if c in FILE_COMPONENTS}
        wanted = [c for c in journal.get('components', []) if c in FILE_COMPONENTS]
        missing = [c for c in wanted if not (files.get(c) and os.path.exists(files[c]))]
        if missing:
            backend = get_backend(self.config.target(journal['target']))
            manifest = sets.load_manifest(backend, journal['source_instance_id'] or self.config.instance_id,
                                          journal['set_id'])
            with self._keyring() as keyring:
                manifest_mod.verify(manifest, keyring)
                files.update(sets.fetch_and_decrypt(backend, keyring, manifest, missing, spool))
            journal.update(files=files)
            journal.step('fetch file components', detail=', '.join(missing))
        return files

    def _reset_runtime_state(self, journal):
        from core.choices import JobStatusChoices
        from core.models import Job
        from django.contrib.sessions.models import Session
        errored = Job.objects.filter(status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES).update(
            status=JobStatusChoices.STATUS_ERRORED, completed=timezone.now(),
            error='Restored from a backup set; never ran on this database',
        )
        journal.step('jobs', detail=f"{errored} restored job(s) marked errored")
        cleared = clear_redis()
        journal.step('redis', detail=cleared)
        deleted, _ = Session.objects.all().delete()
        journal.step('sessions', detail=f"{deleted} session(s) deleted")

    def _drill_config(self, journal, backend, keyring, manifest, spool, keys_used):
        """
        The config component is encrypted to the recovery key alone: a drill with that key mounted decrypts and
        parses it, others record that it was not readable here.
        """
        if layout.OBJECT_NAMES['config'] not in manifest.objects:
            return
        try:
            path = sets.fetch_and_decrypt(backend, keyring, manifest, ['config'], spool, keys_used=keys_used)['config']
            files = _drill_config_doc(path).get('files', {})
            os.unlink(path)
            journal.step('config', detail=f'{len(files)} configuration file(s), decrypted by {keys_used["config"]}')
        except GPGError:
            keys_used.pop('config', None)
            journal.step('config', status='skipped', detail='needs the recovery key (not mounted here)')

    def _compare_secrets(self, journal):
        """
        Name the secrets that differ between this host and the set. The config component is encrypted to the
        recovery key alone, so this works only where that key is mounted; never prints a value.
        """
        import json
        backend = get_backend(self.config.target(journal['target']))
        try:
            manifest = sets.load_manifest(backend, journal.get('source_instance_id') or self.config.instance_id,
                                          journal['set_id'])
            if layout.OBJECT_NAMES['config'] not in manifest.objects:
                return
            spool = conf.spool_dir('restore', journal.restore_id)
            with self._keyring() as keyring:
                manifest_mod.verify(manifest, keyring)
                path = sets.fetch_and_decrypt(backend, keyring, manifest, ['config'], spool)['config']
            with open(path, 'rb') as f:
                from_set = secrets_mod.from_config_component(json.loads(f.read()))
            os.unlink(path)
        except (GPGError, sets.SetError, StorageError, manifest_mod.ManifestError) as e:
            journal.step('compare secrets', status='skipped',
                         detail=f"config component not readable here ({str(e)[:120]}); compare with: netbox-backup "
                                f"decrypt {journal['set_id']} --components config --format env (recovery key)")
            return
        current = {
            'SECRET_KEY': settings.SECRET_KEY,
            'API_TOKEN_PEPPERS': dict(getattr(settings, 'API_TOKEN_PEPPERS', {}) or {}),
            'DB_PASSWORD': settings.DATABASES['default'].get('PASSWORD'),
        }
        differ, unknown = secrets_mod.differences(from_set, current)
        if differ:
            message = f"Secrets differ between this host and the set: {', '.join(differ)}"
            journal.warn(message)
            self.log.warning(message)
        journal.step('compare secrets', detail=f"differ: {', '.join(differ) or 'none'}"
                                               + (f"; not comparable: {', '.join(unknown)}" if unknown else ''))

    def _check_peppers(self, journal):
        from users.models import Token
        configured = set(getattr(settings, 'API_TOKEN_PEPPERS', {}) or {})
        used = set(Token.objects.exclude(pepper_id__isnull=True).values_list('pepper_id', flat=True))
        if used - configured:
            journal.warn(f"Token pepper ID(s) {sorted(used - configured)} missing from API_TOKEN_PEPPERS: those "
                         f"tokens stop validating. Restore the original peppers, or reissue the tokens.")

    def _enqueue_full_backup(self, journal):
        from ..jobs import enqueue_backup
        from ..models import BackupPolicy
        from . import catalog
        catalog.sync_static(self.config)
        policy = BackupPolicy.objects.filter(enabled=True).first()
        if policy is None:
            journal.warn('No enabled policy: take a full backup now')
            return
        try:
            enqueue_backup(policy, trigger=TriggerChoices.POST_RESTORE)
            journal.step('enqueue full backup', detail=policy.name)
        except Exception as e:
            journal.warn(f"Could not enqueue the post-restore backup: {e}")

    # Rollback and finalize

    def rollback(self, restore_id=None, confirm=None):
        journal = journal_mod.load(restore_id, statuses=('loaded', 'finishing', 'finished', 'failed'))
        if journal is None:
            raise RestoreError("No restore to roll back")
        live = live_database_name()
        connection.close()
        if journal['strategy'] == RestoreStrategyChoices.SLOT:
            if journal['status'] in ('loaded', 'failed') and live == journal['previous_database']:
                journal.update(status='rolled-back')
                journal.step('rollback', detail='never switched: the live database was not touched')
                return journal
            if live != journal['previous_database']:
                raise RestoreError(f"Point DATABASES['default']['NAME'] back at {journal['previous_database']} first "
                                   f"(connected to {live})")
            with conf.database().connect() as conn:
                pgcore.set_slot_marker(conn, journal['slot_database'], True)
                pgcore.set_slot_marker(conn, live, False)
            journal.step('switch back', detail=f"connected to {live}")
        else:
            if confirm != live:
                raise RestoreError(f"Rollback reloads {live} from the safety set: pass --confirm {live}")
            self._reload_safety(journal)
        safety_copies = {c: p for c, p in (journal.get('media_safety') or {}).items() if os.path.exists(p)}
        if safety_copies:
            with self._keyring() as keyring:
                for component, path in safety_copies.items():
                    result = media_mod.restore_encrypted_archive(component, path, f'{journal.restore_id}-rollback',
                                                                 keyring, log=self.log)
                    journal.step(f'restore {component}', detail=f"{result['written']} written from the safety copy")
        if journal['strategy'] == RestoreStrategyChoices.IN_PLACE:
            self._reset_runtime_state(journal)
        journal.update(status='rolled-back')
        journal.step('rolled back')
        return journal

    def _reload_safety(self, journal):
        if not journal.get('safety_set_id'):
            raise RestoreError("This in-place restore has no safety set (--skip-safety)")
        target = self.config.target(journal['safety_target'])
        backend = get_backend(target)
        manifest = sets.load_manifest(backend, self.config.instance_id, journal['safety_set_id'])
        with self._keyring() as keyring:
            manifest_mod.verify(manifest, keyring)
            body = manifest_mod.decrypt_body(manifest, keyring)
            obj = manifest.objects[layout.OBJECT_NAMES['database']]
            local = os.path.join(journal.get('safety_local') or conf.spool_dir('safety', journal.restore_id),
                                 layout.OBJECT_NAMES['database'])
            try:
                if not os.path.exists(local):
                    raise sets.SetError('no local copy')
                _verify_local(local, obj)
                self.log.info("Rolling back from the local safety copy")
            except sets.SetError:
                self.log.info("Rolling back from the safety set in storage")
                sets.fetch(backend, obj, local)
            dump = sets.decrypt_file(keyring, local, local.removesuffix('.enc') + '.dump')
        db = conf.database()
        with db.connect() as conn:
            if pgcore.other_sessions(conn):
                raise RestoreError("Other sessions are connected; stop NetBox first")
            pgcore.empty_database(conn)
        pgcore.load_dump(db, dump, jobs=self.config.restore_jobs, log=self.log)
        os.unlink(dump)
        with db.connect() as conn, conn.cursor() as cursor:
            diff = pgcore.compare_baseline(body['baseline'], pgcore.baseline(cursor))
        if diff:
            raise RestoreError(f"Safety set baseline mismatch: {pgcore.format_differences(diff)}")
        journal.step('reload safety set', detail=journal['safety_set_id'])

    def finalize(self, restore_id=None, force=False):
        journal = journal_mod.load(restore_id, statuses=('finished', 'rolled-back'))
        if journal is None:
            raise RestoreError("No finished or rolled-back restore to finalize")
        until = parse_datetime(journal['rollback_until']) if journal.get('rollback_until') else None
        if until and timezone.now() < until and not force:
            raise RestoreError(f"Rollback window open until {until:%Y-%m-%d %H:%M}; pass --force to finalize early")
        if journal['strategy'] == RestoreStrategyChoices.SLOT:
            old = journal['previous_database'] if journal['status'] == 'finished' else journal['slot_database']
            with conf.database().connect() as conn:
                if old == live_database_name():
                    raise RestoreError(f"{old} is the live database")
                if not pgcore.has_slot_marker(conn, old):
                    raise RestoreError(f"{old} has no slot marker; refusing to empty it")
                if pgcore.other_sessions(conn, old):
                    raise RestoreError(f"{old} has other sessions")
            with conf.database(dbname=old).connect() as conn:
                pgcore.empty_database(conn)
            journal.step('empty old database', detail=old)
        for path in [journal.get('safety_local'), journal.get('spool')]:
            if path and os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        journal.update(status='finalized')
        journal.step('finalized')
        return journal

    # Media alone

    def restore_media(self, set_ref='latest', target=None, components=('media',)):
        target_name, backend, manifest = locate(self.config, set_ref, target=target)
        journal = journal_mod.Journal.create(manifest.set_id, 'media', target_name, components=list(components),
                                             user=self.user, live_database=live_database_name())
        spool = conf.spool_dir('restore', journal.restore_id)
        with self._keyring() as keyring:
            manifest_mod.verify(manifest, keyring)
            paths = sets.fetch_and_decrypt(backend, keyring, manifest, list(components), spool)
        journal.step('fetch')
        safety = {}
        with self._keyring() as keyring:
            for component in components:
                path = os.path.join(spool, f'{component}-before.tar.zst.enc')
                media_mod.snapshot_storage(component, path, keyring)
                safety[component] = path
        journal.update(media_safety=safety, files=paths, status='finishing')
        journal.step('media safety copy')
        for component, path in paths.items():
            result = media_mod.restore_archive(component, path, journal.restore_id, log=self.log)
            os.unlink(path)
            journal.step(f'restore {component}', detail=f"{result['written']} written, "
                                                        f"{result['quarantined']} quarantined")
        now = timezone.now()
        journal.update(status='finished', finished=now.isoformat(),
                       rollback_until=(now + timedelta(days=self.config.rollback_days)).isoformat())
        journal.step('finished')
        return journal

    # Drill: the QA restore on demand, in the slot

    def drill(self, set_ref='latest', target=None, headroom_confirmed=False):
        """
        Load a set into the restore slot, compare the baseline, check its migration plan, empty the slot again.
        Passing marks the set as QA-verified (level 2).
        """
        from ..models import BackupSet
        connection.close()
        live = live_database_name()
        slot = slot_for(self.config, live)
        if not slot:
            raise RestoreError('verify --drill needs a restore slot ("databases" in the config)')
        with self._keyring() as keyring:
            plan = build_plan(set_ref, target=target, headroom_confirmed=headroom_confirmed, keyring=keyring)
            plan.slot_database = slot
            if plan.strategy != RestoreStrategyChoices.SLOT:
                # A drill always loads the slot: check it and the server headroom as a slot load would
                plan.strategy = RestoreStrategyChoices.SLOT
                plan.checks = [c for c in plan.checks if c['check'] != 'Space']
                _check_slot(plan, self.config, report=True)
                _check_space(plan, headroom_confirmed)
            aborts = plan.aborts
            if aborts:
                raise RestoreError('Drill preflight failed: ' + '; '.join(f"{c['check']}: {c['detail']}"
                                                                          for c in aborts))
            journal = journal_mod.Journal.create(plan.set_id, 'drill', plan.target, slot_database=slot,
                                                 user=self.user, live_database=live, components=['database'])
            backend = get_backend(self.config.target(plan.target))
            manifest = sets.load_manifest(backend, plan.source_instance_id, plan.set_id)
            spool = conf.spool_dir('restore', journal.restore_id)
            keys_used = {}
            try:
                available = [c for c in ('database', 'media', 'scripts')
                             if layout.OBJECT_NAMES[c] in manifest.objects]
                paths = sets.fetch_and_decrypt(backend, keyring, manifest, available, spool, keys_used=keys_used)
                journal.step('fetch', detail=', '.join(f'{c} decrypted by {k}' for c, k in keys_used.items()))
                for component in ('media', 'scripts'):
                    if component in paths:
                        members = media_mod.archive_members(paths[component])
                        journal.step(f'list {component}', detail=f'{len(members)} member(s), every path checked')
                self._drill_config(journal, backend, keyring, manifest, spool, keys_used)
                self._load_database(journal, plan, conf.database(dbname=slot), paths['database'])
                pending = _pending_migrations(slot)
                journal.step('migration plan', detail=f"{len(pending)} migration(s) would be applied")
            finally:
                with conf.database(dbname=slot).connect() as conn:
                    pgcore.empty_database(conn)
                shutil.rmtree(spool, ignore_errors=True)
        now = timezone.now()
        fields = {'verify_level': VerifyLevelChoices.QA_RESTORE, 'qa_passed_at': now, 'verified_at': now,
                  'status': SetStatusChoices.VERIFIED}
        recovery = self.config.crypto.recovery_key
        if recovery and keys_used and all(k == recovery for k in keys_used.values()):
            fields['recovery_drill_at'] = now  # the offline recovery key alone restored this set
        BackupSet.objects.filter(set_id=plan.set_id).update(**fields)
        journal.update(status='finished', finished=now.isoformat(), keys_used=keys_used)
        journal.step('drill passed')
        return journal


def _drill_config_doc(path):
    import json
    with open(path, 'rb') as f:
        return json.loads(f.read())


def _verify_local(path, obj):
    import hashlib
    digest = hashlib.sha384()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != obj['sha384']:
        raise sets.SetError('local safety copy does not match the safety set')


def _pending_migrations(dbname):
    """
    Migrations the running code would apply to the database in the slot.
    """
    with conf.database(dbname=dbname).connect() as conn, conn.cursor() as cursor:
        cursor.execute('SELECT app, name FROM django_migrations')
        applied = [f'{a}.{n}' for a, n in cursor.fetchall()]
    unknown, pending, _ = migration_state(applied)
    if unknown:
        raise RestoreError(f"The slot has {len(unknown)} migration(s) this code does not know")
    return sorted(pending)


def clear_redis():
    """
    Empty NetBox's RQ queues and registries, and its cache. Whole databases are cleared only when they hold NetBox's
    keys alone; otherwise only RQ keys and the config/config_version cache keys are deleted.
    """
    import django_rq
    from django.core.cache import cache
    tasks = django_rq.get_connection('default')
    deleted = 0
    batch = []
    for key in tasks.scan_iter(match='rq:*', count=1000):
        batch.append(key)
        if len(batch) >= 1000:
            deleted += tasks.delete(*batch)
            batch = []
    if batch:
        deleted += tasks.delete(*batch)
    tasks_cfg, cache_cfg = settings.REDIS.get('tasks', {}), settings.REDIS.get('caching', {})
    shared = all(tasks_cfg.get(k) == cache_cfg.get(k) for k in ('HOST', 'PORT', 'DATABASE'))
    cleared_cache = False
    if not shared:
        try:
            from django_redis import get_redis_connection
            client = get_redis_connection('default')
            prefix = cache.make_key('')
            foreign = any(not k.decode(errors='replace').startswith(prefix)
                          for k in client.scan_iter(count=1000))
            if not foreign:
                cache.clear()
                cleared_cache = True
        except Exception:
            pass
    if not cleared_cache:
        cache.delete_many(['config', 'config_version'])
    return f"{deleted} RQ key(s) deleted; cache {'cleared' if cleared_cache else 'config keys deleted'}"


def init_slot(database):
    """
    One-time setup: mark an empty database owned by the NetBox role as the restore slot.
    """
    config = conf.get_config()
    live = live_database_name()
    if database == live:
        raise RestoreError(f"{database} is the live database")
    if config.databases and database not in config.databases:
        raise RestoreError(f"{database} is not listed in 'databases' ({', '.join(config.databases)})")
    with conf.database().connect() as conn:
        _, owner = pgcore.database_comment(conn, database)
        if owner != conn.info.user:
            raise RestoreError(f"{database} is owned by {owner}, not {conn.info.user}")
        already = pgcore.has_slot_marker(conn, database)
    slot_db = conf.database(dbname=database)
    with slot_db.connect() as conn, conn.cursor() as cursor:
        if not already and not pgcore.is_empty(conn):
            raise RestoreError(f"{database} is not empty; slot init only marks an empty database")
        foreign = pgcore.foreign_owned(cursor)
        if foreign['extensions'] or foreign['schemas']:
            raise RestoreError(
                f"{database} has objects the NetBox role does not own ({foreign}); let the NetBox role create its "
                f"extensions itself"
            )
    with conf.database().connect() as conn:
        pgcore.set_slot_marker(conn, database, True)
    return database
