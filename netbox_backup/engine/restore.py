"""
Restoring a backup.

Order of operations (each step must succeed before anything destructive happens):

    1. preflight     format, instance, NetBox/migration compatibility, client versions, key, open connections
    2. safety backup a backup of the current state, so a mistaken restore can itself be undone
    3. fetch         download, verify checksums and decrypt every artifact needed
    4. database      wipe the schema, pg_restore, then compare per-table row counts with the manifest
    5. files         replace media/scripts/reports trees
    6. migrate       apply migrations if the backup predates the running code
    7. finalize      clear caches, resync the catalog from storage, record a RestoreEvent
"""
import logging
import os
import socket
import tarfile
import tempfile
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from packaging.version import InvalidVersion, Version

from .. import conf
from ..choices import BackupKindChoices, BackupStatusChoices, BackupTriggerChoices, RestoreStatusChoices
from ..storage import get_storage
from . import catalog, crypto, files, pg
from .backup import ADVISORY_LOCK_KEY, BackupError, create_backup
from .verify import compare_row_counts, format_mismatches

logger = logging.getLogger('netbox_backup')

FILE_COMPONENTS = ('media', 'scripts', 'reports')


class RestoreError(BackupError):
    pass


@dataclass
class Preflight:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    run_migrations: bool = False
    components: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(f"{timezone.now():%H:%M:%S} {record.levelname:<7} {record.getMessage()}")


def _parse_version(value):
    try:
        return Version(str(value).split('-')[0])
    except (InvalidVersion, TypeError):
        return None


def migration_state(applied):
    """
    Compare migrations recorded in a backup with those known to the running code. Returns (unknown, pending,
    installed_apps):
    migrations in the backup that this code does not have, and migrations this code has that the backup lacks.
    Squashed migrations count as applied when everything they replace is applied.
    """
    loader = MigrationLoader(None, ignore_no_migrations=True)
    known = set()
    replaces = {}
    for (app, name), migration in loader.disk_migrations.items():
        known.add(f'{app}.{name}')
        if migration.replaces:
            replaced = {f'{a}.{n}' for a, n in migration.replaces}
            replaces[f'{app}.{name}'] = replaced
            known |= replaced
    unknown = set(applied) - known
    pending = set()
    for app, name in loader.graph.nodes:
        key = f'{app}.{name}'
        if key in applied or (key in replaces and replaces[key] <= set(applied)):
            continue
        pending.add(key)
    return unknown, pending, {k.split('.', 1)[0] for k in known}


def available_components(manifest):
    return [a['type'] for a in manifest.get('artifacts', [])]


