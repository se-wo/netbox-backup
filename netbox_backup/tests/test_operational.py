"""
The operational rules settled in M1: locks behind poolers, overruns, server-side encryption that hides checksums,
the disaster-recovery definition of "empty" and reruns, skipped runs, and the generated instance_id.
"""
import os
import uuid
from datetime import timedelta
from unittest import mock

import psycopg
from dcim.models import Site
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from netbox_backup import conf
from netbox_backup.choices import CopyStatusChoices, RestoreStrategyChoices, SetStatusChoices, TriggerChoices
from netbox_backup.core import layout, sets
from netbox_backup.core import pg as pgcore
from netbox_backup.engine import catalog, locks, prune, scheduler
from netbox_backup.engine import journal as journal_mod
from netbox_backup.engine.backup import BackupError, BackupRunner, run_backup
from netbox_backup.engine.restore import RestoreError, Restorer, dr_state
from netbox_backup.models import BackupPolicy, BackupSet, InstanceIdentity, StorageTarget
from netbox_backup.storage import get_backend

from .test_backup import MediaMixin
from .utils import PluginConfigMixin

DB_ONLY = {'default': {'components': ['database']}}


class FakeConnection:
    """
    Answers SELECT pg_backend_pid() from a list, like a transaction-mode pooler handing out server connections.
    """

    def __init__(self, pids):
        self.pids = iter(pids)

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.row = (next(self.pids),)

    def fetchone(self):
        return self.row


class PoolerTest(PluginConfigMixin, TransactionTestCase):

    def test_pid_change_means_pooler(self):
        self.assertIsNone(pgcore.pooled_session(FakeConnection([41, 41, 41])))
        self.assertIn('pooler', pgcore.pooled_session(FakeConnection([41, 57, 41])))
        with conf.database().connect() as conn:  # the test server is a direct session
            self.assertIsNone(pgcore.pooled_session(conn))

    def test_lock_through_pgbouncer_refused(self):
        self.configure(policies=DB_ONLY)
        reason = 'the backend PID changed between transactions (41 -> 57): a connection pooler'
        with mock.patch('netbox_backup.engine.locks.pooled_session', return_value=reason):
            with self.assertRaisesRegex(locks.PooledConnection, 'direct_connection'):
                locks.connect()
            with self.assertRaises(locks.PooledConnection):
                run_backup('default')
        self.assertEqual(BackupSet.objects.get().status, SetStatusChoices.FAILED)
        self.assertEqual(sets.list_sets(get_backend(conf.get_config().targets['primary']), conf.instance_id()), [])

    def test_direct_connection_overrides_the_pooled_dsn(self):
        self.configure(direct_connection={'port': 5432, 'sslmode': 'prefer'})
        db = conf.database()
        self.assertEqual(str(db.params['port']), '5432')
        self.assertEqual(db.params['sslmode'], 'prefer')


class OverrunTest(PluginConfigMixin, TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.configure(policies={'default': {'components': ['database'], 'max_runtime': '1h'}})

    def assert_nothing_visible(self, backup_set):
        backup_set.refresh_from_db()
        self.assertEqual(backup_set.status, SetStatusChoices.FAILED)
        listing = sets.list_sets(get_backend(conf.get_config().targets['primary']), conf.instance_id())
        self.assertEqual(len(listing), 1)
        self.assertFalse(listing[0].complete)
        self.assertNotIn(layout.MANIFEST, listing[0].objects)

    def test_overrun_job_cannot_commit(self):
        original = BackupRunner._upload

        def slow_upload(runner, *args):
            result = original(runner, *args)
            runner.set.started -= timedelta(hours=2)  # as if the upload took longer than max_runtime
            return result

        with mock.patch.object(BackupRunner, '_upload', slow_upload):
            with self.assertRaisesRegex(BackupError, 'max_runtime'):
                run_backup('default')
        self.assert_nothing_visible(BackupSet.objects.get())

    def test_job_that_lost_its_lock_cannot_commit(self):
        original = BackupRunner._upload

        def upload_then_reclaimed(runner, *args):
            result = original(runner, *args)
            self.assertTrue(locks.reclaim('policy:default'))  # what the scheduler does to a hung job
            return result

        with mock.patch.object(BackupRunner, '_upload', upload_then_reclaimed):
            with self.assertRaises((locks.LockLost, BackupError)):
                run_backup('default')
        self.assert_nothing_visible(BackupSet.objects.get())

    def test_tick_frees_the_lock_of_a_hung_job(self):
        catalog.sync_static()
        policy = BackupPolicy.objects.get()
        stuck = BackupSet.objects.create(set_id=layout.new_ulid(), instance_id='x', policy=policy,
                                         policy_name='default', trigger='cli', status=SetStatusChoices.RUNNING,
                                         started=timezone.now() - timedelta(hours=2))
        hung = locks.connect()
        self.addCleanup(hung.close)
        self.assertTrue(locks.try_lock(hung, 'policy:default'))
        with mock.patch('netbox_backup.engine.locks.reclaim', return_value=False):
            self.assertEqual(scheduler.fail_overruns(), 0)  # holder still there: not declared failed
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, SetStatusChoices.RUNNING)
        self.assertEqual(scheduler.fail_overruns(), 1)  # terminates the holder, then marks failed
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, SetStatusChoices.FAILED)
        with self.assertRaises(psycopg.Error):
            hung.execute('SELECT 1')
        self.assertFalse(locks.is_locked('policy:default'))


