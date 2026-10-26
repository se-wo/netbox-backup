"""
Taking one backup set.

    1. Take the policy's advisory lock; refuse to run against a database carrying the slot marker.
    2. Open a REPEATABLE READ READ ONLY transaction and export its snapshot. Record versions, migration state,
       the baseline and the files the database references in it, and keep it open until pg_dump --snapshot exits.
    3. Stream each component through gpg to the primary target with create-only writes, hashing on the way.
       The dump is also fed to pg_restore, whose row counts must equal the baseline (the dump check).
    4. Write the manifest, then _COMPLETE.
    5. Level-1 check: sizes and checksums as the backend reports them, the signature, the lock status.
"""
import dataclasses
import logging
import socket

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .. import __version__, conf
from ..choices import CopyStatusChoices, SetStatusChoices, TriggerChoices, VerifyLevelChoices
from ..core import layout, manifest, pipeline, sets
from ..core import pg as pgcore
from ..core.gpg import Keyring
from ..storage import ObjectExists
from . import catalog, components
from .locks import advisory_lock, assert_held

logger = logging.getLogger('netbox_backup')


class BackupError(Exception):
    pass


def this_host():
    return {'hostname': socket.gethostname(), 'allowed_hosts': sorted(getattr(settings, 'ALLOWED_HOSTS', []))}


def same_install(recorded, here):
    """
    Whether instance.json describes this install. ALLOWED_HOSTS decides; with a wildcard, the hostname must match.
    Pod hostnames change on every rollout, so a hostname alone never decides when ALLOWED_HOSTS is specific.
    """
    hosts = sorted(recorded.get('allowed_hosts') or [])
    if hosts != here['allowed_hosts']:
        return False
    return hosts not in ([], ['*']) or recorded.get('hostname') == here['hostname']


def ensure_instance(backend, config):
    """
    The instance_id to write under, claimed with a create-only instance.json. Unpinned installs generate a random
    UUID on their first backup and record it; later, they refuse to back up if instance.json describes a different
    install (a clone, a QA copy restored from production): pin instance_id there. Returns the instance_id.
    """
    import json
    import uuid

    from ..models import InstanceIdentity
    from .locks import advisory_lock
    here = this_host()
    if config.instance_id_pinned:
        instance_id = config.instance_id
    else:
        with advisory_lock('instance-identity'):
            conf.forget_recorded_instance()
            recorded = conf.recorded_instance()
            if recorded is None:
                instance_id = str(uuid.uuid4())
                InstanceIdentity.objects.create(instance_id=instance_id, **here)
                conf.forget_recorded_instance()
            else:
                instance_id = recorded['instance_id']
    existing = sets.read_instance(backend, instance_id)
    if existing is None:
        document = {'instance_id': instance_id, **here, 'created': timezone.now().isoformat(),
                    'plugin_version': __version__}
        try:
            backend.put_bytes(layout.instance_key(instance_id), json.dumps(document, indent=1).encode())
        except ObjectExists:
            existing = sets.read_instance(backend, instance_id)
    if existing is not None:
        if existing.get('instance_id') != instance_id:
            raise BackupError(f"{layout.instance_key(instance_id)} belongs to {existing.get('instance_id')}")
        if not config.instance_id_pinned and not same_install(existing, here):
            raise BackupError(
                f"instance_id {instance_id} was recorded by another install ({existing.get('hostname')}, "
                f"ALLOWED_HOSTS {existing.get('allowed_hosts')}); this looks like a clone or QA copy. Pin a new "
                f"instance_id in PLUGINS_CONFIG['netbox_backup']"
            )
    return instance_id


def database_volume_problem(target):
    """
    Why a local target's path must not be used, or None: a backup on the database's own volume is lost with it.
    Detectable only for a database on this host whose data_directory the role may read (superuser or
    pg_read_all_settings); otherwise this check passes silently.
    """
    import os
    path = target.options.get('path')
    db = conf.database()
    host = db.params.get('host')
    if not path or (host and not host.startswith('/') and host not in ('localhost', '127.0.0.1', '::1')):
        return None
    try:
        with db.connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT setting FROM pg_settings WHERE name = 'data_directory'")
            row = cursor.fetchone()
    except Exception:
        return None
    data_dir = row[0] if row else None
    if not data_dir or not os.path.exists(data_dir):
        return None
    existing = os.path.abspath(path)
    while not os.path.exists(existing):
        existing = os.path.dirname(existing)
    if os.stat(existing).st_dev == os.stat(data_dir).st_dev:
        return f"{path} is on the same filesystem as the PostgreSQL data directory {data_dir}"
    return None