def preflight(manifest, components=None, allow_foreign_instance=False, allow_newer=False, terminate_connections=False,
              storage=None):
    """
    Decide whether a restore can safely proceed. Nothing is modified.
    """
    result = Preflight()
    storage = storage or get_storage()

    try:
        catalog.check_format(manifest)
    except BackupError as e:
        result.errors.append(str(e))
        return result

    if manifest.get('status') == BackupStatusChoices.STATUS_FAILED:
        result.errors.append("This backup is marked as failed")

    # Components
    available = available_components(manifest)
    if components is None:
        components = ['database'] + [c for c in FILE_COMPONENTS if c in available]
    for component in components:
        if component not in available:
            result.errors.append(f"Backup has no '{component}' artifact (available: {', '.join(available)})")
    result.components = [c for c in components if c in available]
    if 'configuration' in available:
        result.notes.append(
            "Backup includes configuration files. They are not applied automatically; use --extract-config DIR "
            "to retrieve them (needed if SECRET_KEY or API_TOKEN_PEPPERS differ on this host)."
        )

    # Instance: refusing another installation's backup is the guard against shared-bucket accidents
    local_instance = conf.instance_id()
    if manifest['instance_id'] != local_instance:
        message = (
            f"Backup belongs to instance '{manifest['instance_id']}' but this installation is '{local_instance}'. "
            f"Restoring it replaces this NetBox's data with the other installation's."
        )
        (result.warnings if allow_foreign_instance else result.errors).append(
            message if allow_foreign_instance else message + " Pass --allow-foreign-instance if that is intended."
        )

    # Code compatibility: a backup taken by newer code contains schema this code does not know
    backup_migrations = set(manifest.get('migrations', []))
    unknown, pending, installed_apps = migration_state(backup_migrations)
    unknown_installed = sorted(m for m in unknown if m.split('.', 1)[0] in installed_apps)
    unknown_uninstalled = sorted({m.split('.', 1)[0] for m in unknown} - installed_apps)
    if unknown_installed:
        message = (
            f"Backup was taken by newer code (NetBox {manifest.get('netbox', {}).get('version')}, running "
            f"{settings.VERSION}); {len(unknown_installed)} migration(s) are unknown here, e.g. "
            f"{', '.join(unknown_installed[:5])}. Install the same or a newer NetBox/plugin version first."
        )
        (result.warnings if allow_newer else result.errors).append(message)
    if unknown_uninstalled:
        result.warnings.append(
            f"Backup contains data for apps that are not installed here: {', '.join(unknown_uninstalled)}. "
            f"Install those plugins to use that data."
        )
    if pending:
        result.run_migrations = True
        result.warnings.append(
            f"Backup predates the running code; {len(pending)} migration(s) will be applied after the restore."
        )
    backup_version = _parse_version(manifest.get('netbox', {}).get('version'))
    running_version = _parse_version(settings.VERSION)
    if backup_version and running_version and backup_version > running_version and not unknown_installed:
        result.warnings.append(f"Backup was taken on NetBox {backup_version}; this is {running_version}.")

    # Encryption
    encryption = manifest.get('encryption') or {}
    if encryption.get('enabled'):
        key = conf.encryption_key()
        if not key:
            result.errors.append("Backup is encrypted but no encryption_key is configured")
        elif encryption.get('key_fingerprint') and crypto.key_fingerprint(key) != encryption['key_fingerprint']:
            result.errors.append(
                f"Configured encryption_key (fingerprint {crypto.key_fingerprint(key)}) is not the key this backup "
                f"was encrypted with ({encryption['key_fingerprint']})"
            )

    # PostgreSQL client tools
    if 'database' in result.components:
        try:
            with pg.connect() as conn:
                restore_major, server_major = pg.check_client_compatibility(conn, 'pg_restore')
                dump_major = manifest.get('postgresql', {}).get('pg_dump_major_version')
                if dump_major and restore_major and restore_major < dump_major:
                    result.errors.append(
                        f"pg_restore {restore_major} cannot read a dump made by pg_dump {dump_major}; install "
                        f"PostgreSQL {dump_major}+ client tools"
                    )
                source_server = _parse_version(manifest.get('postgresql', {}).get('server_version', '').split()[0])
                if source_server and source_server.major > server_major:
                    result.warnings.append(
                        f"Backup came from PostgreSQL {source_server.major}; restoring into {server_major}. "
                        f"Restoring into an older major version can fail."
                    )
                others = pg.other_connections(conn)
                django_pid = _django_backend_pid()
                others = [c for c in others if c[0] != django_pid]
                if others and not terminate_connections:
                    apps = ', '.join(sorted({c[2] or c[1] or 'unknown' for c in others}))
                    result.errors.append(
                        f"{len(others)} other session(s) are connected to the database ({apps}). Stop NetBox "
                        f"(gunicorn/uwsgi, netbox-rq) first, or pass --terminate-connections."
                    )
        except pg.PostgresError as e:
            result.errors.append(str(e))

    # Local file trees
    trees = files.file_trees()
    for component in result.components:
        if component in FILE_COMPONENTS:
            path, warning = trees.get(component, (None, f"'{component}' is not in include_files"))
            if warning or not path:
                result.errors.append(f"Cannot restore '{component}': {warning or 'no path configured'}")

    return result


def database_initialized():
    """
    True if the target database already holds a NetBox schema (as opposed to a fresh, empty DR host).
    """
    with pg.connect() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass('django_migrations') IS NOT NULL AND to_regclass('netbox_backup_backup') "
                       "IS NOT NULL")
        return cursor.fetchone()[0]


