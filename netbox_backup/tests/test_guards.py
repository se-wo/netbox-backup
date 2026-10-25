"""
Crash and guard tests. A crash is simulated by an exception right after a step was journaled (the worst moment:
the step happened, the process died before the next one). Re-running must succeed, and before the switch the live
database must be unchanged. Guards: everything that empties a database refuses one without the slot marker or with
other sessions, and an in-place load stops when its safety set does not verify.
"""
from unittest import mock

from dcim.models import Site
from django.db import connection

from netbox_backup import conf
from netbox_backup.choices import RestoreStrategyChoices
from netbox_backup.core import layout, pg, sets
from netbox_backup.engine import journal as journal_mod
from netbox_backup.engine.backup import BackupError, run_backup
from netbox_backup.engine.restore import RestoreError, Restorer, init_slot
from netbox_backup.storage import get_backend
from netbox_backup.storage.local import LocalBackend

from .test_restore import SLOT, RestoreTestBase, SlotTestBase
from .utils import INSTANCE_ID


class Killed(BaseException):
    """
    Stands in for SIGKILL: not an Exception, so no handler that catches Exception can tidy up after it.
    """


def kill_after(step_name):
    original = journal_mod.Journal.step

    def step(self, name, *args, **kwargs):
        original(self, name, *args, **kwargs)
        if name == step_name:
            raise Killed(name)
    return mock.patch.object(journal_mod.Journal, 'step', step)


class BackupCrashTest(RestoreTestBase):

    def complete_sets(self):
        return sets.list_sets(get_backend(conf.get_config().targets['primary']), INSTANCE_ID,
                              include_incomplete=False)

    def test_kill_at_every_upload_step_leaves_no_valid_set(self):
        from netbox_backup.core import pipeline
        upload = pipeline.upload_component
        for n in range(4):  # database, media, scripts, config
            calls = {'n': 0}

            def dying(*args, **kwargs):
                if calls['n'] == n:
                    raise Killed(f'upload {n}')
                calls['n'] += 1
                return upload(*args, **kwargs)
            with mock.patch('netbox_backup.core.pipeline.upload_component', dying), self.assertRaises(Killed):
                run_backup('default')
        put = LocalBackend.put_bytes
        for name in (layout.MANIFEST, layout.COMPLETE):
            def dying_put(backend, key, data, name=name):
                if key.endswith(name):
                    raise Killed(name)
                return put(backend, key, data)
            with mock.patch.object(LocalBackend, 'put_bytes', dying_put), self.assertRaises(Killed):
                run_backup('default')
        self.assertEqual(self.complete_sets(), [], 'no killed run may leave a complete set behind')
        backup_set = run_backup('default')
        self.assertEqual([s.set_id for s in self.complete_sets()], [backup_set.set_id])


class SlotCrashTest(SlotTestBase):

    def test_kill_at_every_load_step_then_rerun(self):
        init_slot(SLOT)
        backup_set = run_backup('default')
        Site.objects.filter(name='Site 0').delete()
        for step in ('plan', 'fetch', 'empty', 'pg_restore', 'baseline', 'loaded'):
            with self.subTest(step=step), kill_after(step), self.assertRaises(Killed):
                Restorer().load(backup_set.set_id, strategy=RestoreStrategyChoices.SLOT, headroom_confirmed=True)
            connection.close()
            self.assertEqual(Site.objects.count(), 19, f'killed after {step}: the live database must be untouched')
            with conf.database().connect() as c:
                self.assertTrue(pg.has_slot_marker(c, SLOT))
        journal = Restorer().load(backup_set.set_id, strategy=RestoreStrategyChoices.SLOT, headroom_confirmed=True)
        self.assertEqual(journal['status'], 'loaded')

        # finish is re-runnable too: killed after migrate, a second finish completes
        self.switch(SLOT)
        with kill_after('migrate'), self.assertRaises(Killed):
            Restorer().finish(journal.restore_id)
        finished = Restorer().finish(journal.restore_id)
        self.assertEqual(finished['status'], 'finished')
        self.assertEqual(Site.objects.count(), 20)
        self.switch(self.original_name)
        with conf.database().connect() as c:
            pg.set_slot_marker(c, self.original_name, False)


