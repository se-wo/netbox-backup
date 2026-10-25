"""
One contract suite, run against every backend: local directly, S3 against moto, Azure Blob against Azurite (set
NBB_TEST_AZURITE=1 with Azurite listening on 127.0.0.1:10000).
"""
import os
import shutil
import tempfile
import unittest
from datetime import timedelta

from netbox_backup.core.config import TargetConfig
from netbox_backup.storage import ObjectExists, ObjectNotFound, get_backend
from netbox_backup.storage.base import md5_etag
from netbox_backup.storage.s3 import MIN_PART, part_size_for

AZURITE_CONNECTION_STRING = (
    'DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;'
    'AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;'
    'BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;'
)


class ContractMixin:
    """
    Behaviour every backend must have. Subclasses provide make_backend(prune=False).
    """

    def test_roundtrip(self):
        backend = self.make_backend()
        info = backend.put_bytes('a/b/object', b'hello')
        self.assertEqual(info.size, 5)
        self.assertEqual(backend.get_bytes('a/b/object'), b'hello')
        stat = backend.stat('a/b/object')
        self.assertEqual(stat.size, 5)
        if backend.capabilities.object_checksum:
            self.assertEqual(stat.checksum, info.checksum)

    def test_streaming_multi_part(self):
        backend = self.make_backend()
        chunk = os.urandom(1024 * 1024)
        with backend.open_write('big', size_hint=None) as writer:
            for _ in range(int(MIN_PART * 2.5) // len(chunk)):
                writer.write(chunk)
            info = writer.commit()
        stat = backend.stat('big')
        self.assertEqual(stat.size, info.size)
        if backend.capabilities.object_checksum:
            self.assertEqual(stat.checksum, info.checksum)
        with backend.open_read('big') as f:
            self.assertEqual(f.read(len(chunk)), chunk)

    def test_create_only(self):
        backend = self.make_backend()
        backend.put_bytes('once', b'first')
        with self.assertRaises(ObjectExists):
            backend.put_bytes('once', b'second')
        self.assertEqual(backend.get_bytes('once'), b'first')

    def test_abort_leaves_nothing(self):
        backend = self.make_backend()
        writer = backend.open_write('aborted')
        writer.write(b'partial')
        writer.abort()
        self.assertIsNone(backend.stat('aborted'))
        self.assertEqual([o.key for o in backend.list('aborted')], [])

    def test_missing(self):
        backend = self.make_backend()
        self.assertIsNone(backend.stat('nope'))
        with self.assertRaises(ObjectNotFound):
            backend.get_bytes('nope')

    def test_list_and_prune_delete(self):
        backend = self.make_backend()
        for key in ('sets/1/x', 'sets/1/y', 'sets/2/x', 'other/z'):
            backend.put_bytes(key, b'.')
        self.assertEqual(sorted(o.key for o in backend.list('sets/')), ['sets/1/x', 'sets/1/y', 'sets/2/x'])
        pruner = self.make_backend(prune=True)
        self.assertEqual(pruner.delete(['sets/1/x', 'sets/1/y']), [])
        self.assertEqual(sorted(o.key for o in backend.list('sets/')), ['sets/2/x'])

    def test_probe(self):
        report = self.make_backend().probe()
        names = {c['check']: c['ok'] for c in report.checks}
        self.assertTrue(names['write'])
        self.assertTrue(names['read back'])
        self.assertTrue(names['create-only writes'])

    def test_abort_incomplete(self):
        self.assertGreaterEqual(self.make_backend().abort_incomplete(timedelta(0)), 0)


class LocalBackendTest(ContractMixin, unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='nbb-local-')
        self.addCleanup(shutil.rmtree, self.root, True)

    def make_backend(self, prune=False):
        target = TargetConfig.from_dict('local', {'backend': 'local', 'path': self.root, 'prefix': 'prod'})
        return get_backend(target, prune=prune)

    def test_worker_cannot_delete(self):
        backend = self.make_backend()
        backend.put_bytes('k', b'.')
        self.assertEqual(len(backend.delete(['k'])), 1)
        self.assertIsNotNone(backend.stat('k'))

    def test_key_escape(self):
        from netbox_backup.storage import StorageError
        with self.assertRaises(StorageError):
            self.make_backend().put_bytes('../escape', b'.')

    def test_crash_leaves_only_part_file(self):
        backend = self.make_backend()
        writer = backend.open_write('crash')
        writer.write(b'data')
        os.close(writer.fd)  # simulate a crash: never committed or aborted
        writer.closed = True
        self.assertIsNone(backend.stat('crash'))
        self.assertEqual(backend.abort_incomplete(timedelta(seconds=-1)), 1)


try:
    import boto3
    from moto import mock_aws
except ImportError:  # pragma: no cover
    mock_aws = None


@unittest.skipIf(mock_aws is None, 'boto3/moto not installed')
class S3BackendTest(ContractMixin, unittest.TestCase):

    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        os.environ.update({'NBB_TEST_S3_KEY': 'testing', 'NBB_TEST_S3_SECRET': 'testing'})
        boto3.client('s3', region_name='us-east-1').create_bucket(Bucket='backups')

    def make_backend(self, prune=False, **options):
        target = TargetConfig.from_dict('s3', {
            'backend': 's3', 'bucket': 'backups', 'prefix': 'prod', 'region': 'us-east-1',
            'credentials': {'access_key': 'env:NBB_TEST_S3_KEY', 'secret_key': 'env:NBB_TEST_S3_SECRET'},
            **options,
        })
        return get_backend(target, prune=prune)

    def test_probe_reports_missing_lock_and_lifecycle(self):
        report = self.make_backend(retention_days=35).probe()
        self.assertFalse(report.ok)
        failed = {c['check'] for c in report.checks if not c['ok']}
        self.assertIn('default retention (compliance mode)', failed)
        self.assertIn('lifecycle aborts incomplete uploads', failed)

    def test_probe_passes_with_locked_bucket(self):
        client = boto3.client('s3', region_name='us-east-1')
        client.create_bucket(Bucket='locked', ObjectLockEnabledForBucket=True)
        client.put_object_lock_configuration(Bucket='locked', ObjectLockConfiguration={
            'ObjectLockEnabled': 'Enabled', 'Rule': {'DefaultRetention': {'Mode': 'COMPLIANCE', 'Days': 35}},
        })
        client.put_bucket_lifecycle_configuration(Bucket='locked', LifecycleConfiguration={'Rules': [{
            'ID': 'abort', 'Status': 'Enabled', 'Filter': {'Prefix': ''},
            'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 1},
        }]})
        report = self.make_backend(bucket='locked', retention_days=35).probe()
        self.assertTrue(report.ok, report.checks)
        self.assertEqual(report.object_checksum, 'etag-md5')
        backend = self.make_backend(bucket='locked', retention_days=35)
        backend.put_bytes('locked-object', b'x')
        self.assertIsNotNone(backend.stat('locked-object').retain_until)

    def test_literal_secret_refused(self):
        from netbox_backup.core.config import ConfigError
        with self.assertRaises(ConfigError):
            TargetConfig.from_dict('s3', {'backend': 's3', 'bucket': 'b', 'credentials': {'secret_key': 'plain'}})
        with self.assertRaises(ConfigError):
            TargetConfig.from_dict('s3', {'backend': 's3', 'bucket': 'b', 'secret_key': 'env:X'})


@unittest.skipUnless(os.environ.get('NBB_TEST_AZURITE'), 'set NBB_TEST_AZURITE=1 with Azurite on :10000')
class AzureBlobBackendTest(ContractMixin, unittest.TestCase):

    def setUp(self):
        from azure.storage.blob import BlobServiceClient
        os.environ['NBB_TEST_AZURE'] = AZURITE_CONNECTION_STRING
        self.container = f'c{os.urandom(6).hex()}'
        service = BlobServiceClient.from_connection_string(AZURITE_CONNECTION_STRING)
        service.create_container(self.container)
        self.addCleanup(service.delete_container, self.container)

    def make_backend(self, prune=False):
        target = TargetConfig.from_dict('az', {
            'backend': 'azure_blob', 'container': self.container, 'prefix': 'prod',
            'credentials': {'connection_string': 'env:NBB_TEST_AZURE'},
        })
        return get_backend(target, prune=prune)


class PartSizeTest(unittest.TestCase):

    def test_part_size_grows(self):
        self.assertEqual(part_size_for(None, 1), MIN_PART)
        self.assertEqual(part_size_for(None, 1001), 2 * MIN_PART)
        # A 200 GiB hint fits well below 10,000 parts
        hint = 200 * 1024 ** 3
        self.assertLess(hint / part_size_for(hint, 1), 9001)

    def test_md5_etag(self):
        import hashlib
        a, b = hashlib.md5(b'a').digest(), hashlib.md5(b'b').digest()
        self.assertEqual(md5_etag([a], multipart=False), a.hex())
        self.assertEqual(md5_etag([a, b], multipart=True), hashlib.md5(a + b).hexdigest() + '-2')
