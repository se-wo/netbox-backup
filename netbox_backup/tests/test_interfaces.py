"""
UI, REST API, management command, standalone CLI, metrics and dashboard widget.
"""
import io
import json
import os
from contextlib import redirect_stdout
from unittest import mock

import psycopg
from dcim.models import Site
from django.core.management import call_command
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from users.models import User

from netbox_backup import cli, conf
from netbox_backup.engine import catalog
from netbox_backup.engine.backup import run_backup
from netbox_backup.models import BackupPolicy, BackupSet, StorageTarget

from .test_backup import MediaMixin
from .utils import PluginConfigMixin

EXTRA_DB = 'test_netbox_nbb_cli'


@override_settings(EXEMPT_VIEW_PERMISSIONS=['*'], LOGIN_REQUIRED=False)
class InterfaceTest(MediaMixin, PluginConfigMixin, TransactionTestCase):

    def setUp(self):
        super().setUp()
        Site.objects.bulk_create([Site(name=f'Site {i}', slug=f'site-{i}') for i in range(5)])
        self.admin = User.objects.create_superuser(username='admin', password='admin')
        self.client.force_login(self.admin)
        self.set = run_backup('default')

    def test_ui_pages(self):
        target = StorageTarget.objects.get()
        policy = BackupPolicy.objects.get()
        urls = [
            reverse('plugins:netbox_backup:status'),
            reverse('plugins:netbox_backup:storagetarget_list'),
            target.get_absolute_url(),
            reverse('plugins:netbox_backup:backuppolicy_list'),
            policy.get_absolute_url(),
            reverse('plugins:netbox_backup:backupset_list'),
            self.set.get_absolute_url(),
            reverse('plugins:netbox_backup:restorerun_list'),
        ]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
        self.assertContains(self.client.get(self.set.get_absolute_url()), self.set.set_id)
        self.assertContains(self.client.get(self.set.get_absolute_url()), 'public.dcim_site')

    def test_ui_actions_need_permissions(self):
        policy = BackupPolicy.objects.get()
        with mock.patch('netbox_backup.views.BackupJob.enqueue') as enqueue:
            enqueue.return_value = mock.Mock(pk=1)
            response = self.client.post(reverse('plugins:netbox_backup:backuppolicy_run', kwargs={'pk': policy.pk}))
            self.assertEqual(response.status_code, 302)
            self.assertEqual(enqueue.call_args.kwargs['queue_name'], 'netbox_backup.netbox_backup')
        plain = User.objects.create_user(username='viewer', password='x')
        self.client.force_login(plain)
        response = self.client.post(reverse('plugins:netbox_backup:backuppolicy_run', kwargs={'pk': policy.pk}))
        self.assertEqual(response.status_code, 403)
        response = self.client.post(reverse('plugins:netbox_backup:backupset_verify', kwargs={'pk': self.set.pk}))
        self.assertEqual(response.status_code, 403)

    def test_api(self):
        api = APIClient()
        api.force_authenticate(self.admin)
        for name in ('storagetarget', 'backuppolicy', 'backupset', 'restorerun'):
            response = api.get(reverse(f'plugins-api:netbox_backup-api:{name}-list'))
            self.assertEqual(response.status_code, 200, name)
        response = api.get(reverse('plugins-api:netbox_backup-api:backupset-detail', kwargs={'pk': self.set.pk}))
        self.assertEqual(response.data['set_id'], self.set.set_id)
        self.assertEqual(response.data['copies'][0]['target']['name'], 'primary')
        response = api.get(reverse('plugins-api:netbox_backup-api:status'))
        self.assertIn(response.data['status'], ('ok', 'warning', 'error'))
        # Downloads are off unless allow_download
        response = api.post(reverse('plugins-api:netbox_backup-api:backupset-download', kwargs={'pk': self.set.pk}))
        self.assertEqual(response.status_code, 403)
        # Policies are read-only
        response = api.patch(reverse('plugins-api:netbox_backup-api:backuppolicy-detail',
                                     kwargs={'pk': BackupPolicy.objects.get().pk}), {'enabled': False})
        self.assertEqual(response.status_code, 405)
        with mock.patch('netbox_backup.api.views.BackupJob.enqueue') as enqueue:
            enqueue.return_value = mock.Mock(pk=7, job_id='x')
            response = api.post(reverse('plugins-api:netbox_backup-api:backuppolicy-run',
                                        kwargs={'pk': BackupPolicy.objects.get().pk}))
            self.assertEqual(response.status_code, 202)
            self.assertEqual(enqueue.call_args.kwargs['trigger'], 'api')

    def test_commands(self):
        out = io.StringIO()
        call_command('nbbackup', 'list', '--json', stdout=out)
        self.assertEqual(json.loads(out.getvalue())[0]['set_id'], self.set.set_id)
        out = io.StringIO()
        call_command('nbbackup', 'show', self.set.set_id, stdout=out)
        shown = json.loads(out.getvalue())
        self.assertEqual(shown['header']['set_id'], self.set.set_id)
        self.assertEqual(shown['baseline']['rows'], self.set.baseline['rows'])
        call_command('nbbackup', 'verify', self.set.set_id, stdout=io.StringIO())
        call_command('nbbackup', 'target', 'probe', stdout=io.StringIO())
        self.assertTrue(StorageTarget.objects.get().probe_ok)
        out = io.StringIO()
        with self.assertRaises(SystemExit) as exit_:
            call_command('nbbackup', 'status', '--json', '--no-workers', stdout=out)
        summary = json.loads(out.getvalue())
        self.assertEqual(exit_.exception.code, {'ok': 0, 'warning': 1, 'error': 2}[summary['status']])
        # A local target is never off-site, so health asks for a locked off-site copy
        self.assertIn('LockedOffsiteCopy', {c['name'] for c in summary['conditions'] if c['status'] == 'error'})
        out = io.StringIO()
        call_command('nbbackup', 'restore', 'plan', self.set.set_id, '--json', stdout=out)
        self.assertEqual(json.loads(out.getvalue())['strategy'], 'in-place')
        out = io.StringIO()
        call_command('nbbackup', 'keys', 'generate', '--name', 'test <t@example.com>', '--out',
                     os.path.join(self.tmp, 'keys'), stdout=out)
        self.assertEqual(len(os.listdir(os.path.join(self.tmp, 'keys'))), 2)

    def test_catalog_sync_command(self):
        BackupSet.objects.all().delete()
        call_command('nbbackup', 'catalog', 'sync', stdout=io.StringIO())
        self.assertTrue(BackupSet.objects.filter(set_id=self.set.set_id).exists())

    def test_metrics_and_widget(self):
        from django.test import RequestFactory

        from netbox_backup.metrics import BackupCollector
        from netbox_backup.widgets import BackupHealthWidget
        families = {f.name: f for f in BackupCollector().collect()}
        self.assertIn('netbox_backup_last_success_timestamp_seconds', families)
        sample = families['netbox_backup_last_success_timestamp_seconds'].samples[0]
        self.assertEqual(sample.labels, {'policy': 'default', 'target': 'primary'})
        self.assertEqual(families['netbox_backup_enabled_policies'].samples[0].value, 1)
        request = RequestFactory().get('/')
        request.user = self.admin
        self.assertIn('Backup status', BackupHealthWidget().render(request))

    def standalone_config(self):
        path = os.path.join(self.tmp, 'nbb.json')
        with open(path, 'w') as f:
            json.dump(conf.raw(), f)
        return path

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(['--config', self.standalone_config(), *args])
        return code, out.getvalue()

    def test_standalone_cli(self):
        code, out = self.run_cli('list', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]['set_id'], self.set.set_id)
        code, out = self.run_cli('verify', '--deep', 'latest')
        self.assertEqual(code, 0, out)
        self.assertIn('db.pgdump.enc: decrypt', out)
        code, out = self.run_cli('check', '--max-age', '1h')
        self.assertEqual(code, 0, out)
        code, out = self.run_cli('decrypt', 'latest', '--out', os.path.join(self.tmp, 'plain'),
                                 '--components', 'config')
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'plain', 'config.json')))
        code, out = self.run_cli('fetch', 'latest', '--out', os.path.join(self.tmp, 'cipher'))
        self.assertEqual(code, 0, out)
        self.assertIn('db.pgdump.enc', os.listdir(os.path.join(self.tmp, 'cipher')))

    def test_standalone_restore_db(self):
        db = conf.database()
        admin = psycopg.connect(password=db.password, autocommit=True, **{**db.params, 'dbname': 'postgres'})
        admin.execute(f'DROP DATABASE IF EXISTS {EXTRA_DB}')
        admin.execute(f'CREATE DATABASE {EXTRA_DB}')
        dsn = f"postgresql://{db.params['user']}@{db.params.get('host', 'localhost')}/{EXTRA_DB}"
        os.environ['PGPASSWORD'] = db.password
        with self.assertRaisesRegex(SystemExit, '--confirm'):
            self.run_cli('restore-db', 'latest', '--dsn', dsn)  # no slot marker and no --confirm
        try:
            code, out = self.run_cli('restore-db', 'latest', '--dsn', dsn, '--confirm', EXTRA_DB)
            self.assertEqual(code, 0, out)
            with psycopg.connect(password=db.password, **{**db.params, 'dbname': EXTRA_DB}) as c:
                self.assertEqual(c.execute('SELECT count(*) FROM dcim_site').fetchone()[0], 5)
        finally:
            os.environ.pop('PGPASSWORD', None)
            admin.execute(f'DROP DATABASE IF EXISTS {EXTRA_DB} WITH (FORCE)')
            admin.close()

    def test_static_mirror_disables_removed_policies(self):
        self.configure(policies={'other': {}})
        catalog.sync_static()
        self.assertFalse(BackupPolicy.objects.get(name='default').enabled)
        self.assertTrue(BackupPolicy.objects.get(name='other').enabled)
