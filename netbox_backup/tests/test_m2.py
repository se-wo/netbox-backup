"""
M2: replicas, QA restore instances with signed reports, scheduled verification and drill headroom, and the
operational checks around them.
"""
import os
from datetime import datetime, timedelta
from unittest import mock

from dcim.models import Site
from django.conf import settings
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from netbox_backup import conf
from netbox_backup.choices import CopyStatusChoices, RestoreStrategyChoices, SetStatusChoices, VerifyLevelChoices
from netbox_backup.core import layout, sets
from netbox_backup.core.config import Config, ConfigError
from netbox_backup.engine import checks, health, prune, qa, scheduler
from netbox_backup.engine.backup import database_volume_problem, run_backup
from netbox_backup.engine.replicate import due_retries, replicate
from netbox_backup.engine.restore import RestoreError, Restorer, build_plan, server_headroom
from netbox_backup.engine.verify import verify_set
from netbox_backup.models import BackupCopy, BackupSet
from netbox_backup.storage import get_backend

from .test_backup import MediaMixin
from .test_core import KeyMaterial
from .utils import INSTANCE_ID, PluginConfigMixin

try:
    import boto3
    from moto import mock_aws
except ImportError:  # pragma: no cover
    mock_aws = None

QA_INSTANCE_ID = '9f8e7d6c-5b4a-4392-8172-6a5b4c3d2e1f'


