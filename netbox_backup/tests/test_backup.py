"""
Backups against the real test database: these run pg_dump and gpg, so they use TransactionTestCase (data must be
committed to be visible to pg_dump).
"""
import base64
import json
import os
from datetime import UTC, datetime, timedelta
from unittest import mock

from dcim.models import Site
from django.conf import settings
from django.core.files.base import ContentFile
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from netbox_backup import conf
from netbox_backup.choices import CopyStatusChoices, SetStatusChoices, VerifyLevelChoices
from netbox_backup.core import layout, manifest, sets
from netbox_backup.core.config import RetentionConfig
from netbox_backup.core.gpg import Keyring
from netbox_backup.engine import catalog, prune, retention, scheduler
from netbox_backup.engine.backup import BackupError, run_backup
from netbox_backup.engine.locks import LockHeld, advisory_lock
from netbox_backup.models import BackupPolicy, BackupSet, StorageTarget
from netbox_backup.storage import get_backend

from .utils import PluginConfigMixin

PNG_1PX = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
)


def attach_image(site, name):
    from extras.models import ImageAttachment
    attachment = ImageAttachment(parent=site, name=name)
    attachment.image.save(f'{name}.png', ContentFile(PNG_1PX), save=False)
    attachment.image_height = attachment.image_width = 1
    attachment.save()
    return attachment


class MediaMixin:

    def setUp(self):
        super().setUp()
        self.media = os.path.join(self.tmp, 'media')
        os.makedirs(os.path.join(self.media, 'image-attachments'))
        with open(os.path.join(self.media, 'image-attachments', 'rack.png'), 'wb') as f:
            f.write(b'\x89PNG fake')
        self.media_override = self.settings(STORAGES={
            **settings.STORAGES,
            'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage',
                        'OPTIONS': {'location': self.media}},
        })
        self.media_override.enable()
        self._reset_storages()

    def tearDown(self):
        self.media_override.disable()
        self._reset_storages()
        super().tearDown()

    @staticmethod
    def _reset_storages():
        from django.core.files.storage import storages
        storages._storages.pop('default', None)


