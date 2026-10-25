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
from ..storage import ObjectExists, get_backend
from . import catalog, components
from .locks import advisory_lock

logger = logging.getLogger('netbox_backup')


class BackupError(Exception):
    pass


def ensure_instance(backend, config):
    """
    Claim <instance_id>/instance.json with a create-only write; refuse if another install claimed it first.
    """
    import json
    existing = sets.read_instance(backend, config.instance_id)
    if existing is None:
        document = {
            'instance_id': config.instance_id,
            'hostname': socket.gethostname(),
            'site_url': getattr(settings, 'BASE_PATH', '') or '',
            'created': timezone.now().isoformat(),
            'plugin_version': __version__,
        }
        try:
            backend.put_bytes(layout.instance_key(config.instance_id), json.dumps(document, indent=1).encode())
        except ObjectExists:
            existing = sets.read_instance(backend, config.instance_id)
    if existing is not None and existing.get('instance_id') != config.instance_id:
        raise BackupError(f"{layout.instance_key(config.instance_id)} belongs to {existing.get('instance_id')}")


def preflight(config):
    problems = conf.startup_problems()
    if problems:
        raise BackupError('; '.join(problems))
    if not config.crypto.configured:
        raise BackupError("crypto is not configured: there is no unencrypted mode (see docs/configuration.md)")


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
        preflight(self.config)
        targets = catalog.sync_static(self.config)
        policy_row = BackupPolicy.objects.get(name=self.policy.name)
        set_id = layout.new_ulid()
        backup_set = BackupSet.objects.create(
            set_id=set_id, instance_id=self.config.instance_id, policy=policy_row, policy_name=self.policy.name,
            status=SetStatusChoices.RUNNING, trigger=self.trigger, created_by=self.created_by,
            started=timezone.now(), netbox_version=settings.VERSION, job=self.job,
        )
        copy = BackupCopy.objects.create(set=backup_set, target=targets[self.target.name], is_primary=True,
                                         status=CopyStatusChoices.UPLOADING)
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
                backend = get_backend(self.target)
                ensure_instance(backend, self.config)
                with Keyring(self.config.crypto, signing=True) as keyring:
                    header, body = self._upload(backend, keyring, db, set_id)
                    raw = manifest.build(header, body, keyring)
                    backend.put_bytes(layout.object_key(self.config.instance_id, set_id, layout.MANIFEST), raw)
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
            self._job_data()
            self.log.error(f"Set {set_id} failed: {backup_set.error}")
            raise
        self._job_data()
        self.log.info(
            f"Set {set_id} {backup_set.get_status_display().lower()}: {backup_set.size_bytes} bytes on "
            f"{self.target.name}"
        )
        return backup_set

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
            record = pipeline.upload_component(keyring, backend, key('config'), source)
            self.objects[layout.OBJECT_NAMES['config']] = record.as_dict()
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
            'recipients': list(self.config.crypto.recipients),
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
            'components': self.summaries,
            'warnings': self.warnings,
        }
        return header, body


def run_backup(policy_name, trigger=TriggerChoices.SCHEDULED, created_by='', job=None, log=None, components=None):
    # Long jobs hold their own connections; Django's may have gone stale in a long-lived worker
    connection.close_if_unusable_or_obsolete()
    return BackupRunner(policy_name, trigger=trigger, created_by=created_by, job=job, log=log,
                        components_override=components).run()
