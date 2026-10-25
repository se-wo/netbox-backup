"""
Restore round trips against the real test database: in place, and through a marked restore slot with a switch.
"""
import io
import os
import tarfile

import psycopg
from dcim.models import Site
from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase

from netbox_backup import conf
from netbox_backup.choices import RestoreStrategyChoices, TriggerChoices
from netbox_backup.core import pg
from netbox_backup.engine import journal as journal_mod
from netbox_backup.engine import media
from netbox_backup.engine.backup import run_backup
from netbox_backup.engine.restore import RestoreError, Restorer, build_plan, init_slot
from netbox_backup.models import BackupSet, RestoreRun

from .test_backup import MediaMixin
from .utils import PluginConfigMixin

SLOT = 'test_netbox_nbb_slot'


def admin():
    db = conf.database()
    return psycopg.connect(password=db.password, autocommit=True, **{**db.params, 'dbname': 'postgres'})


class RestoreTestBase(MediaMixin, PluginConfigMixin, TransactionTestCase):

    def setUp(self):
        super().setUp()
        Site.objects.bulk_create([Site(name=f'Site {i}', slug=f'site-{i}') for i in range(20)])

    def live(self):
        return settings.DATABASES['default']['NAME']

    def write_media(self, name, data=b'x'):
        path = os.path.join(self.media, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)


class InPlaceRestoreTest(RestoreTestBase):

    def test_round_trip_and_rollback(self):
        backup_set = run_backup('default')
        Site.objects.filter(name__in=[f'Site {i}' for i in range(5)]).delete()
        self.write_media('image-attachments/rack.png', b'changed')
        self.write_media('stray.txt')
        restorer = Restorer(user='tester')

        plan = build_plan(backup_set.set_id)
        self.assertEqual(plan.strategy, RestoreStrategyChoices.IN_PLACE, plan.reason)
        self.assertTrue(plan.ok, plan.aborts)
        with self.assertRaisesRegex(RestoreError, '--confirm'):
            restorer.load(backup_set.set_id, strategy=RestoreStrategyChoices.IN_PLACE)
        with self.assertRaisesRegex(RestoreError, 'plan recommends'):
            restorer.load(backup_set.set_id, strategy=RestoreStrategyChoices.SLOT)

        journal = restorer.load(backup_set.set_id, strategy=RestoreStrategyChoices.IN_PLACE, confirm=self.live())
        self.assertEqual(journal['status'], 'finished', journal.data)
        connection.close()
        self.assertEqual(Site.objects.count(), 20)
        with open(os.path.join(self.media, 'image-attachments/rack.png'), 'rb') as f:
            self.assertEqual(f.read(), b'\x89PNG fake')
        self.assertFalse(os.path.exists(os.path.join(self.media, 'stray.txt')))
        self.assertTrue(os.path.exists(os.path.join(self.media, media.QUARANTINE, journal.restore_id, 'stray.txt')))
        self.assertTrue(RestoreRun.objects.filter(restore_id=journal.restore_id, status='finished').exists())
        safety = BackupSet.objects.get(set_id=journal['safety_set_id'])
        # Rebuilt by catalog sync from the signed header alone, so the baseline (encrypted body) is not there
        self.assertEqual(safety.trigger, TriggerChoices.SAFETY)
        step_names = [s['step'] for s in journal['steps']]
        for step in ('safety', 'empty', 'pg_restore', 'baseline', 'migrate', 'jobs', 'redis', 'sessions'):
            self.assertIn(step, step_names)

        # Journal is in storage too, for a fresh container
        os.unlink(journal.local_path())
        self.assertEqual(journal_mod.load(journal.restore_id)['status'], 'finished')

        with self.assertRaisesRegex(RestoreError, '--confirm'):
            restorer.rollback(journal.restore_id)
        rolled = restorer.rollback(journal.restore_id, confirm=self.live())
        self.assertEqual(rolled['status'], 'rolled-back')
        connection.close()
        self.assertEqual(Site.objects.count(), 15)
        with open(os.path.join(self.media, 'image-attachments/rack.png'), 'rb') as f:
            self.assertEqual(f.read(), b'changed')

        finalized = restorer.finalize(journal.restore_id, force=True)
        self.assertEqual(finalized['status'], 'finalized')

    def test_resurrected_identities_reported_and_revoked(self):
        from users.models import User
        User.objects.create_user(username='leaver', password='x')
        backup_set = run_backup('default')
        User.objects.filter(username='leaver').update(is_active=False)
        journal = Restorer().load(backup_set.set_id, strategy=RestoreStrategyChoices.IN_PLACE, confirm=self.live(),
                                  skip_safety=True, revoke_resurrected=True)
        self.assertIn('leaver', journal['resurrected']['users'])
        connection.close()
        self.assertFalse(User.objects.get(username='leaver').is_active)
        self.assertTrue(any('--skip-safety' in w for w in journal['warnings']))

    def test_restore_media_alone(self):
        backup_set = run_backup('default', components=None)
        self.write_media('image-attachments/rack.png', b'changed')
        journal = Restorer().restore_media(backup_set.set_id)
        self.assertEqual(journal['status'], 'finished')
        with open(os.path.join(self.media, 'image-attachments/rack.png'), 'rb') as f:
            self.assertEqual(f.read(), b'\x89PNG fake')
        self.assertEqual(Site.objects.count(), 20)