def _django_backend_pid():
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_backend_pid()')
            return cursor.fetchone()[0]
    except Exception:
        return None


def extract_configuration(archive, dest):
    os.makedirs(dest, mode=0o700, exist_ok=True)
    with tarfile.open(archive, 'r:gz') as tar:
        tar.extractall(dest, filter='data')
    return sorted(os.path.join(dest, n) for n in os.listdir(os.path.join(dest, 'config')))


def restore_backup(manifest, components=None, restored_by='', safety_backup=True, allow_foreign_instance=False,
                   allow_newer=False, terminate_connections=False, migrate=True, extract_config_to=None, jobs=1,
                   log=None):
    """
    Restore a backup described by its manifest. Returns the RestoreEvent recorded in the restored database.
    """
    log = log or logger
    handler = _ListHandler()
    log.addHandler(handler)
    if log.getEffectiveLevel() > logging.INFO:
        log.setLevel(logging.INFO)
    started_at = timezone.now()
    storage = get_storage()
    safety_name = ''
    mismatches = {}
    warnings = []
    database_touched = False
    try:
        check = preflight(
            manifest, components=components, allow_foreign_instance=allow_foreign_instance,
            allow_newer=allow_newer, terminate_connections=terminate_connections, storage=storage,
        )
        for note in check.notes:
            log.info(note)
        for warning in check.warnings:
            log.warning(warning)
        if not check.ok:
            raise RestoreError('Preflight failed:\n  - ' + '\n  - '.join(check.errors))
        warnings += check.warnings
        components = check.components

        if safety_backup and not database_initialized():
            log.info("Target database holds no NetBox schema (new host); no safety backup needed")
            safety_backup = False
        if safety_backup:
            kind = BackupKindChoices.KIND_FULL if set(components) & set(FILE_COMPONENTS) else \
                BackupKindChoices.KIND_DATABASE
            log.info(f"Creating {kind} safety backup of the current state")
            safety = create_backup(kind=kind, trigger=BackupTriggerChoices.TRIGGER_PRE_RESTORE,
                                   requested_by=restored_by, log=log)
            safety_name = safety.name
            log.info(f"Safety backup {safety_name} created; restore it to undo this restore")

        with tempfile.TemporaryDirectory(prefix='netbox-backup-restore-', dir=conf.work_dir()) as workdir:
            types = list(components)
            if extract_config_to:
                types.append('configuration')
            paths = catalog.fetch_artifacts(manifest, workdir, types=types, storage=storage, log=log)

            if 'database' in components:
                database_touched = True
                mismatches = _restore_database(manifest, paths['database'], terminate_connections, jobs, log)

            for component in components:
                if component in FILE_COMPONENTS:
                    target = files.file_trees()[component][0]
                    log.info(f"Restoring {component} into {target}")
                    files.restore_tree(paths[component], target)

            if extract_config_to:
                extracted = extract_configuration(paths['configuration'], extract_config_to)
                log.info(f"Configuration files extracted to {extract_config_to}: {', '.join(extracted)}")

        if 'database' in components and migrate and check.run_migrations:
            log.info("Applying migrations")
            call_command('migrate', interactive=False, verbosity=0)
        elif check.run_migrations:
            warnings.append("Migrations were not applied; run 'manage.py migrate' before starting NetBox")
            log.warning(warnings[-1])

        cache.clear()
        if 'database' in components:
            try:
                reconcile_jobs(log)
            except Exception as e:
                warnings.append(f"Could not reconcile background jobs: {e}")
                log.warning(warnings[-1])
            try:
                catalog.sync(log=log, storage=storage)
            except Exception as e:  # The restore itself succeeded; a sync failure must not mask that
                warnings.append(f"Catalog sync failed: {e}")
                log.warning(warnings[-1])

        if mismatches:
            status = RestoreStatusChoices.STATUS_RECOVERY_REQUIRED
        elif warnings:
            status = RestoreStatusChoices.STATUS_WARNING
        else:
            status = RestoreStatusChoices.STATUS_COMPLETED
        log.info(f"Restore of {manifest['name']} finished: {status}")
        return _record(manifest, status, started_at, restored_by, components, safety_name, mismatches, handler)
    except Exception as e:
        log.error(f"Restore failed: {e}")
        if database_touched:
            log.error(
                "The database may be partially restored. "
                + (f"Restore the safety backup to return to the previous state: manage.py netbox_backup restore "
                   f"{safety_name}" if safety_name else "No safety backup was taken.")
            )
        try:
            _record(manifest, RestoreStatusChoices.STATUS_FAILED, started_at, restored_by, components or [],
                    safety_name, mismatches, handler)
        except Exception:
            pass  # The database may be unusable; the error is still raised and logged
        raise
    finally:
        log.removeHandler(handler)