class ReplicaTest(MediaMixin, PluginConfigMixin, TransactionTestCase):

    def setUp(self):
        super().setUp()
        Site.objects.bulk_create([Site(name=f'Site {i}', slug=f'site-{i}') for i in range(10)])
        self.replica_path = f'{self.tmp}/replica'
        self.configure(
            targets={
                'primary': {'backend': 'local', 'path': self.storage_path, 'allow_prune': True},
                'replica': {'backend': 'local', 'path': self.replica_path, 'allow_prune': True},
            },
            policies={'default': {'target': 'primary', 'replicas': ['replica']}},
        )

    def backend(self, name):
        return get_backend(conf.get_config().targets[name])

    def test_set_is_copied_objects_first_and_verified(self):
        older = run_backup('default')
        backup_set = run_backup('default')
        replica = backup_set.copies.get(target__name='replica')
        self.assertEqual(replica.status, CopyStatusChoices.PENDING)

        # Until the replica holds a set, the primary keeps it whatever retention says
        strict = {'keep_last': 1, 'keep_daily': 0, 'keep_weekly': 0, 'keep_monthly': 0, 'min_age': 0}
        self.configure(
            targets={
                'primary': {'backend': 'local', 'path': self.storage_path, 'allow_prune': True, 'retention': strict},
                'replica': {'backend': 'local', 'path': self.replica_path, 'allow_prune': True},
            },
            policies={'default': {'target': 'primary', 'replicas': ['replica']}},
        )
        report = prune.prune_target(conf.get_config(), conf.get_config().target('primary'), dry_run=True)
        self.assertEqual(report.waiting_for_replicas, [older.set_id])
        self.assertEqual(replicate(older.set_id), {'replica': CopyStatusChoices.VERIFIED})
        report = prune.prune_target(conf.get_config(), conf.get_config().target('primary'), dry_run=True)
        self.assertEqual((report.waiting_for_replicas, report.delete), ([], [older.set_id]))

        self.assertEqual(replicate(backup_set.set_id), {'replica': CopyStatusChoices.VERIFIED})
        replica.refresh_from_db()
        self.assertEqual(replica.verify_level, VerifyLevelChoices.INTEGRITY)
        self.assertEqual({r['method'] for r in replica.copied_objects.values()}, {'stream'})
        listing = sets.resolve(self.backend('replica'), INSTANCE_ID)
        self.assertEqual(listing.set_id, backup_set.set_id)
        self.assertEqual(self.backend('replica').get_bytes(f'{INSTANCE_ID}/instance.json'),
                         self.backend('primary').get_bytes(f'{INSTANCE_ID}/instance.json'))
        self.assertTrue(verify_set(backup_set.set_id))

        # A rerun keeps what is there after reading it back
        self.assertEqual(replicate(backup_set.set_id), {'replica': CopyStatusChoices.VERIFIED})
        replica.refresh_from_db()
        self.assertEqual({r['method'] for r in replica.copied_objects.values()}, {'existing'})

    def test_corrupt_source_never_completes_and_leaves_the_set_partial(self):
        backup_set = run_backup('default')
        key = layout.object_key(INSTANCE_ID, backup_set.set_id, layout.OBJECT_NAMES['media'])
        path = os.path.join(self.storage_path, key)
        os.chmod(path, 0o600)
        with open(path, 'r+b') as f:
            f.seek(10)
            f.write(b'\0\0\0\0')
        self.assertEqual(replicate(backup_set.set_id), {'replica': CopyStatusChoices.FAILED})
        backup_set.refresh_from_db()
        self.assertEqual(backup_set.status, SetStatusChoices.PARTIAL)
        copy = backup_set.copies.get(target__name='replica')
        self.assertIn('SHA-384', copy.error)
        listing = sets.list_sets(self.backend('replica'), INSTANCE_ID)
        self.assertFalse(any(s.complete for s in listing), 'a set that failed to copy must stay invisible')
        conds = health.conditions(check_workers=False)
        self.assertTrue(any(c.name == 'Replica' and c.status == health.WARNING for c in conds))

    def test_retries_are_scheduled_and_bounded(self):
        backup_set = run_backup('default')
        copy = backup_set.copies.get(target__name='replica')
        later = timezone.now() + timedelta(hours=2)
        self.assertEqual(due_retries(later), {backup_set.set_id: ['replica']})
        BackupCopy.objects.filter(pk=copy.pk).update(status=CopyStatusChoices.FAILED, attempts=3,
                                                     last_attempt=timezone.now())
        self.assertEqual(due_retries(later), {})
        BackupCopy.objects.filter(pk=copy.pk).update(attempts=1)
        self.assertEqual(due_retries(timezone.now()), {}, 'an hour must pass between attempts')
        with mock.patch('netbox_backup.jobs.enqueue_replicate') as enqueue:
            scheduler.retry_replicas(conf.get_config(), later, log=mock.Mock())
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs['targets'], ['replica'])
        with mock.patch('netbox_backup.jobs.enqueue_replicate') as enqueue:
            scheduler.retry_replicas(conf.get_config(), later + timedelta(minutes=5), log=mock.Mock())
        enqueue.assert_not_called()  # still queued from the previous tick

    def test_backup_job_enqueues_replication(self):
        from netbox_backup.jobs import BackupJob
        runner = BackupJob(mock.Mock())
        with mock.patch('netbox_backup.engine.backup.run_backup', return_value=mock.Mock(set_id='S')), \
                mock.patch('netbox_backup.jobs.enqueue_replicate') as replicate_job, \
                mock.patch('netbox_backup.jobs.enqueue_prune'):
            runner.run('default')
        replicate_job.assert_called_once_with('S', timeout=conf.get_config().policy('default').max_runtime)


class S3ReplicaTest(MediaMixin, PluginConfigMixin, TransactionTestCase):
    """
    S3 to S3 on one endpoint copies server-side; the copy job records the part size and the ETag that follows.
    """

    def setUp(self):
        if mock_aws is None:
            self.skipTest('boto3/moto not installed')
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        super().setUp()
        os.environ.update({'NBB_TEST_S3_KEY': 'testing', 'NBB_TEST_S3_SECRET': 'testing'})
        client = boto3.client('s3', region_name='us-east-1')
        for bucket in ('primary-bucket', 'replica-bucket'):
            client.create_bucket(Bucket=bucket)
        Site.objects.create(name='Site', slug='site')
        creds = {'access_key': 'env:NBB_TEST_S3_KEY', 'secret_key': 'env:NBB_TEST_S3_SECRET'}
        self.configure(
            targets={
                'primary': {'backend': 's3', 'bucket': 'primary-bucket', 'region': 'us-east-1', 'credentials': creds},
                'replica': {'backend': 's3', 'bucket': 'replica-bucket', 'region': 'us-east-1', 'credentials': creds},
                'nas': {'backend': 'local', 'path': f'{self.tmp}/nas'},
            },
            policies={'default': {'target': 'primary', 'replicas': ['replica', 'nas']}},
        )

    def test_server_side_copy_and_streaming(self):
        backup_set = run_backup('default')
        self.assertEqual(replicate(backup_set.set_id), {'replica': 'verified', 'nas': 'verified'})
        s3_copy = backup_set.copies.get(target__name='replica')
        self.assertEqual({r['method'] for r in s3_copy.copied_objects.values()}, {'server-side'})
        db = s3_copy.copied_objects[layout.OBJECT_NAMES['database']]
        self.assertEqual(db['checksum']['type'], 'etag-md5')
        self.assertTrue(db['checksum']['value'].endswith('-1'), db)
        self.assertGreater(db['part_size'], 0)
        self.assertEqual(s3_copy.checksum_mode, 'etag-md5')
        self.assertEqual({r['method'] for r in backup_set.copies.get(target__name='nas').copied_objects.values()},
                         {'stream'})
        self.assertTrue(verify_set(backup_set.set_id))

        # A replaced object on the replica fails level 1 there
        replica = get_backend(conf.get_config().target('replica'))
        replica.client.put_object(Bucket='replica-bucket', Key=db_key(backup_set), Body=b'replaced')
        self.assertFalse(verify_set(backup_set.set_id))
        backup_set.refresh_from_db()
        self.assertEqual(backup_set.status, SetStatusChoices.PARTIAL)


