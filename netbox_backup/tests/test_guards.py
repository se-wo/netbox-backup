"""
Crash and guard tests. A crash is simulated by an exception right after a step was journaled (the worst moment:
the step happened, the process died before the next one). Re-running must succeed, and before the switch the live
database must be unchanged. Guards: everything that empties a database refuses one without the slot marker or with
other sessions, and an in-place load stops when its safety set does not verify. Prune after a restore: the sets
a restored database does not know as verified keep counting toward retention, and the sets an open restore needs
are held whatever retention says.
"""
from datetime import timedelta
from unittest import mock

from dcim.models import Site
from django.db import connection
from django.utils import timezone

from netbox_backup import conf
from netbox_backup.choices import RestoreStrategyChoices, SetStatusChoices, VerifyLevelChoices
from netbox_backup.core import layout, pg, sets
from netbox_backup.engine import journal as journal_mod
from netbox_backup.engine import prune
from netbox_backup.engine.backup import BackupError, run_backup
from netbox_backup.engine.restore import RestoreError, Restorer, init_slot
from netbox_backup.models import BackupSet, RestoreRun
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


KEEP_ONE = {'keep_last': 1, 'keep_daily': 0, 'keep_weekly': 0, 'keep_monthly': 0, 'min_age': '0h'}


class PruneAfterRestoreTest(RestoreTestBase):
    """
    A restored database's catalog is as old as its set: it calls that set "running", and has never heard of the
    sets taken after it, the safety set among them. Prune used to see all of them as unverified and delete them
    a day later, with the rollback window still open.
    """

    def in_storage(self):
        backend = get_backend(conf.get_config().targets['primary'])
        return {s.set_id for s in sets.list_sets(backend, conf.instance_id(), include_incomplete=False)}

    def restore(self):
        restored = run_backup('default')
        newer = run_backup('default')
        journal = Restorer(user='tester').load(restored.set_id, strategy=RestoreStrategyChoices.IN_PLACE,
                                               confirm=self.live())
        connection.close()
        return restored.set_id, newer.set_id, journal

    def test_sets_found_by_the_sync_after_a_restore_are_verified_and_kept(self):
        restored, newer, journal = self.restore()
        safety = journal['safety_set_id']
        for set_id in (restored, newer, safety):
            row = BackupSet.objects.get(set_id=set_id)
            self.assertEqual(row.status, SetStatusChoices.VERIFIED, set_id)
            self.assertEqual(row.verify_level, VerifyLevelChoices.INTEGRITY, set_id)
            self.assertEqual(row.copies.get().verify_level, VerifyLevelChoices.INTEGRITY, set_id)
        post = run_backup('default')  # the post-restore backup finish enqueues; it is followed by a prune
        config = conf.get_config()
        # A day later every set is past min_age, and the rollback window (rollback_days: 2) is still open
        report = prune.prune_target(config, config.target('primary'), now=timezone.now() + timedelta(hours=25))
        self.assertEqual(report.deleted, [])
        self.assertEqual(report.held, {})
        self.assertEqual(self.in_storage(), {restored, newer, safety, post.set_id})
        rolled = Restorer(user='tester').rollback(journal.restore_id, confirm=self.live())
        self.assertEqual(rolled['status'], 'rolled-back')

    def test_sets_of_an_open_restore_are_held_whatever_retention_says(self):
        self.configure(policies={'default': {'retention': KEEP_ONE}})
        restored, newer, journal = self.restore()
        safety = journal['safety_set_id']
        post = run_backup('default')
        report = prune.prune()[0]
        # Retention keeps the newest set only; the restore keeps its safety set and the set it loaded
        self.assertEqual(report.deleted, [newer])
        self.assertEqual(set(report.needed_by_restore), {restored, safety})
        self.assertIn(journal.restore_id, report.needed_by_restore[safety])
        self.assertEqual(self.in_storage(), {restored, safety, post.set_id})

        # The journal is gone (another host, a lost spool, the target unreachable): the catalog row still holds them
        with mock.patch.object(journal_mod, 'list_journals', return_value=[]):
            self.assertEqual(set(journal_mod.protected_sets()), {restored, safety})
            # ... but not once the rollback window has closed
            self.assertEqual(journal_mod.protected_sets(now=timezone.now() + timedelta(days=3)), {})
        # A row older than its journal (brought back by a restored database) does not outvote the journal
        RestoreRun.objects.filter(restore_id=journal.restore_id).update(status='loading')
        self.assertEqual(journal_mod.protected_sets(now=timezone.now() + timedelta(days=3)), {})
        RestoreRun.objects.filter(restore_id=journal.restore_id).update(status='finished')

        rolled = Restorer(user='tester').rollback(journal.restore_id, confirm=self.live())
        self.assertEqual(rolled['status'], 'rolled-back')
        connection.close()
        # Rolled back: nothing needs them any more, and the catalog (now the safety set's) was synced again
        report = prune.prune()[0]
        self.assertEqual(report.needed_by_restore, {})
        self.assertEqual(self.in_storage(), {post.set_id})

    def test_a_failed_load_keeps_its_sets(self):
        self.configure(policies={'default': {'retention': KEEP_ONE}})
        restored = run_backup('default')
        with kill_after('safety'), self.assertRaises(Killed):
            Restorer().load(restored.set_id, strategy=RestoreStrategyChoices.IN_PLACE, confirm=self.live())
        connection.close()
        journal = journal_mod.load()
        self.assertEqual(journal['status'], 'failed')
        safety = journal['safety_set_id']
        self.assertEqual(set(journal_mod.protected_sets()), {restored.set_id, safety})
        # The same journal written by another install (a staging copy restoring from our sets) pins nothing here
        foreign = journal_mod.Journal({**journal.data, 'restored_by': '11111111-2222-4333-8444-555555555555'},
                                      journal.target_name)
        with mock.patch.object(journal_mod, 'list_journals', return_value=[foreign]):
            self.assertEqual(journal_mod.protected_sets(), {})
        report = prune.prune()[0]
        # Retention alone would keep only the safety set, the newest; the rerun needs the set it was loading
        self.assertEqual(report.deleted, [])
        self.assertEqual(set(report.needed_by_restore), {restored.set_id})
        self.assertEqual(self.in_storage(), {restored.set_id, safety})
        Restorer().rollback(journal.restore_id, confirm=self.live())
        connection.close()
        self.assertEqual(Site.objects.count(), 20)
        self.assertEqual(journal_mod.protected_sets(), {})


class SlotPruneTest(SlotTestBase):

    def test_a_loaded_slot_keeps_its_set(self):
        self.configure(databases=[self.live(), SLOT], policies={'default': {'retention': KEEP_ONE}})
        init_slot(SLOT)
        loaded = run_backup('default')
        journal = Restorer().load(loaded.set_id, strategy=RestoreStrategyChoices.SLOT, headroom_confirmed=True)
        connection.close()
        newer = run_backup('default')
        report = prune.prune()[0]
        self.assertEqual(report.deleted, [])
        self.assertEqual(set(report.needed_by_restore), {loaded.set_id})
        # Never switched: rolling back releases the set
        Restorer().rollback(journal.restore_id)
        report = prune.prune()[0]
        self.assertEqual(report.deleted, [loaded.set_id])
        self.assertEqual(report.keep.keys(), {newer.set_id})