def reconcile_jobs(log):
    """
    The restored core_job table reflects the moment of the backup: jobs that were queued or running then (including
    the backup job itself) have no counterpart in the task queue now. Left alone they look alive forever, and
    NetBox's system job scheduler treats them as already scheduled, so recurring jobs such as scheduled backups
    would silently stop. Mark them as errored; netbox-rq re-creates system job schedules when it restarts.
    """
    import django_rq
    from core.choices import JobStatusChoices
    from core.models import Job
    from utilities.rqworker import get_queue_for_model

    orphaned = []
    for job in Job.objects.filter(status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES):
        # Job.queue_name exists from NetBox 4.5; older releases derive the queue from the object type
        queue_name = getattr(job, 'queue_name', None) or get_queue_for_model(
            job.object_type.model if job.object_type else None
        )
        queue = django_rq.get_queue(queue_name)
        if queue.fetch_job(str(job.job_id)) is None:
            orphaned.append(job.pk)
    if orphaned:
        Job.objects.filter(pk__in=orphaned).update(
            status=JobStatusChoices.STATUS_ERRORED,
            completed=timezone.now(),
            error='Orphaned by database restore (no longer in the task queue)',
        )
        log.info(f"Marked {len(orphaned)} orphaned background job(s) from the backup as errored; restart netbox-rq "
                 f"to re-create scheduled jobs")
    return orphaned


def _restore_database(manifest, dump_path, terminate_connections, jobs, log):
    # Django's own connection would otherwise block the schema drop and hold stale state
    connection.close()
    with pg.connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(%s)', [ADVISORY_LOCK_KEY])
            if not cursor.fetchone()[0]:
                raise RestoreError("A backup or restore is currently running")
        try:
            if terminate_connections:
                pg.terminate_other_connections(conn)
            elif pg.other_connections(conn):
                raise RestoreError("Other sessions connected to the database; stop NetBox or use "
                                   "--terminate-connections")
            log.info("Dropping current database objects")
            pg.wipe_schema(conn)
            log.info(f"Restoring database dump ({jobs} job(s))")
            pg.restore(dump_path, jobs=jobs, logger=log)
            with conn.cursor() as cursor:
                restored = pg.row_counts(cursor)
        finally:
            with conn.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [ADVISORY_LOCK_KEY])

    expected = manifest.get('database', {}).get('row_counts', {})
    mismatches = compare_row_counts(expected, restored)
    if mismatches:
        log.error(f"RECOVERY REQUIRED: restored row counts differ from the backup: {format_mismatches(mismatches)}")
    else:
        log.info(f"Row counts verified: {len(expected)} tables, {sum(expected.values())} rows match the backup")
    return mismatches


def _record(manifest, status, started_at, restored_by, components, safety_name, mismatches, handler):
    from ..models import RestoreEvent
    return RestoreEvent.objects.create(
        backup_name=manifest['name'],
        backup_instance_id=manifest['instance_id'],
        backup_created=parse_datetime(manifest['started_at']) if manifest.get('started_at') else None,
        status=status,
        started_at=started_at,
        completed_at=timezone.now(),
        restored_by=restored_by or '',
        hostname=socket.gethostname(),
        components=list(components),
        netbox_version_backup=manifest.get('netbox', {}).get('version', ''),
        netbox_version_running=settings.VERSION,
        safety_backup_name=safety_name,
        row_count_mismatches=mismatches,
        log='\n'.join(handler.lines),
    )
