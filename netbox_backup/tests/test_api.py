from core.models import Job
from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase

from netbox_backup.models import Backup, RestoreEvent

from .utils import PluginConfigMixin


def make_backup(name='20260101T000000Z-database-aaaa', **kwargs):
    return Backup.objects.create(name=name, instance_id='test', kind='database', trigger='cli',
                                 status='completed', manifest={'artifacts': []}, **kwargs)


class BackupAPITest(PluginConfigMixin, APITestCase):

    def test_list_requires_permission(self):
        make_backup()
        response = self.client.get(reverse('plugins-api:netbox_backup-api:backup-list'), **self.header)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.add_permissions('netbox_backup.view_backup')
        response = self.client.get(reverse('plugins-api:netbox_backup-api:backup-list'), **self.header)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['status']['value'], 'completed')

    def test_read_only(self):
        backup = make_backup()
        self.add_permissions('netbox_backup.view_backup', 'netbox_backup.change_backup')
        url = reverse('plugins-api:netbox_backup-api:backup-detail', kwargs={'pk': backup.pk})
        response = self.client.patch(url, {'name': 'x'}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_delete(self):
        backup = make_backup()
        self.add_permissions('netbox_backup.view_backup', 'netbox_backup.delete_backup')
        url = reverse('plugins-api:netbox_backup-api:backup-detail', kwargs={'pk': backup.pk})
        response = self.client.delete(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Backup.objects.exists())

    def test_run_requires_run_permission(self):
        url = reverse('plugins-api:netbox_backup-api:backup-run')
        response = self.client.post(url, {'kind': 'full'}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.add_permissions('netbox_backup.run_backup')
        response = self.client.post(url, {'kind': 'full', 'verify_level': 'deep'}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        job = Job.objects.get(pk=response.data['id'])
        self.assertEqual(job.name, 'Backup')

    def test_run_validates_input(self):
        self.add_permissions('netbox_backup.run_backup')
        url = reverse('plugins-api:netbox_backup-api:backup-run')
        response = self.client.post(url, {'kind': 'everything'}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_verify(self):
        backup = make_backup()
        self.add_permissions('netbox_backup.run_backup')
        url = reverse('plugins-api:netbox_backup-api:backup-verify', kwargs={'pk': backup.pk})
        # The backup must also be visible to the user
        response = self.client.post(url, {'level': 'restore'}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.add_permissions('netbox_backup.view_backup')
        response = self.client.post(url, {'level': 'restore'}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_status(self):
        url = reverse('plugins-api:netbox_backup-api:status')
        self.assertEqual(self.client.get(url, **self.header).status_code, status.HTTP_403_FORBIDDEN)
        self.add_permissions('netbox_backup.view_backup')
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {c['name'] for c in response.data['conditions']}
        self.assertIn('StorageReachable', names)
        self.assertIn(response.data['status'], ('ok', 'info', 'warning', 'error'))

    def test_sync(self):
        self.add_permissions('netbox_backup.run_backup')
        response = self.client.post(reverse('plugins-api:netbox_backup-api:backup-sync'), **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('created', response.data)


class RestoreEventAPITest(APITestCase):

    def test_list(self):
        from django.utils import timezone
        RestoreEvent.objects.create(backup_name='b', backup_instance_id='i', status='completed',
                                    started_at=timezone.now())
        self.add_permissions('netbox_backup.view_restoreevent')
        response = self.client.get(reverse('plugins-api:netbox_backup-api:restoreevent-list'), **self.header)
        self.assertEqual(response.data['count'], 1)


class ReadOnlyTokenTest(PluginConfigMixin, APITestCase):

    def test_read_only_token_cannot_run(self):
        self.add_permissions('netbox_backup.run_backup')
        self.token.write_enabled = False
        self.token.save()
        response = self.client.post(reverse('plugins-api:netbox_backup-api:backup-run'), {'kind': 'database'},
                                    format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
