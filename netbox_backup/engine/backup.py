"""
Creating backups.

A backup is a directory in storage, <instance_id>/<name>/, holding one file per artifact plus manifest.json. The
manifest is uploaded last and is the commit marker: a directory without one is an incomplete backup.
"""
import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
import socket
import tempfile
import time

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .. import __version__, conf
from ..choices import BackupKindChoices, BackupStatusChoices
from ..storage import get_storage
from . import crypto, files, pg

MANIFEST_FORMAT = 'netbox-backup/1'
MANIFEST_NAME = 'manifest.json'
ADVISORY_LOCK_KEY = 0x4E42424B  # "NBBK"

logger = logging.getLogger('netbox_backup')


class BackupError(Exception):
    pass


class BackupInProgress(BackupError):
    pass


def generate_name(kind, when=None):
    when = when or timezone.now()
    return f"{when.strftime('%Y%m%dT%H%M%SZ')}-{kind}-{secrets.token_hex(2)}"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def storage_key(instance_id, name, filename=''):
    return '/'.join(p for p in (instance_id, name, filename) if p)


def location_of(storage, instance_id, name):
    info = storage.describe()
    if info['backend'] == 's3':
        prefix = info['prefix'].strip('/')
        return f"s3://{info['bucket']}/{'/'.join(p for p in (prefix, instance_id, name) if p)}/"
    if info['backend'] == 'local':
        return os.path.join(info['path'], instance_id, name) + os.sep
    return f"{info['backend']}:{instance_id}/{name}/"


def installed_plugins():
    from django.apps import apps
    result = {}
    for name in getattr(settings, 'PLUGINS', []):
        try:
            app = apps.get_app_config(name.rsplit('.', 1)[-1])
            result[name] = getattr(app, 'version', '') or ''
        except LookupError:
            result[name] = ''
    return result


@contextlib.contextmanager
def backup_lock():
    """
    Cluster-wide mutual exclusion for backups and restores via a PostgreSQL advisory lock.
    """
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s)', [ADVISORY_LOCK_KEY])
        if not cursor.fetchone()[0]:
            raise BackupInProgress("Another backup or restore is already running")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_unlock(%s)', [ADVISORY_LOCK_KEY])