class BackupTest(MediaMixin, PluginConfigMixin, TransactionTestCase):

    def setUp(self):
        super().setUp()
        Site.objects.bulk_create([Site(name=f'Site {i}', slug=f'site-{i}') for i in range(25)])

    def backend(self):
        return get_backend(conf.get_config().targets['primary'])

    def test_full_set(self):
        backup_set = run_backup('default', trigger='cli', created_by='tester')
        self.assertEqual(backup_set.status, SetStatusChoices.VERIFIED, backup_set.error)
        self.assertEqual(backup_set.verify_level, VerifyLevelChoices.INTEGRITY)
        self.assertEqual(set(backup_set.components), {'database', 'media', 'scripts', 'config'})
        self.assertEqual(backup_set.baseline['tables']['public.dcim_site'], 25)
        self.assertEqual(backup_set.components['media']['files'], 1)
        self.assertTrue(backup_set.components['database']['dump_checked'])
        copy = backup_set.copies.get()
        self.assertEqual(copy.status, CopyStatusChoices.VERIFIED)
        self.assertEqual(copy.checksum_mode, 'size')

        backend = self.backend()
        listing = sets.resolve(backend, conf.instance_id())
        self.assertEqual(listing.set_id, backup_set.set_id)
        self.assertEqual(set(listing.objects), {
            'manifest.json', '_COMPLETE', 'db.pgdump.enc', 'media.tar.zst.enc', 'scripts.tar.zst.enc',
            'config.json.enc',
        })
        self.assertIsNotNone(sets.read_instance(backend, conf.instance_id()))

        # Header readable without a key; body only with an identity
        loaded = sets.load_manifest(backend, conf.instance_id(), backup_set.set_id)
        self.assertEqual(loaded.header['policy'], 'default')
        self.assertNotIn('baseline', loaded.header)
        with Keyring(conf.get_config().crypto, identities=True) as keyring:
            manifest.verify(loaded, keyring)
            body = manifest.decrypt_body(loaded, keyring)
            self.assertEqual(body['baseline']['tables']['public.dcim_site'], 25)
            self.assertIn('dcim.0001_squashed', ' '.join(body['migrations']))
            spool = conf.spool_dir('test')
            paths = sets.fetch_and_decrypt(backend, keyring, loaded, ['config', 'media'], spool)
            config_doc = json.loads(open(paths['config'], 'rb').read())
            self.assertTrue(any(p.endswith('.py') for p in config_doc['files']))

    def test_database_only_policy(self):
        self.configure(policies={'db': {'components': ['database'], 'schedule': 'hourly'}})
        backup_set = run_backup('db')
        self.assertEqual(list(backup_set.components), ['database'])

    def test_media_missing_warns(self):
        attachment = attach_image(Site.objects.first(), 'gone')
        os.unlink(os.path.join(self.media, attachment.image.name))
        backup_set = run_backup('default')
        self.assertEqual(backup_set.status, SetStatusChoices.VERIFIED)
        self.assertEqual(backup_set.components['media']['missing'], 1)
        self.assertTrue(any('missing' in w for w in backup_set.warnings))

    def test_media_volume_not_mounted_fails(self):
        attach_image(Site.objects.first(), 'a')
        for root, _, files in os.walk(self.media):
            for name in files:
                os.unlink(os.path.join(root, name))
        with self.assertRaisesRegex(BackupError, 'mounted'):
            run_backup('default')
        self.assertEqual(BackupSet.objects.get().status, SetStatusChoices.FAILED)

    def test_failed_dump_never_completes(self):
        with mock.patch('netbox_backup.core.pg.dump_args', return_value=['false']):
            with self.assertRaises(Exception):
                run_backup('default')
        backup_set = BackupSet.objects.get()
        self.assertEqual(backup_set.status, SetStatusChoices.FAILED)
        self.assertEqual(sets.list_sets(self.backend(), conf.instance_id(), include_incomplete=False), [])

    def test_dump_check_catches_count_mismatch(self):
        real = __import__('netbox_backup.core.pg', fromlist=['baseline']).baseline

        def wrong(cursor):
            result = real(cursor)
            result['tables']['public.dcim_site'] += 1
            return result
        with mock.patch('netbox_backup.core.pg.baseline', side_effect=wrong):
            with self.assertRaisesRegex(Exception, 'Dump check'):
                run_backup('default')

    def test_no_crypto_no_backup(self):
        self.configure(crypto={})
        with self.assertRaisesRegex(BackupError, 'no unencrypted mode'):
            run_backup('default')

    def test_lock_prevents_concurrent_runs(self):
        with advisory_lock('policy:default'):
            with self.assertRaises(LockHeld):
                run_backup('default')

    def test_refuses_marked_database(self):
        from netbox_backup.core import pg
        db = conf.database()
        with db.connect() as c:
            pg.set_slot_marker(c, db.name, True)
        try:
            with self.assertRaisesRegex(BackupError, 'slot marker'):
                run_backup('default')
        finally:
            with db.connect() as c:
                pg.set_slot_marker(c, db.name, False)

    def test_catalog_sync_rebuilds_from_storage(self):
        backup_set = run_backup('default')
        BackupSet.objects.all().delete()
        stats = catalog.sync()
        self.assertEqual(stats['created'], 1)
        rebuilt = BackupSet.objects.get(set_id=backup_set.set_id)
        self.assertEqual(rebuilt.policy.name, 'default')
        self.assertEqual(rebuilt.size_bytes, sum(o['size'] for o in sets.load_manifest(
            self.backend(), conf.instance_id(), backup_set.set_id).objects.values()))

    def test_prune(self):
        self.configure(policies={'default': {'components': ['database'],
                                             'retention': {'keep_last': 1, 'keep_daily': 0, 'keep_weekly': 0,
                                                           'keep_monthly': 0, 'min_age': '0h'}}})
        first = run_backup('default')
        second = run_backup('default')
        dry = prune.prune(dry_run=True)[0]
        self.assertEqual(dry.delete, [first.set_id])
        self.assertEqual(dry.deleted, [])
        report = prune.prune()[0]
        self.assertEqual(report.deleted, [first.set_id])
        self.assertEqual([s.set_id for s in sets.list_sets(self.backend(), conf.instance_id())], [second.set_id])
        first.refresh_from_db()
        self.assertEqual(first.status, SetStatusChoices.EXPIRED)

    def test_prune_without_credential_only_reports(self):
        self.configure(
            targets={'primary': {'backend': 'local', 'path': self.storage_path}},
            policies={'default': {'components': ['database'], 'retention': {'keep_last': 1, 'keep_daily': 0,
                                  'keep_weekly': 0, 'keep_monthly': 0, 'min_age': '0h'}}},
        )
        run_backup('default')
        run_backup('default')
        report = prune.prune()[0]
        self.assertFalse(report.can_delete)
        self.assertEqual(len(report.delete), 1)
        self.assertEqual(len(sets.list_sets(self.backend(), conf.instance_id())), 2)


