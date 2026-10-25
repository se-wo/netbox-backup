import os
import tempfile
import unittest

from django.test import SimpleTestCase

from netbox_backup.storage import StorageError, get_storage

try:
    import boto3
    from moto import mock_aws
except ImportError:  # pragma: no cover
    boto3 = mock_aws = None


class StorageContract:
    """
    Behaviour every storage backend must provide.
    """

    def make_storage(self):
        raise NotImplementedError

    def test_contract(self):
        storage = self.make_storage()
        storage.check()
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'x' * 1000)
        try:
            storage.put_file('inst/backup-1/a.bin', f.name)
            storage.put_bytes('inst/backup-1/manifest.json', b'{}')
            storage.put_bytes('inst/backup-2/manifest.json', b'{}')
            storage.put_bytes('other/backup-9/manifest.json', b'{}')
            self.assertTrue(storage.exists('inst/backup-1/a.bin'))
            self.assertFalse(storage.exists('inst/backup-1/nope'))
            self.assertEqual(storage.size('inst/backup-1/a.bin'), 1000)
            self.assertEqual(storage.get_bytes('inst/backup-1/manifest.json'), b'{}')
            self.assertEqual(storage.list_children(''), ['inst', 'other'])
            self.assertEqual(storage.list_children('inst'), ['backup-1', 'backup-2'])
            with storage.open_read('inst/backup-1/a.bin') as stream:
                self.assertEqual(stream.read(), b'x' * 1000)
            out = f.name + '.out'
            storage.get_file('inst/backup-1/a.bin', out)
            self.assertEqual(os.path.getsize(out), 1000)
            os.unlink(out)
            storage.delete_prefix('inst/backup-1')
            self.assertEqual(storage.list_children('inst'), ['backup-2'])
            # A prefix that merely shares leading characters must survive
            self.assertTrue(storage.exists('inst/backup-2/manifest.json'))
            with self.assertRaises(StorageError):
                storage.get_bytes('inst/backup-1/manifest.json')
            with self.assertRaises(StorageError):
                storage.delete_prefix('')
        finally:
            os.unlink(f.name)


class LocalStorageTest(StorageContract, SimpleTestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def make_storage(self):
        return get_storage({'backend': 'local', 'path': self.dir.name})

    def test_path_traversal_rejected(self):
        storage = self.make_storage()
        with self.assertRaises(StorageError):
            storage.put_bytes('../escape', b'')

    def test_files_are_private(self):
        storage = self.make_storage()
        storage.put_bytes('i/b/manifest.json', b'{}')
        mode = os.stat(os.path.join(self.dir.name, 'i/b/manifest.json')).st_mode & 0o777
        self.assertEqual(mode, 0o600)


@unittest.skipIf(mock_aws is None, 'boto3/moto not installed')
class S3StorageTest(StorageContract, SimpleTestCase):

    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        boto3.client('s3', region_name='us-east-1').create_bucket(Bucket='nb-backups')

    def tearDown(self):
        self.mock.stop()

    def make_storage(self):
        return get_storage({
            'backend': 's3', 'bucket': 'nb-backups', 'prefix': 'netbox/', 'region_name': 'us-east-1',
            'access_key_id': 'test', 'secret_access_key': 'test',
        })

    def test_prefix_applied(self):
        self.make_storage().put_bytes('i/b/manifest.json', b'{}')
        keys = [o['Key'] for o in boto3.client('s3', region_name='us-east-1').list_objects_v2(
            Bucket='nb-backups')['Contents']]
        self.assertEqual(keys, ['netbox/i/b/manifest.json'])
