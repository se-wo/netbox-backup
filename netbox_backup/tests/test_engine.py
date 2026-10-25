"""
End-to-end tests against the real test database: these run pg_dump/pg_restore, so they use TransactionTestCase
(data must be committed to be visible to pg_dump) and require PostgreSQL client tools on the PATH.
"""
import json
import os
from datetime import timedelta
from unittest import mock

from dcim.models import Site
from django.conf import settings
from django.test import TransactionTestCase
from django.utils import timezone

from netbox_backup.choices import BackupStatusChoices, RestoreStatusChoices, VerificationStatusChoices
from netbox_backup.engine import catalog, health, restore, retention
from netbox_backup.engine.backup import MANIFEST_NAME, create_backup
from netbox_backup.engine.verify import Verifier, verify_backup
from netbox_backup.models import Backup, RestoreEvent
from netbox_backup.storage import get_storage

from .utils import PluginConfigMixin


class EngineTestBase(PluginConfigMixin, TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.media = os.path.join(self.tmp, 'media')
        os.makedirs(os.path.join(self.media, 'image-attachments'))
        with open(os.path.join(self.media, 'image-attachments', 'rack.png'), 'wb') as f:
            f.write(b'\x89PNG fake')
        self.media_override = self.settings(MEDIA_ROOT=self.media, STORAGES={
            **settings.STORAGES, 'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage',
                                             'OPTIONS': {'location': self.media}},
        })
        self.media_override.enable()
        from django.core.files.storage import storages
        storages._storages.pop('default', None)
        Site.objects.bulk_create([Site(name=f'Site {i}', slug=f'site-{i}') for i in range(25)])

    def tearDown(self):
        self.media_override.disable()
        from django.core.files.storage import storages
        storages._storages.pop('default', None)
        super().tearDown()

    def read_manifest(self, backup):
        return json.loads(get_storage().get_bytes(f'{backup.instance_id}/{backup.name}/{MANIFEST_NAME}'))

    def write_manifest(self, backup, manifest):
        get_storage().put_bytes(f'{backup.instance_id}/{backup.name}/{MANIFEST_NAME}', json.dumps(manifest).encode())