class BackupBuilder:
    """
    Builds the artifacts of one backup in a private working directory and uploads them.
    """

    def __init__(self, backup, log=None, storage=None):
        self.backup = backup
        self.log = log or logger
        self.storage = storage or get_storage()
        self.key = conf.encryption_key()
        self.warnings = []
        self.artifacts = []
        self.manifest = {}

    def run(self):
        backup = self.backup
        backup.status = BackupStatusChoices.STATUS_RUNNING
        backup.started_at = timezone.now()
        backup.instance_id = backup.instance_id or conf.instance_id()
        backup.encrypted = bool(self.key)
        backup.netbox_version = settings.VERSION
        backup.location = location_of(self.storage, backup.instance_id, backup.name)
        backup.save()
        started = time.monotonic()
        workdir = tempfile.mkdtemp(prefix='netbox-backup-', dir=conf.work_dir())
        os.chmod(workdir, 0o700)
        try:
            with backup_lock():
                self.storage.check()
                self._build(workdir)
                if conf.get('verify_after_backup'):
                    self._quick_verify(workdir)
                self._upload(workdir, time.monotonic() - started)
        except Exception as e:
            backup.status = BackupStatusChoices.STATUS_FAILED
            backup.error = f'{type(e).__name__}: {e}'
            backup.completed_at = timezone.now()
            backup.save()
            self.log.error(f"Backup {backup.name} failed: {backup.error}")
            # Never leave a half-written backup behind: without a manifest it is unusable anyway.
            with contextlib.suppress(Exception):
                self.storage.delete_prefix(storage_key(backup.instance_id, backup.name))
            raise
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        backup.status = BackupStatusChoices.STATUS_WARNING if self.warnings else BackupStatusChoices.STATUS_COMPLETED
        backup.completed_at = timezone.now()
        backup.manifest = self.manifest
        backup.size = sum(a['size'] for a in self.artifacts)
        backup.save()
        self.log.info(f"Backup {backup.name} {backup.get_status_display().lower()} ({backup.size} bytes)")
        return backup

    def warn(self, message):
        self.warnings.append(message)
        self.log.warning(message)

    def _build(self, workdir):
        backup = self.backup
        dump_path = os.path.join(workdir, 'database.dump')
        self.log.info(f"Dumping database {settings.DATABASES['default'].get('NAME')}")
        with pg.exported_snapshot() as (snapshot_id, cursor, conn):
            client_version, server_major = pg.check_client_compatibility(conn)
            row_counts = pg.row_counts(cursor)
            migrations = pg.applied_migrations(cursor)
            server_version = conn.info.parameter_status('server_version')
            pg.dump(dump_path, snapshot_id=snapshot_id, logger=self.log)
        if not row_counts:
            self.warn("The database contains no tables; this backup holds an empty schema")
        self.log.info(f"Dumped {len(row_counts)} tables, {sum(row_counts.values())} rows")
        self._add_artifact(workdir, 'database.dump', 'database', dump_path)

        if backup.kind == BackupKindChoices.KIND_FULL:
            for name, (path, warning) in files.file_trees().items():
                if warning:
                    self.warn(warning)
                    continue
                if not os.path.isdir(path):
                    self.warn(f"Directory for '{name}' does not exist: {path}")
                    continue
                archive = os.path.join(workdir, f'{name}.tar.gz')
                count, size = files.archive_tree(path, archive)
                self.log.info(f"Archived {name}: {count} files, {size} bytes from {path}")
                self._add_artifact(workdir, f'{name}.tar.gz', name, archive, source=path, files=count,
                                   source_bytes=size)
            if conf.include_configuration():
                archive = os.path.join(workdir, 'configuration.tar.gz')
                included, warnings = files.archive_configuration(archive)
                for warning in warnings:
                    self.warn(warning)
                self.log.info(f"Archived configuration: {', '.join(included)}")
                self._add_artifact(workdir, 'configuration.tar.gz', 'configuration', archive, source=included)
            else:
                self.warn(
                    "Configuration not included (it contains secrets; enable encryption or set "
                    "include_configuration=True). Keep SECRET_KEY and API_TOKEN_PEPPERS safe separately."
                )

        self.manifest = {
            'format': MANIFEST_FORMAT,
            'name': backup.name,
            'kind': backup.kind,
            'trigger': backup.trigger,
            'requested_by': backup.requested_by,
            'instance_id': backup.instance_id,
            'hostname': socket.gethostname(),
            'started_at': backup.started_at.isoformat(),
            'netbox': {
                'version': settings.VERSION,
                'plugins': installed_plugins(),
            },
            'plugin_version': __version__,
            'postgresql': {
                'server_version': server_version,
                'pg_dump_major_version': client_version,
            },
            'database': {
                'name': settings.DATABASES['default'].get('NAME'),
                'tables': len(row_counts),
                'rows': sum(row_counts.values()),
                'row_counts': row_counts,
            },
            'migrations': migrations,
            'encryption': {
                'enabled': bool(self.key),
                'algorithm': 'AES-256-GCM, scrypt key derivation' if self.key else None,
                'key_fingerprint': crypto.key_fingerprint(self.key) if self.key else None,
            },
            'artifacts': self.artifacts,
            'warnings': self.warnings,
        }

    def _add_artifact(self, workdir, filename, artifact_type, path, **extra):
        plaintext_size = os.path.getsize(path)
        plaintext_sha256 = sha256_file(path)
        stored_path, stored_name = path, filename
        if self.key:
            stored_name = f'{filename}.enc'
            stored_path = os.path.join(workdir, stored_name)
            crypto.encrypt_file(path, stored_path, self.key)
        self.artifacts.append({
            'name': stored_name,
            'type': artifact_type,
            'size': os.path.getsize(stored_path),
            'sha256': sha256_file(stored_path) if self.key else plaintext_sha256,
            'plaintext_size': plaintext_size,
            'plaintext_sha256': plaintext_sha256,
            'encrypted': bool(self.key),
            **extra,
        })

    def _quick_verify(self, workdir):
        """
        Before uploading, prove the dump is readable and actually contains data for every non-empty table.
        Guards against a "successful" backup that holds nothing.
        """
        toc = pg.dump_toc(os.path.join(workdir, 'database.dump'))
        tables_with_data = {name for entry_type, name in toc if entry_type == 'TABLE DATA'}
        counts = self.manifest['database']['row_counts']
        missing = sorted(t for t, n in counts.items() if n and t not in tables_with_data)
        if missing:
            raise BackupError(f"Dump is missing data for {len(missing)} table(s): {', '.join(missing[:10])}")
        for artifact in self.artifacts:
            if artifact['type'] in ('database', 'configuration'):
                continue
            files.list_archive(os.path.join(workdir, artifact['name'].removesuffix('.enc')))
        self.log.info(f"Pre-upload verification passed ({len(tables_with_data)} tables with data)")

    def _upload(self, workdir, elapsed):
        backup = self.backup
        for artifact in self.artifacts:
            key = storage_key(backup.instance_id, backup.name, artifact['name'])
            self.log.info(f"Uploading {artifact['name']} ({artifact['size']} bytes)")
            self.storage.put_file(key, os.path.join(workdir, artifact['name']))
            stored = self.storage.size(key)
            if stored != artifact['size']:
                raise BackupError(f"Uploaded {artifact['name']} is {stored} bytes, expected {artifact['size']}")
        self.manifest['completed_at'] = timezone.now().isoformat()
        self.manifest['duration_seconds'] = round(elapsed, 3)
        self.manifest['size'] = sum(a['size'] for a in self.artifacts)
        self.manifest['status'] = (
            BackupStatusChoices.STATUS_WARNING if self.warnings else BackupStatusChoices.STATUS_COMPLETED
        )
        self.storage.put_bytes(
            storage_key(backup.instance_id, backup.name, MANIFEST_NAME),
            json.dumps(self.manifest, indent=2, sort_keys=True).encode(),
        )


def create_backup(kind=BackupKindChoices.KIND_DATABASE, trigger='cli', requested_by='', job=None, log=None,
                  backup=None):
    """
    Create and upload a backup. Returns the Backup row. Raises on failure (the row is marked failed first).
    """
    from ..models import Backup
    if backup is None:
        backup = Backup(
            name=generate_name(kind),
            instance_id=conf.instance_id(),
            kind=kind,
            trigger=trigger,
            requested_by=requested_by or '',
            job=job,
        )
    return BackupBuilder(backup, log=log).run()