class SlotGuardTest(SlotTestBase):

    def test_drill_and_load_refuse_an_unmarked_slot_or_other_sessions(self):
        backup_set = run_backup('default')
        with self.assertRaisesRegex(RestoreError, 'marker'):
            Restorer().drill(backup_set.set_id, headroom_confirmed=True)
        init_slot(SLOT)
        blocker = conf.database(dbname=SLOT).connect()
        try:
            with self.assertRaisesRegex(RestoreError, 'session'):
                Restorer().drill(backup_set.set_id, headroom_confirmed=True)
        finally:
            blocker.close()

    def test_finalize_refuses_an_unmarked_database_or_other_sessions(self):
        init_slot(SLOT)
        backup_set = run_backup('default')
        journal = Restorer().load(backup_set.set_id, strategy=RestoreStrategyChoices.SLOT, headroom_confirmed=True)
        self.switch(SLOT)
        Restorer().finish(journal.restore_id)
        self.switch(self.original_name)
        Restorer().rollback(journal.restore_id)  # finalize now empties the slot, which holds the restored copy
        with conf.database().connect() as c:
            pg.set_slot_marker(c, SLOT, False)
        with self.assertRaisesRegex(RestoreError, 'no slot marker'):
            Restorer().finalize(journal.restore_id, force=True)
        with conf.database().connect() as c:
            pg.set_slot_marker(c, SLOT, True)
        blocker = conf.database(dbname=SLOT).connect()
        try:
            with self.assertRaisesRegex(RestoreError, 'other sessions'):
                Restorer().finalize(journal.restore_id, force=True)
        finally:
            blocker.close()
        with conf.database(dbname=SLOT).connect() as c:
            self.assertFalse(pg.is_empty(c), 'a refused finalize must not empty anything')
        Restorer().finalize(journal.restore_id, force=True)
        with conf.database(dbname=SLOT).connect() as c:
            self.assertTrue(pg.is_empty(c))


class InPlaceGuardTest(RestoreTestBase):

    def test_safety_set_that_fails_verification_stops_the_load(self):
        backup_set = run_backup('default')
        Site.objects.filter(name='Site 0').delete()
        with mock.patch('netbox_backup.engine.catalog.mark_level', return_value=False), \
                self.assertRaisesRegex(BackupError, 'Level-1 check failed'):
            Restorer().load(backup_set.set_id, strategy=RestoreStrategyChoices.IN_PLACE, confirm=self.live())
        connection.close()
        self.assertEqual(Site.objects.count(), 19, 'nothing may be deleted without a verified safety set')
        crashed = next(j for j in journal_mod.list_journals() if j['set_id'] == backup_set.set_id)
        self.assertFalse(crashed.get('emptied'))

    def test_kill_during_in_place_load_is_recoverable_with_rollback(self):
        backup_set = run_backup('default')
        Site.objects.create(name='after the backup', slug='after')
        with kill_after('empty'), self.assertRaises(Killed):
            Restorer().load(backup_set.set_id, strategy=RestoreStrategyChoices.IN_PLACE, confirm=self.live())
        connection.close()
        killed = next(j for j in journal_mod.list_journals() if j['set_id'] == backup_set.set_id)
        self.assertTrue(killed['emptied'])
        # The process died without recording a failure; mark it the way restore status would show it
        killed.update(status='failed')
        killed.save('killed')
        rolled = Restorer().rollback(killed.restore_id, confirm=self.live())
        self.assertEqual(rolled['status'], 'rolled-back')
        self.assertEqual(Site.objects.count(), 21, 'rollback restores the database as it was before the load')