def db_key(backup_set):
    return layout.object_key(backup_set.instance_id, backup_set.set_id, layout.OBJECT_NAMES['database'])


class QARestoreTest(MediaMixin, PluginConfigMixin, TransactionTestCase):
    """
    Production backs up; a QA instance (own instance_id, own report key) restores that set in place, checks it and
    signs a report; production imports the report and marks the set level 2.
    """

    def setUp(self):
        super().setUp()
        Site.objects.bulk_create([Site(name=f'Site {i}', slug=f'site-{i}') for i in range(5)])
        self.keys = KeyMaterial.get()

    def production(self):
        self.configure(crypto={**self.crypto_config(), 'report_signers': [self.keys['stranger']['fpr']]})

    def qa_instance(self):
        crypto = {**self.crypto_config(), 'signing_key': self.keys['stranger']['fpr'],
                  'signing_secret_key': f"file:{self.keys['stranger']['sec']}",
                  'trusted_signers': [self.keys['signing']['fpr']]}
        self.configure(instance_id=QA_INSTANCE_ID, crypto=crypto, policies={})

    def live(self):
        return settings.DATABASES['default']['NAME']

    def test_report_round_trip(self):
        self.production()
        backup_set = run_backup('default')
        set_id = backup_set.set_id
        Site.objects.create(name='after the backup', slug='after')

        self.qa_instance()
        with self.assertRaisesRegex(RestoreError, '--from-instance'):
            Restorer().qa_run(set_id, from_instance=QA_INSTANCE_ID, confirm=self.live())
        with mock.patch('netbox_backup.engine.restore.Restorer._enqueue_full_backup') as post_backup:
            journal, report = Restorer().qa_run(set_id, from_instance=INSTANCE_ID, confirm=self.live())
        post_backup.assert_not_called()  # a QA restore takes no backup of its own
        self.assertTrue(report['passed'], report)
        self.assertEqual({c['check'] for c in report['checks']}, {'baseline', 'migrate --check', 'API smoke test'})
        self.assertEqual(Site.objects.count(), 5)
        backend = get_backend(conf.get_config().targets['primary'])
        reports = list(backend.list(f'{INSTANCE_ID}/reports/'))
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0].key.endswith(f"{report['report_id']}-{set_id}.json"))

        self.production()
        self.assertEqual(qa.import_reports(), 1)
        row = BackupSet.objects.get(set_id=set_id)
        self.assertEqual(row.verify_level, VerifyLevelChoices.QA_RESTORE)
        self.assertIsNotNone(row.qa_passed_at)
        self.assertTrue(row.qa_report['passed'])
        self.assertEqual(row.qa_report['signer'], self.keys['stranger']['fpr'])
        self.assertEqual(qa.import_reports(), 0, 'importing is idempotent')

    def test_failed_qa_restore_is_reported(self):
        self.production()
        set_id = run_backup('default').set_id
        self.qa_instance()
        with mock.patch('netbox_backup.engine.qa.api_smoke_test', return_value=[{'path': '/api/', 'status': 500}]):
            with self.assertRaisesRegex(RestoreError, 'QA checks failed'):
                Restorer().qa_run(set_id, from_instance=INSTANCE_ID, confirm=self.live())
        self.production()
        qa.import_reports()
        row = BackupSet.objects.get(set_id=set_id)
        self.assertFalse(row.qa_report['passed'])
        self.assertIsNone(row.qa_passed_at)
        conds = health.conditions(check_workers=False)
        self.assertTrue(any(c.name == 'QARestore' and c.status == health.ERROR for c in conds), conds)

    def test_reports_need_a_report_signer(self):
        self.production()
        set_id = run_backup('default').set_id
        # Signed with production's own key: a compromised worker must not be able to vouch for its own sets
        report = qa.build_report(conf.get_config(), set_id, INSTANCE_ID, timezone.now(), True, [])
        qa.store_report(conf.get_config(), report, 'primary')
        self.assertEqual(qa.import_reports(), 0)
        self.assertIsNone(BackupSet.objects.get(set_id=set_id).qa_report)
        with self.assertRaisesRegex(ConfigError, 'report_signers'):
            Config.from_dict({'crypto': {**self.crypto_config(), 'report_signers': [self.keys['signing']['fpr']]}})

    def test_load_qa_needs_a_pinned_instance(self):
        self.production()
        set_id = run_backup('default').set_id
        self.configure(instance_id=None)
        with self.assertRaisesRegex(RestoreError, 'pinned'):
            Restorer().load(set_id, strategy=RestoreStrategyChoices.IN_PLACE, qa=True, confirm=self.live(),
                            from_instance=INSTANCE_ID)