class SkippedRunTest(PluginConfigMixin, TestCase):

    def test_skipped_run_recorded_as_failed(self):
        from datetime import UTC, datetime

        from core.models import Job
        self.configure(policies={'nightly': {'schedule': 'daily', 'time_zone': 'UTC'}})
        t0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        with mock.patch('netbox_backup.jobs.BackupJob.enqueue'), mock.patch('netbox_backup.jobs.PruneJob.enqueue'):
            scheduler.tick(now=t0)
            row = BackupPolicy.objects.get()
            Job.objects.create(object_type=ContentType.objects.get_for_model(BackupPolicy), object_id=row.pk,
                               name='Backup', status='running', job_id=uuid.uuid4())
            scheduler.tick(now=t0 + timedelta(days=1))
        skipped = BackupSet.objects.get()
        self.assertEqual(skipped.status, SetStatusChoices.FAILED)
        self.assertIn('Skipped', skipped.error)


class ServerSideEncryptionTest(PluginConfigMixin, TransactionTestCase):
    """
    SSE-KMS and SSE-C change the ETag: level 1 must degrade to size only rather than fail every set, and prune
    must not let history go while the newest sets can't be verified.
    """

    def setUp(self):
        super().setUp()
        import boto3
        from moto import mock_aws
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        os.environ.update({'NBB_TEST_S3_KEY': 'testing', 'NBB_TEST_S3_SECRET': 'testing'})
        s3 = boto3.client('s3', region_name='us-east-1')
        s3.create_bucket(Bucket='kms-bucket')
        s3.put_bucket_encryption(Bucket='kms-bucket', ServerSideEncryptionConfiguration={'Rules': [
            {'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'aws:kms'}}]})
        creds = {'access_key': 'env:NBB_TEST_S3_KEY', 'secret_key': 'env:NBB_TEST_S3_SECRET'}
        self.configure(
            targets={'s3': {'backend': 's3', 'bucket': 'kms-bucket', 'region': 'us-east-1', 'credentials': creds,
                            'prune_credentials': creds}},
            policies={'default': {'components': ['database'], 'target': 's3',
                                  'retention': {'keep_last': 1, 'keep_daily': 0, 'keep_weekly': 0,
                                                'keep_monthly': 0, 'min_age': '0h'}}},
        )

    def test_kms_degrades_to_size_only_and_prune_holds(self):
        target = conf.get_config().targets['s3']
        report = get_backend(target).probe(under=conf.instance_id())
        self.assertIsNone(report.object_checksum)
        catalog.sync_static()
        StorageTarget.objects.filter(name='s3').update(last_probe=report.as_dict(), last_probe_at=timezone.now())

        first = run_backup('default')
        copy = first.copies.get()
        self.assertEqual(copy.status, CopyStatusChoices.VERIFIED)
        self.assertEqual(copy.checksum_mode, 'size')
        info = get_backend(target).stat(layout.object_key(conf.instance_id(), first.set_id, 'db.pgdump.enc'))
        self.assertIsNone(info.checksum)
        self.assertIn('aws:kms', info.checksum_unavailable)

        second = run_backup('default')
        BackupSet.objects.filter(pk=second.pk).update(verify_level=0)  # the newest set failed to verify
        held = prune.prune_target(conf.get_config(), target)
        self.assertEqual(held.deleted, [])
        self.assertIn('default', held.held)
        self.assertEqual(len(sets.list_sets(get_backend(target), conf.instance_id())), 2)

        BackupSet.objects.filter(pk=second.pk).update(verify_level=1)
        self.assertEqual(prune.prune_target(conf.get_config(), target).deleted, [first.set_id])


class DisasterRecoveryTest(MediaMixin, PluginConfigMixin, TransactionTestCase):

    def setUp(self):
        super().setUp()
        Site.objects.bulk_create([Site(name=f'Site {i}', slug=f'site-{i}') for i in range(5)])

    def live(self):
        return connection.settings_dict['NAME']

    def test_empty_needs_no_marker_and_an_empty_partner(self):
        config = conf.get_config()
        empty, reasons, newest = dr_state(config, self.live())
        self.assertFalse(empty)
        self.assertIn(self.live(), newest)
        with mock.patch('netbox_backup.core.pg.is_empty', return_value=True):
            self.assertTrue(dr_state(config, self.live())[0])
            db = conf.database()
            with db.connect() as c:
                pgcore.set_slot_marker(c, db.name, True)
            try:
                empty, reasons, _ = dr_state(config, self.live())
                self.assertFalse(empty)
                self.assertIn('slot marker', ' '.join(reasons))
            finally:
                with db.connect() as c:
                    pgcore.set_slot_marker(c, db.name, False)

        def only_live_empty(conn):
            return conn.info.dbname == self.live()

        self.configure(databases=[self.live(), 'postgres'])
        with mock.patch('netbox_backup.core.pg.is_empty', side_effect=only_live_empty):
            empty, reasons, _ = dr_state(conf.get_config(), self.live())
        self.assertFalse(empty)
        self.assertIn('postgres', ' '.join(reasons))

    def test_rerun_after_crashed_load_reuses_the_safety_set(self):
        backup_set = run_backup('default')
        with mock.patch('netbox_backup.core.pg.load_dump', side_effect=RuntimeError('worker killed')):
            with self.assertRaisesRegex(RuntimeError, 'worker killed'):
                Restorer().load(backup_set.set_id, strategy=RestoreStrategyChoices.IN_PLACE, confirm=self.live())
        crashed = next(j for j in journal_mod.list_journals() if j['set_id'] == backup_set.set_id)
        self.assertEqual(crashed['status'], 'failed')
        self.assertTrue(crashed['emptied'])
        safety_set_id = crashed['safety_set_id']
        self.assertIsNotNone(safety_set_id)

        # The live database is empty now, but an unfinished journal means this is not disaster recovery
        with self.assertRaisesRegex(RestoreError, 'unfinished'):
            Restorer().load(backup_set.set_id, strategy=RestoreStrategyChoices.IN_PLACE)
        rerun = Restorer().load(backup_set.set_id, strategy=RestoreStrategyChoices.IN_PLACE, confirm=self.live())
        self.assertEqual(rerun['status'], 'finished', rerun.data)
        self.assertEqual(rerun['safety_set_id'], safety_set_id)
        self.assertEqual(rerun['rerun_of'], crashed.restore_id)
        connection.close()
        self.assertEqual(Site.objects.count(), 5)
        statuses = {j.restore_id: j['status'] for j in journal_mod.list_journals()}
        self.assertEqual(statuses[crashed.restore_id], 'superseded')
        stored = sets.list_sets(get_backend(conf.get_config().targets['primary']), conf.instance_id())
        safety = [s for s in stored if s.set_id != backup_set.set_id]
        self.assertEqual([s.set_id for s in safety], [safety_set_id])  # no second safety set of half a load
        self.assertEqual(BackupSet.objects.get(set_id=safety_set_id).trigger, TriggerChoices.SAFETY)


class InstanceIdentityTest(PluginConfigMixin, TransactionTestCase):

    def setUp(self):
        super().setUp()
        conf.forget_recorded_instance()
        self.addCleanup(conf.forget_recorded_instance)
        self.configure(instance_id=None, policies=DB_ONLY)

    def test_first_backup_generates_and_records_the_id(self):
        self.assertIsNone(conf.get_config().instance_id)
        backup_set = run_backup('default')
        identity = InstanceIdentity.objects.get()
        uuid.UUID(identity.instance_id)
        self.assertEqual(backup_set.instance_id, identity.instance_id)
        self.assertEqual(conf.get_config().instance_id, identity.instance_id)
        backend = get_backend(conf.get_config().targets['primary'])
        recorded = sets.read_instance(backend, identity.instance_id)
        self.assertEqual(recorded['instance_id'], identity.instance_id)
        self.assertEqual(run_backup('default').instance_id, identity.instance_id)  # stable afterwards

    def test_clone_with_other_allowed_hosts_refused(self):
        run_backup('default')
        with self.settings(ALLOWED_HOSTS=['qa.example.net']):  # a QA copy restored from this install
            with self.assertRaisesRegex(BackupError, 'clone or QA copy'):
                run_backup('default')

    def test_restore_paths_need_a_pinned_id(self):
        from netbox_backup.engine.restore import locate
        with self.assertRaises(RestoreError):
            locate(conf.get_config(), 'latest')
