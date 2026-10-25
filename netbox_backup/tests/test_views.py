import os

from core.models import Job
from django.urls import reverse
from django.utils import timezone
from utilities.testing import TestCase

from netbox_backup.models import Backup, RestoreEvent

from .utils import PluginConfigMixin


class ViewTest(PluginConfigMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.backup = Backup.objects.create(
            name='20260101T000000Z-database-aaaa', instance_id='test', kind='database', trigger='cli',
            status='completed', size=10, manifest={
                'artifacts': [{'name': 'database.dump', 'type': 'database', 'size': 4, 'sha256': 'x' * 64}],
                'database': {'row_counts': {'dcim_site': 3}, 'tables': 1, 'rows': 3},
            },
        )
        os.makedirs(os.path.join(self.storage_path, 'test', self.backup.name))
        with open(os.path.join(self.storage_path, 'test', self.backup.name, 'database.dump'), 'wb') as f:
            f.write(b'DUMP')
        self.event = RestoreEvent.objects.create(backup_name=self.backup.name, backup_instance_id='test',
                                                 status='recovery-required', started_at=timezone.now(),
                                                 row_count_mismatches={'dcim_site': {'expected': 3, 'actual': 1}})

    def test_pages_require_view_permission(self):
        for url in (reverse('plugins:netbox_backup:status'), reverse('plugins:netbox_backup:backup_list'),
                    self.backup.get_absolute_url()):
            self.assertIn(self.client.get(url).status_code, (403, 404), url)

    def test_pages_render(self):
        self.add_permissions('netbox_backup.view_backup', 'netbox_backup.view_restoreevent',
                             'netbox_backup.run_backup')
        for url in (
            reverse('plugins:netbox_backup:status'),
            reverse('plugins:netbox_backup:backup_list'),
            reverse('plugins:netbox_backup:backup_run'),
            self.backup.get_absolute_url(),
            reverse('plugins:netbox_backup:restoreevent_list'),
            self.event.get_absolute_url(),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
        self.assertContains(self.client.get(self.event.get_absolute_url()), 'Recovery required')
        self.assertContains(self.client.get(self.backup.get_absolute_url()), 'netbox_backup restore')

    def test_run_backup_queues_job(self):
        self.add_permissions('netbox_backup.run_backup')
        response = self.client.post(reverse('plugins:netbox_backup:backup_run'), {'kind': 'database'})
        job = Job.objects.get(name='Backup')
        self.assertRedirects(response, reverse('core:job', kwargs={'pk': job.pk}), fetch_redirect_response=False)

    def test_run_backup_forbidden(self):
        response = self.client.post(reverse('plugins:netbox_backup:backup_run'), {'kind': 'database'})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Job.objects.exists())

    def test_download(self):
        url = reverse('plugins:netbox_backup:backup_download', kwargs={'pk': self.backup.pk,
                                                                       'artifact': 'database.dump'})
        self.add_permissions('netbox_backup.view_backup')
        self.assertEqual(self.client.get(url).status_code, 403)
        self.add_permissions('netbox_backup.download_backup')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), b'DUMP')
        bad = reverse('plugins:netbox_backup:backup_download', kwargs={'pk': self.backup.pk,
                                                                       'artifact': '..%2F..%2Fetc%2Fpasswd'})
        self.assertEqual(self.client.get(bad).status_code, 404)

    def test_delete_removes_from_storage(self):
        self.add_permissions('netbox_backup.view_backup', 'netbox_backup.delete_backup')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('plugins:netbox_backup:backup_delete', kwargs={'pk': self.backup.pk}),
                             {'confirm': True})
        self.assertFalse(Backup.objects.exists())
        self.assertFalse(os.path.exists(os.path.join(self.storage_path, 'test', self.backup.name)))