class HeadroomTest(PluginConfigMixin, TransactionTestCase):

    def test_threshold(self):
        ok, detail = server_headroom({'storage_bytes': 10 ** 15, 'max_used_percent': 80, 'ignore_databases': ()})
        self.assertTrue(ok, detail)
        ok, detail = server_headroom({'storage_bytes': 10 ** 6, 'max_used_percent': 80, 'ignore_databases': ()})
        self.assertFalse(ok)
        self.assertIn('after the load', detail)

    def test_unmeasurable_database_blocks(self):
        with mock.patch('netbox_backup.core.pg.server_usage', return_value=(100, ['other_tenant'])):
            ok, detail = server_headroom({'storage_bytes': 10 ** 15, 'max_used_percent': 80,
                                          'ignore_databases': ()})
        self.assertFalse(ok)
        self.assertIn('other_tenant', detail)

    def test_config_validation(self):
        with self.assertRaisesRegex(ConfigError, 'storage_bytes'):
            Config.from_dict({'headroom': {'max_used_percent': 80}})
        with self.assertRaisesRegex(ConfigError, '95'):
            Config.from_dict({'headroom': {'storage_bytes': 1, 'max_used_percent': 96}})
        config = Config.from_dict({'headroom': {'storage_bytes': 1024}})
        self.assertEqual(config.headroom['max_used_percent'], 80)


class SlotHeadroomTest(MediaMixin, PluginConfigMixin, TransactionTestCase):
    """
    A scheduled drill measures the server instead of needing --headroom-confirmed.
    """
    SLOT = 'test_netbox_nbb_drill'

    def setUp(self):
        super().setUp()
        from .test_restore import admin
        from .utils import drop_database
        with admin() as c:
            drop_database(c, self.SLOT)
            c.execute(f'CREATE DATABASE {self.SLOT} OWNER {conf.database().params["user"]}')
        Site.objects.create(name='Site', slug='site')

    def tearDown(self):
        from .test_restore import admin
        from .utils import drop_database
        with admin() as c:
            drop_database(c, self.SLOT)
        super().tearDown()

    def test_drill_with_headroom(self):
        from netbox_backup.engine.restore import init_slot
        live = settings.DATABASES['default']['NAME']
        self.configure(databases=[live, self.SLOT])
        init_slot(self.SLOT)
        backup_set = run_backup('default')
        self.assertFalse(build_plan(backup_set.set_id).ok)  # neither confirmed nor configured
        self.configure(databases=[live, self.SLOT], headroom={'storage_bytes': 10 ** 15})
        plan = build_plan(backup_set.set_id)
        self.assertTrue(plan.ok, plan.aborts)
        journal = Restorer().drill(backup_set.set_id)
        self.assertEqual(journal['status'], 'finished')
        self.configure(databases=[live, self.SLOT], headroom={'storage_bytes': 1024})
        with self.assertRaisesRegex(RestoreError, 'Space'):
            Restorer().drill(backup_set.set_id)