class SlotRestoreTest(RestoreTestBase):

    def setUp(self):
        super().setUp()
        with admin() as c:
            c.execute(f'DROP DATABASE IF EXISTS {SLOT}')
            c.execute(f'CREATE DATABASE {SLOT} OWNER {conf.database().params["user"]}')
        self.configure(databases=[self.live(), SLOT])
        self.original_name = self.live()

    def tearDown(self):
        connection.close()
        settings.DATABASES['default']['NAME'] = self.original_name
        connection.settings_dict['NAME'] = self.original_name
        with admin() as c:
            c.execute(f'DROP DATABASE IF EXISTS {SLOT} WITH (FORCE)')
        super().tearDown()

    def switch(self, name):
        connection.close()
        settings.DATABASES['default']['NAME'] = name
        connection.settings_dict['NAME'] = name

    def test_slot_init_guards(self):
        with self.assertRaisesRegex(RestoreError, 'live database'):
            init_slot(self.live())
        with conf.database(dbname=SLOT).connect() as c:
            c.execute('CREATE TABLE junk (id int)')
        with self.assertRaisesRegex(RestoreError, 'not empty'):
            init_slot(SLOT)
        with conf.database(dbname=SLOT).connect() as c:
            c.execute('DROP TABLE junk')
        init_slot(SLOT)
        with conf.database().connect() as c:
            self.assertTrue(pg.has_slot_marker(c, SLOT))

    def test_unmarked_slot_not_recommended(self):
        backup_set = run_backup('default')
        plan = build_plan(backup_set.set_id, headroom_confirmed=True)
        self.assertEqual(plan.strategy, RestoreStrategyChoices.IN_PLACE)
        self.assertIn('marker', plan.reason)

    def test_round_trip_switch_rollback_finalize(self):
        init_slot(SLOT)
        backup_set = run_backup('default')
        Site.objects.filter(name='Site 0').delete()
        restorer = Restorer(user='tester')

        plan = build_plan(backup_set.set_id)
        self.assertEqual(plan.strategy, RestoreStrategyChoices.SLOT)
        self.assertFalse(plan.ok)  # server headroom not confirmed
        plan = build_plan(backup_set.set_id, headroom_confirmed=True)
        self.assertTrue(plan.ok, plan.aborts)

        # Other sessions on the slot block the load
        blocker = conf.database(dbname=SLOT).connect()
        try:
            with self.assertRaises(RestoreError):
                restorer.load(backup_set.set_id, strategy=RestoreStrategyChoices.SLOT, headroom_confirmed=True)
        finally:
            blocker.close()

        journal = restorer.load(backup_set.set_id, strategy=RestoreStrategyChoices.SLOT, headroom_confirmed=True)
        self.assertEqual(journal['status'], 'loaded')
        connection.close()
        self.assertEqual(Site.objects.count(), 19, 'the live database must be untouched')

        with self.assertRaisesRegex(RestoreError, 'not the restored database'):
            restorer.finish(journal.restore_id)
        self.switch(SLOT)
        finished = restorer.finish(journal.restore_id)
        self.assertEqual(finished['status'], 'finished')
        self.assertEqual(Site.objects.count(), 20)
        with conf.database().connect() as c:
            self.assertFalse(pg.has_slot_marker(c, SLOT))
            self.assertTrue(pg.has_slot_marker(c, self.original_name))

        # Backups refuse to run against a marked database: the old one now carries the marker
        self.switch(self.original_name)
        with self.assertRaisesRegex(Exception, 'slot marker'):
            run_backup('default')
        rolled = restorer.rollback(journal.restore_id)
        self.assertEqual(rolled['status'], 'rolled-back')
        with conf.database().connect() as c:
            self.assertTrue(pg.has_slot_marker(c, SLOT))
            self.assertFalse(pg.has_slot_marker(c, self.original_name))
        self.assertEqual(Site.objects.count(), 19)

        with self.assertRaisesRegex(RestoreError, 'Rollback window'):
            restorer.finalize(journal.restore_id)
        restorer.finalize(journal.restore_id, force=True)
        with conf.database(dbname=SLOT).connect() as c:
            self.assertTrue(pg.is_empty(c))

    def test_drill(self):
        init_slot(SLOT)
        backup_set = run_backup('default')
        journal = Restorer().drill(backup_set.set_id, headroom_confirmed=True)
        self.assertEqual(journal['status'], 'finished')
        backup_set.refresh_from_db()
        self.assertEqual(backup_set.verify_level, 2)
        self.assertIsNotNone(backup_set.qa_passed_at)
        with conf.database(dbname=SLOT).connect() as c:
            self.assertTrue(pg.is_empty(c))


class ArchiveSafetyTest(TransactionTestCase):

    def make_archive(self, path, members):
        import zstandard
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as tar:
            for name, kind in members:
                info = tarfile.TarInfo(name)
                if kind == 'symlink':
                    info.type = tarfile.SYMTYPE
                    info.linkname = '/etc/passwd'
                    tar.addfile(info)
                else:
                    info.size = 1
                    tar.addfile(info, io.BytesIO(b'x'))
        with open(path, 'wb') as f:
            f.write(zstandard.ZstdCompressor().compress(buffer.getvalue()))

    def test_escaping_members_rejected(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        for members in ([('../evil', 'file')], [('/abs', 'file')], [('link', 'symlink')],
                        [(f'{media.QUARANTINE}/x', 'file')]):
            path = os.path.join(tmp, 'a.tar.zst')
            self.make_archive(path, members)
            with self.assertRaises(Exception):
                media.archive_members(path)