class BackupTest(EngineTestBase):

    def test_database_backup(self):
        backup = create_backup(kind='database', trigger='cli')
        self.assertEqual(backup.status, BackupStatusChoices.STATUS_COMPLETED)
        manifest = self.read_manifest(backup)
        self.assertEqual(manifest['database']['row_counts']['dcim_site'], 25)
        self.assertIn('dcim.0001_squashed', ' '.join(manifest['migrations']))
        self.assertEqual([a['type'] for a in manifest['artifacts']], ['database'])
        self.assertFalse(manifest['encryption']['enabled'])
        self.assertTrue(manifest['size'] > 0)

    def test_full_encrypted_backup_and_deep_verify(self):
        self.configure(encryption_key='s3cret', include_files=['media'])
        backup = create_backup(kind='full', trigger='cli')
        manifest = self.read_manifest(backup)
        types = {a['type'] for a in manifest['artifacts']}
        self.assertEqual(types, {'database', 'media', 'configuration'})
        self.assertTrue(all(a['name'].endswith('.enc') for a in manifest['artifacts']))
        passed, results = verify_backup(backup, level='restore')
        self.assertTrue(passed, results)
        backup.refresh_from_db()
        self.assertEqual(backup.verification_status, VerificationStatusChoices.STATUS_PASSED)
        self.assertIn('test restore row counts match', [r['check'] for r in results])

    def test_configuration_excluded_when_unencrypted(self):
        self.configure(include_files=['media'])
        backup = create_backup(kind='full', trigger='cli')
        self.assertEqual(backup.status, BackupStatusChoices.STATUS_WARNING)
        self.assertNotIn('configuration', [a['type'] for a in backup.artifacts])

    def test_corruption_detected_by_deep_verify(self):
        backup = create_backup(kind='database', trigger='cli')
        path = os.path.join(self.storage_path, backup.instance_id, backup.name, 'database.dump')
        with open(path, 'r+b') as f:
            f.seek(500)
            f.write(b'CORRUPT')
        verifier = Verifier(self.read_manifest(backup))
        self.assertTrue(Verifier(self.read_manifest(backup)).run('quick'))  # size unchanged
        self.assertFalse(verifier.run('deep'))
        self.assertIn('checksum mismatch', json.dumps(verifier.results))

    def test_missing_artifact_detected_by_quick_verify(self):
        backup = create_backup(kind='database', trigger='cli')
        os.unlink(os.path.join(self.storage_path, backup.instance_id, backup.name, 'database.dump'))
        passed, _ = verify_backup(backup, level='quick')
        self.assertFalse(passed)

    def test_failure_cleans_up_storage(self):
        with mock.patch('netbox_backup.engine.pg.dump', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                create_backup(kind='database', trigger='cli')
        backup = Backup.objects.get()
        self.assertEqual(backup.status, BackupStatusChoices.STATUS_FAILED)
        self.assertIn('boom', backup.error)
        self.assertEqual(get_storage().list_children(backup.instance_id), [])

    def test_backups_do_not_include_backup_storage(self):
        # Storage located inside MEDIA_ROOT must not be archived into itself
        self.storage_path = os.path.join(self.media, 'backups')
        self.configure(include_files=['media'])
        create_backup(kind='database', trigger='cli')
        backup = create_backup(kind='full', trigger='cli')
        from netbox_backup.engine import files
        workdir = os.path.join(self.tmp, 'dl')
        os.makedirs(workdir)
        paths = catalog.fetch_artifacts(self.read_manifest(backup), workdir, types=['media'])
        self.assertFalse(any(n.startswith('backups') for n in files.list_archive(paths['media'])))


class CatalogTest(EngineTestBase):

    def test_sync_rebuilds_catalog_from_storage(self):
        first = create_backup(kind='database', trigger='cli')
        second = create_backup(kind='database', trigger='cli')
        for backup in Backup.objects.all():
            backup.delete(delete_from_storage=False)
        stats = catalog.sync()
        self.assertEqual(stats['created'], 2)
        self.assertEqual(set(Backup.objects.values_list('name', flat=True)), {first.name, second.name})

    def test_sync_marks_vanished_backups_missing(self):
        backup = create_backup(kind='database', trigger='cli')
        get_storage().delete_prefix(f'{backup.instance_id}/{backup.name}')
        catalog.sync()
        backup.refresh_from_db()
        self.assertEqual(backup.status, BackupStatusChoices.STATUS_MISSING)

    def test_sync_marks_interrupted_backups_failed(self):
        Backup.objects.create(name='20200101T000000Z-database-dead', instance_id='x', kind='database',
                              trigger='cli', status=BackupStatusChoices.STATUS_RUNNING)
        catalog.sync()
        self.assertEqual(Backup.objects.get(name='20200101T000000Z-database-dead').status,
                         BackupStatusChoices.STATUS_FAILED)

    def test_resolve(self):
        db = create_backup(kind='database', trigger='cli')
        full = create_backup(kind='full', trigger='cli')
        self.assertEqual(catalog.resolve('latest')['name'], full.name)
        self.assertEqual(catalog.resolve('latest-database')['name'], db.name)
        self.assertEqual(catalog.resolve(db.name[:-2])['name'], db.name)


class RetentionTest(EngineTestBase):

    def test_prune_respects_retention_and_keep_last(self):
        self.configure(retention='7 days', keep_last=2)
        backups = [create_backup(kind='database', trigger='cli') for _ in range(4)]
        old = timezone.now() - timedelta(days=30)
        Backup.objects.filter(pk__in=[b.pk for b in backups]).update(started_at=old)
        removed = retention.prune()
        # All four are old, but the newest two survive because of keep_last
        self.assertEqual(len(removed), 2)
        self.assertEqual(Backup.objects.count(), 2)
        self.assertEqual(len(get_storage().list_children(backups[0].instance_id)), 2)

    def test_dry_run_deletes_nothing(self):
        self.configure(retention='1 day', keep_last=0)
        backup = create_backup(kind='database', trigger='cli')
        Backup.objects.filter(pk=backup.pk).update(started_at=timezone.now() - timedelta(days=2))
        self.assertEqual(retention.prune(dry_run=True), [backup.name])
        self.assertTrue(Backup.objects.filter(pk=backup.pk).exists())


class RestoreTest(EngineTestBase):

    def test_full_restore_roundtrip(self):
        from core.models import Job
        self.configure(encryption_key='k', include_files=['media'])
        # A queued job captured in the backup will have no counterpart in the task queue after the restore
        Job.objects.create(name='Scheduled database backup', status='scheduled', interval=1440,
                           job_id='00000000-0000-0000-0000-000000000001')
        backup = create_backup(kind='full', trigger='cli')
        manifest = catalog.resolve(backup.name)

        # Damage the instance: lose data, change data, lose and add files
        Site.objects.filter(slug__in=['site-1', 'site-2', 'site-3']).delete()
        Site.objects.filter(slug='site-4').update(name='Renamed')
        Site.objects.create(name='Extra', slug='extra')
        os.unlink(os.path.join(self.media, 'image-attachments', 'rack.png'))
        with open(os.path.join(self.media, 'stray.txt'), 'w') as f:
            f.write('stray')

        event = restore.restore_backup(manifest, restored_by='tester')
        self.assertEqual(Job.objects.get(job_id='00000000-0000-0000-0000-000000000001').status, 'errored')
        self.assertEqual(event.status, RestoreStatusChoices.STATUS_COMPLETED, event.log)
        self.assertEqual(event.row_count_mismatches, {})
        self.assertTrue(event.safety_backup_name)
        self.assertEqual(Site.objects.count(), 25)
        self.assertEqual(Site.objects.get(slug='site-4').name, 'Site 4')
        self.assertFalse(Site.objects.filter(slug='extra').exists())
        self.assertTrue(os.path.exists(os.path.join(self.media, 'image-attachments', 'rack.png')))
        self.assertFalse(os.path.exists(os.path.join(self.media, 'stray.txt')))
        # The safety backup captured the damaged state and is in the restored catalog
        safety = Backup.objects.get(name=event.safety_backup_name)
        self.assertEqual(safety.row_counts['dcim_site'], 23)
        self.assertEqual(RestoreEvent.objects.count(), 1)
        self.assertIn('Row counts verified', event.log)

    def test_restore_undo_via_safety_backup(self):
        backup = create_backup(kind='database', trigger='cli')
        Site.objects.create(name='After', slug='after')
        event = restore.restore_backup(catalog.resolve(backup.name))
        self.assertFalse(Site.objects.filter(slug='after').exists())
        restore.restore_backup(catalog.resolve(event.safety_backup_name), safety_backup=False)
        self.assertTrue(Site.objects.filter(slug='after').exists())

    def test_foreign_instance_refused(self):
        backup = create_backup(kind='database', trigger='cli')
        manifest = self.read_manifest(backup)
        manifest['instance_id'] = 'someone-else'
        check = restore.preflight(manifest)
        self.assertFalse(check.ok)
        self.assertIn('someone-else', ' '.join(check.errors))
        self.assertTrue(restore.preflight(manifest, allow_foreign_instance=True).ok)

    def test_newer_code_refused(self):
        backup = create_backup(kind='database', trigger='cli')
        manifest = self.read_manifest(backup)
        manifest['migrations'].append('dcim.9999_from_the_future')
        manifest['migrations'].append('some_plugin.0001_initial')
        check = restore.preflight(manifest)
        self.assertFalse(check.ok)
        self.assertIn('dcim.9999_from_the_future', ' '.join(check.errors))
        self.assertIn('some_plugin', ' '.join(check.warnings))

    def test_older_backup_schedules_migrations(self):
        backup = create_backup(kind='database', trigger='cli')
        manifest = self.read_manifest(backup)
        manifest['migrations'] = [m for m in manifest['migrations'] if m != 'netbox_backup.0001_initial']
        self.assertTrue(restore.preflight(manifest).run_migrations)
        self.assertFalse(restore.preflight(self.read_manifest(backup)).run_migrations)

    def test_wrong_key_refused(self):
        self.configure(encryption_key='right')
        backup = create_backup(kind='database', trigger='cli')
        self.configure(encryption_key='wrong')
        check = restore.preflight(self.read_manifest(backup))
        self.assertFalse(check.ok)
        self.assertIn('not the key', ' '.join(check.errors))

    def test_row_count_mismatch_flags_recovery_required(self):
        backup = create_backup(kind='database', trigger='cli')
        manifest = self.read_manifest(backup)
        manifest['database']['row_counts']['dcim_site'] = 999
        event = restore.restore_backup(manifest, safety_backup=False)
        self.assertEqual(event.status, RestoreStatusChoices.STATUS_RECOVERY_REQUIRED)
        self.assertEqual(event.row_count_mismatches['dcim_site'], {'expected': 999, 'actual': 25})
        conds = {c.name: c for c in health.conditions(check_storage=False)}
        self.assertEqual(conds['RecoveryRequired'].status, health.ERROR)


class HealthTest(EngineTestBase):

    def test_schedule_without_queued_job(self):
        from core.models import Job
        self.configure(database_backup_interval=60, full_backup_interval=None)
        conds = {c.name: c for c in health.conditions(check_storage=False)}
        self.assertEqual(conds['ScheduleConfigured'].status, health.WARNING)
        self.assertIn('Restart netbox-rq', conds['ScheduleConfigured'].message)
        Job.objects.create(name='Scheduled database backup', status='scheduled', interval=60,
                           job_id='00000000-0000-0000-0000-000000000002')
        conds = {c.name: c for c in health.conditions(check_storage=False)}
        self.assertEqual(conds['ScheduleConfigured'].status, health.OK)

    def test_conditions(self):
        conds = {c.name: c for c in health.conditions()}
        self.assertEqual(conds['BackupAvailable'].status, health.ERROR)
        self.assertEqual(conds['StorageReachable'].status, health.OK)
        create_backup(kind='database', trigger='cli')
        conds = {c.name: c for c in health.conditions()}
        self.assertEqual(conds['BackupAvailable'].status, health.OK)
        self.assertEqual(conds['FullBackupAvailable'].status, health.WARNING)
        self.assertEqual(conds['RestoreTested'].status, health.WARNING)

    def test_shared_storage_warning(self):
        get_storage().put_bytes('another-instance/x/manifest.json', b'{}')
        conds = {c.name: c for c in health.conditions()}
        self.assertEqual(conds['StorageIsolated'].status, health.WARNING)

    def test_stale_backup(self):
        self.configure(database_backup_interval=60)
        backup = create_backup(kind='database', trigger='cli')
        Backup.objects.filter(pk=backup.pk).update(started_at=timezone.now() - timedelta(hours=5),
                                                   completed_at=timezone.now() - timedelta(hours=5))
        conds = {c.name: c for c in health.conditions()}
        self.assertEqual(conds['BackupAvailable'].status, health.WARNING)

    def test_unreachable_storage(self):
        self.configure(storage={'backend': 'local', 'path': '/proc/forbidden'})
        conds = {c.name: c for c in health.conditions()}
        self.assertEqual(conds['StorageReachable'].status, health.ERROR)