def preflight(config, target=None):
    problems = conf.startup_problems()
    if problems:
        raise BackupError('; '.join(problems))
    if not config.crypto.configured:
        raise BackupError("crypto is not configured: there is no unencrypted mode (see docs/configuration.md)")
    if target is not None and target.backend == 'local':
        problem = database_volume_problem(target)
        if problem:
            raise BackupError(f"Target {target.name}: {problem}; a backup there is lost with the database")


class BackupRunner:

    def __init__(self, policy_name, trigger=TriggerChoices.SCHEDULED, created_by='', job=None, log=None,
                 components_override=None):
        self.config = conf.get_config()
        self.policy = self.config.policy(policy_name)
        self.target = self.config.target(self.policy.target)
        self.trigger = trigger
        self.created_by = created_by or ''
        self.job = job
        self.log = log or logger
        self.components = tuple(components_override or self.policy.components)
        self.warnings = []
        self.objects = {}
        self.summaries = {}

    def warn(self, message):
        self.warnings.append(message)
        self.log.warning(message)

    def run(self):
        from ..models import BackupCopy, BackupPolicy, BackupSet
        preflight(self.config, self.target)
        targets = catalog.sync_static(self.config)
        policy_row = BackupPolicy.objects.get(name=self.policy.name)
        set_id = layout.new_ulid()
        backup_set = BackupSet.objects.create(
            set_id=set_id, instance_id=self.config.instance_id or '', policy=policy_row, policy_name=self.policy.name,
            status=SetStatusChoices.RUNNING, trigger=self.trigger, created_by=self.created_by,
            started=timezone.now(), netbox_version=settings.VERSION, job=self.job,
        )
        copy = BackupCopy.objects.create(set=backup_set, target=targets[self.target.name], is_primary=True,
                                         status=CopyStatusChoices.UPLOADING)
        # Replica copies exist from the start, so prune holds the set on the primary until every replica has it
        for replica in self.policy.replicas:
            BackupCopy.objects.create(set=backup_set, target=targets[replica], is_primary=False,
                                      status=CopyStatusChoices.PENDING)
        self.set = backup_set
        self._job_data()
        db = conf.database()
        try:
            with advisory_lock(f'policy:{self.policy.name}', db=db) as lock_conn:
                if pgcore.has_slot_marker(lock_conn, db.name):
                    raise BackupError(
                        f"Database {db.name} carries the restore slot marker: this NetBox points at a slot that "
                        f"was never finished. Refusing to back up possibly stale data."
                    )
                backend = catalog.configured_backend(self.target)
                instance_id = ensure_instance(backend, self.config)
                self.config = dataclasses.replace(self.config, instance_id=instance_id)
                if backup_set.instance_id != instance_id:
                    backup_set.instance_id = instance_id
                    backup_set.save(update_fields=('instance_id',))
                with Keyring(self.config.crypto, signing=True) as keyring:
                    header, body = self._upload(backend, keyring, db, set_id)
                    raw = manifest.build(header, body, keyring)
                    lock_name = f'policy:{self.policy.name}'
                    self._before_visible(lock_conn, lock_name)
                    backend.put_bytes(layout.object_key(self.config.instance_id, set_id, layout.MANIFEST), raw)
                    self._before_visible(lock_conn, lock_name)
                    backend.put_bytes(layout.object_key(self.config.instance_id, set_id, layout.COMPLETE),
                                      manifest.complete_marker(raw))
                    backup_set.manifest_sha384 = manifest.sha384(raw)
                    backup_set.status = SetStatusChoices.UPLOADED
                    backup_set.finished = timezone.now()
                    backup_set.size_bytes = sum(o['size'] for o in header['objects'].values()) + len(raw)
                    backup_set.components = catalog.set_fields_from_header(header)['components']
                    backup_set.baseline = body['baseline']
                    backup_set.warnings = self.warnings
                    backup_set.save()
                    copy.status = CopyStatusChoices.UPLOADED
                    copy.uploaded = timezone.now()
                    copy.save()
                    # Level 1: compare what the backend reports with what we computed while streaming
                    loaded = sets.load_manifest(backend, self.config.instance_id, set_id)
                    results = sets.level1(backend, loaded, keyring, target=self.target)
            copy.checksum_mode = backend.capabilities.object_checksum or 'size'
            stats = [backend.stat(o['key']) for o in header['objects'].values()]
            locks = [s.retain_until for s in stats if s and s.retain_until]
            copy.locked_until = min(locks) if locks else None
            copy.save()
            if not catalog.mark_level(backup_set, results, VerifyLevelChoices.INTEGRITY, target=self.target.name):
                failed = '; '.join(f"{r['check']}: {r['detail']}" for r in results if not r['ok'])
                raise BackupError(f"Level-1 check failed: {failed}")
        except BaseException as e:
            backup_set.status = SetStatusChoices.FAILED
            backup_set.error = f'{type(e).__name__}: {e}'
            backup_set.finished = timezone.now()
            backup_set.warnings = self.warnings
            backup_set.save()
            copy.status = CopyStatusChoices.FAILED
            copy.error = backup_set.error
            copy.save()
            backup_set.copies.filter(is_primary=False).update(status=CopyStatusChoices.FAILED,
                                                              error='the backup itself failed')
            self._job_data()
            self.log.error(f"Set {set_id} failed: {backup_set.error}")
            raise
        self._job_data()
        self.log.info(
            f"Set {set_id} {backup_set.get_status_display().lower()}: {backup_set.size_bytes} bytes on "
            f"{self.target.name}"
        )
        return backup_set

    def _before_visible(self, lock_conn, lock_name):
        """
        A set becomes visible only while this job still holds its lock and is within max_runtime: past it, the
        scheduler may already have declared the set failed and freed the policy for the next run.
        """
        deadline = self.set.started + self.policy.max_runtime
        if timezone.now() > deadline:
            raise BackupError(f"max_runtime ({self.policy.max_runtime}) exceeded; not writing the manifest")
        assert_held(lock_conn, lock_name)

    def _job_data(self):
        if self.job is not None:
            self.job.data = {
                'set_id': self.set.set_id, 'status': self.set.status, 'policy': self.policy.name,
                'size_bytes': self.set.size_bytes, 'warnings': self.warnings, 'error': self.set.error,
            }
            self.job.save(update_fields=('data',))

    def _upload(self, backend, keyring, db, set_id):
        instance_id = self.config.instance_id
        key = lambda component: layout.object_key(instance_id, set_id, layout.OBJECT_NAMES[component])  # noqa: E731
        with pgcore.exported_snapshot(db) as (snapshot, cursor, conn):
            client_major = pgcore.check_dump_client(db, conn)
            baseline = pgcore.baseline(cursor)
            if not baseline['tables']:
                raise BackupError("The database contains no tables; refusing to record an empty set")
            cursor.execute("SELECT to_regclass('django_migrations') IS NOT NULL")
            migrations = []
            if cursor.fetchone()[0]:
                cursor.execute('SELECT app, name FROM django_migrations ORDER BY app, name')
                migrations = [f'{a}.{n}' for a, n in cursor.fetchall()]
            cursor.execute('SELECT pg_database_size(current_database()), version()')
            db_size, server_version = cursor.fetchone()
            server_major = conn.info.server_version // 10000
            grantees = pgcore.grantees(cursor)
            cursor.execute("SELECT to_regclass('users_token') IS NOT NULL")
            peppers = []
            if cursor.fetchone()[0]:
                cursor.execute('SELECT DISTINCT pepper_id FROM users_token WHERE pepper_id IS NOT NULL')
                peppers = sorted(row[0] for row in cursor.fetchall())
            schemas = pgcore.user_schemas(cursor)
            references = components.referenced_files(cursor) if set(self.components) & {'media', 'scripts'} \
                else {}

            if 'database' in self.components:
                self.log.info(f"Dumping {db.name} ({db_size} bytes on disk, {baseline['rows']} rows)")
                compression = f'zstd:{self.policy.zstd_level}' if client_major and client_major >= 16 else None
                with db.environment() as env:
                    source = pipeline.CommandSource(
                        pgcore.dump_args(db, snapshot=snapshot, compression=compression), env=env, name='pg_dump',
                    )
                    taps = [pgcore.RowCountTap(db, baseline['tables'])] if self.config.verify_dump else []
                    record = pipeline.upload_component(keyring, backend, key('database'), source,
                                                       size_hint=db_size, taps=taps)
                self.objects[layout.OBJECT_NAMES['database']] = record.as_dict()
                self.summaries['database'] = {
                    'tables': len(baseline['tables']), 'rows': baseline['rows'],
                    'dump_bytes': record.plaintext_size, 'dump_checked': bool(taps),
                }
                if record.plaintext_size == 0:
                    raise BackupError("pg_dump produced no output")
            # The snapshot ends here; media is read after it, so every file it references already exists

        for component in ('media', 'scripts'):
            if component not in self.components:
                continue
            tree = components.StorageTree(component, references.get(component, ()), level=self.policy.zstd_level)
            if tree.referenced and not tree.names:
                raise BackupError(
                    f"{component}: the database references {len(tree.referenced)} file(s) but the storage is empty; "
                    f"is the volume mounted?"
                )
            record = pipeline.upload_component(keyring, backend, key(component), tree.source)
            self.objects[layout.OBJECT_NAMES[component]] = record.as_dict()
            self.summaries[component] = tree.summary()
            if tree.missing:
                self.summaries[component]['media_missing'] = tree.missing[:1000]
                self.warn(f"{component}: {len(tree.missing)} file(s) referenced by the database are missing from "
                          f"storage, e.g. {tree.missing[0]}")
            self.log.info(f"{component}: {tree.source.files} files, {tree.source.bytes} bytes")

        if 'config' in self.components:
            source, summary = components.config_source()
            # The secrets go to the offline recovery key alone: the operational key sits on QA and restore hosts
            recovery = [self.config.crypto.recovery_key]
            record = pipeline.upload_component(keyring, backend, key('config'), source, recipients=recovery)
            self.objects[layout.OBJECT_NAMES['config']] = {**record.as_dict(), 'recipients': recovery}
            self.summaries['config'] = {'files': summary['files'], 'bytes': summary['bytes']}
            for path in summary['missing']:
                self.warn(f"config: {path} could not be read")

        now = timezone.now()
        header = {
            'set_id': set_id,
            'instance_id': instance_id,
            'policy': self.policy.name,
            'trigger': self.trigger,
            'created_at': self.set.started.isoformat(),
            'finished_at': now.isoformat(),
            'netbox_version': settings.VERSION,
            'plugin_version': __version__,
            'recipients': list(self.config.crypto.all_recipients),
            'recovery_key': self.config.crypto.recovery_key,
            'signer': self.config.crypto.signing_key,
            'objects': self.objects,
            'components': {
                name: {k: v for k, v in summary.items() if k != 'media_missing'}
                for name, summary in self.summaries.items()
            },
            'warnings': [w.split(', e.g.')[0] for w in self.warnings],
        }
        body = {
            'versions': {
                'netbox': settings.VERSION,
                'plugin': __version__,
                'plugins': components.installed_plugins(),
                'postgresql_server': server_version,
                'postgresql_server_major': server_major,
                'pg_dump_major': client_major,
            },
            'migrations': migrations,
            'created_by': self.created_by,
            'hostname': socket.gethostname(),
            'database': {'name': db.name, 'size_bytes': db_size, 'schemas': schemas, 'grantees': grantees},
            'baseline': baseline,
            'token_peppers': peppers,
            'components': self.summaries,
            'warnings': self.warnings,
        }
        return header, body


def run_backup(policy_name, trigger=TriggerChoices.SCHEDULED, created_by='', job=None, log=None, components=None):
    # Long jobs hold their own connections; Django's may have gone stale in a long-lived worker
    connection.close_if_unusable_or_obsolete()
    return BackupRunner(policy_name, trigger=trigger, created_by=created_by, job=job, log=log,
                        components_override=components).run()