class SchedulerTest(PluginConfigMixin, TestCase):

    def test_tick_schedules_once_per_slot(self):
        self.configure(policies={'nightly': {'schedule': 'daily', 'time_zone': 'UTC'}})
        with mock.patch('netbox_backup.jobs.BackupJob.enqueue') as enqueue, \
                mock.patch('netbox_backup.jobs.PruneJob.enqueue'):
            t0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
            scheduler.tick(now=t0)  # first tick only records the slot
            enqueue.assert_not_called()
            scheduler.tick(now=t0 + timedelta(hours=10))  # 22:00, no new slot
            enqueue.assert_not_called()
            scheduler.tick(now=t0 + timedelta(days=3))  # three missed slots run once
            self.assertEqual(enqueue.call_count, 1)
            self.assertEqual(enqueue.call_args.kwargs['policy'], 'nightly')
            self.assertEqual(enqueue.call_args.kwargs['queue_name'], 'netbox_backup.netbox_backup')
            scheduler.tick(now=t0 + timedelta(days=3, minutes=5))
            self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(BackupPolicy.objects.get().primary_target, StorageTarget.objects.get(name='primary'))
        self.assertIsNotNone(scheduler.last_tick())

    def test_worst_case_loss_and_next_runs(self):
        policy = conf.get_config().policies['default']
        self.assertEqual(scheduler.worst_case_loss(policy), timedelta(days=1))
        runs = scheduler.next_runs(policy, now=datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
        self.assertEqual(len(runs), 5)

    def test_overrun_marked_failed(self):
        catalog.sync_static()
        policy = BackupPolicy.objects.get()
        stuck = BackupSet.objects.create(set_id=layout.new_ulid(), instance_id='x', policy=policy, trigger='cli',
                                         status=SetStatusChoices.RUNNING, started=timezone.now() - timedelta(hours=5))
        self.assertEqual(scheduler.fail_overruns(), 1)
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, SetStatusChoices.FAILED)


class RetentionTest(TestCase):

    def candidates(self, days, verified=True):
        now = datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
        return now, [retention.Candidate(set_id=f'{d:03d}', created=now - timedelta(days=d), verified=verified)
                     for d in days]

    def test_gfs(self):
        now, cands = self.candidates(range(0, 400))
        rules = RetentionConfig(keep_last=3, keep_daily=7, keep_weekly=4, keep_monthly=3, keep_yearly=1,
                                min_age=timedelta(hours=1))
        keep, pruned = retention.plan(cands, rules, now)
        self.assertIn('000', keep)
        self.assertEqual(len([k for k, v in keep.items() if 'daily' in v]), 7)
        self.assertEqual(len([k for k, v in keep.items() if 'monthly' in v]), 3)
        self.assertGreater(len(pruned), 350)
        self.assertNotIn('000', pruned)

    def test_unverified_sets_do_not_count(self):
        now, cands = self.candidates([0, 1, 2])
        cands[0].verified = False
        rules = RetentionConfig(keep_last=1, keep_daily=0, keep_weekly=0, keep_monthly=0, min_age=timedelta(0))
        keep, pruned = retention.plan(cands, rules, now)
        self.assertEqual(set(keep), {'001'})
        self.assertEqual(pruned, ['000', '002'])

    def test_min_age_and_newest_verified_always_kept(self):
        now, cands = self.candidates([0, 10])
        rules = RetentionConfig(keep_last=0, keep_daily=0, keep_weekly=0, keep_monthly=0,
                                min_age=timedelta(hours=24))
        keep, _ = retention.plan(cands, rules, now)
        self.assertEqual(set(keep), {'000'})
        self.assertIn('min_age', keep['000'])
        self.assertIn('newest verified', keep['000'])