class OperationalChecksTest(PluginConfigMixin, TransactionTestCase):

    def test_ownership_check_alerts(self):
        self.assertEqual(checks.check_ownership()['problems'], [])
        with mock.patch('netbox_backup.core.pg.foreign_owned',
                        return_value={'schemas': [], 'extensions': ['ltree']}):
            result = checks.check_ownership()
        self.assertIn('ltree', result['problems'][0])
        conds = health.conditions(check_workers=False)
        self.assertTrue(any(c.name == 'Ownership' and c.status == health.ERROR for c in conds))
        checks.check_ownership()
        self.assertFalse(any(c.name == 'Ownership' for c in health.conditions(check_workers=False)))

    def test_scheduler_runs_the_ownership_check_daily(self):
        from django.core.cache import cache
        cache.delete(checks.OWNERSHIP_CACHE_KEY)
        with mock.patch('netbox_backup.engine.checks.check_ownership', wraps=checks.check_ownership) as check:
            scheduler.periodic_checks(conf.get_config(), timezone.now(), log=mock.Mock())
            scheduler.periodic_checks(conf.get_config(), timezone.now(), log=mock.Mock())
            scheduler.periodic_checks(conf.get_config(), timezone.now() + timedelta(days=1, minutes=1),
                                      log=mock.Mock())
        self.assertEqual(check.call_count, 2)

    def test_foreign_lock_holders_abort_the_plan(self):
        Site.objects.create(name='Site', slug='site')
        backup_set = run_backup('default')
        with mock.patch('netbox_backup.core.pg.foreign_lock_holders', return_value=['reporting']):
            plan = build_plan(backup_set.set_id)
        locks = next(c for c in plan.checks if c['check'] == 'Locks')
        self.assertEqual(locks['result'], 'abort')
        self.assertIn('reporting', locks['detail'])

    def test_local_target_on_the_database_volume_refused(self):
        class FakeCursor:
            def __init__(self, data_dir):
                self.data_dir = data_dir

            def execute(self, *args):
                pass

            def fetchone(self):
                return (self.data_dir,)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

        class FakeConn(FakeCursor):
            def cursor(self):
                return FakeCursor(self.data_dir)

        data_dir = os.path.join(self.tmp, 'pgdata')
        os.makedirs(data_dir)
        target = conf.get_config().target('primary')
        with mock.patch('netbox_backup.core.pg.Database.connect', return_value=FakeConn(data_dir)):
            self.assertIn('data directory', database_volume_problem(target))
        with mock.patch('netbox_backup.core.pg.Database.connect', return_value=FakeConn(None)):
            self.assertIsNone(database_volume_problem(target))
        with mock.patch('netbox_backup.core.pg.Database.connect', return_value=FakeConn(data_dir)), \
                self.assertRaisesRegex(Exception, 'lost with the database'):
            run_backup('default')


class VerifyScheduleTest(PluginConfigMixin, TestCase):

    def test_verify_schedule_enqueues_level1_each_slot(self):
        from django.core.cache import cache
        self.configure(policies={'nightly': {'schedule': 'daily', 'verify_schedule': '0 3 * * 0'}})
        cache.delete(scheduler.VERIFY_SLOT_CACHE_KEY.format('nightly'))
        start = timezone.make_aware(datetime(2026, 10, 4, 4, 0))  # a Sunday, after 03:00
        with mock.patch('netbox_backup.jobs.enqueue_verify') as enqueue:
            scheduler.schedule_verifications(conf.get_config(), start, log=mock.Mock())
            enqueue.assert_not_called()  # the first tick only records the slot
            scheduler.schedule_verifications(conf.get_config(), start + timedelta(days=1), log=mock.Mock())
            enqueue.assert_not_called()
            scheduler.schedule_verifications(conf.get_config(), start + timedelta(days=7), log=mock.Mock())
        enqueue.assert_called_once_with(None, policy='nightly')
