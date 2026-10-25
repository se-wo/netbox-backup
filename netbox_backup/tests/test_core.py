"""
Django-free tests of the core: config, GnuPG, manifest, pipeline and set reader, including tamper tests.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta

from netbox_backup.core import layout, manifest, pipeline, sets
from netbox_backup.core.config import Config, ConfigError, CryptoConfig, parse_duration, resolve_secret
from netbox_backup.core.gpg import GPGError, Keyring, generate_key
from netbox_backup.storage import get_backend


class KeyMaterial:
    """
    Generates an ops key (encryption), a recovery key, a signing key and a stranger key once per process.
    """
    _cache = None

    @classmethod
    def get(cls):
        if cls._cache is None:
            root = tempfile.mkdtemp(prefix='nbb-keys-')
            keys = {}
            for name in ('ops', 'recovery', 'signing', 'stranger'):
                fpr, public, secret = generate_key(f'nbb test {name}')
                pub_path = os.path.join(root, f'{name}.pub.asc')
                sec_path = os.path.join(root, f'{name}.sec.asc')
                with open(pub_path, 'wb') as f:
                    f.write(public)
                with open(sec_path, 'wb') as f:
                    f.write(secret)
                keys[name] = {'fpr': fpr, 'pub': pub_path, 'sec': sec_path}
            bundle = os.path.join(root, 'pubkeys.asc')
            with open(bundle, 'wb') as out:
                for name in ('ops', 'recovery', 'signing', 'stranger'):
                    with open(keys[name]['pub'], 'rb') as f:
                        out.write(f.read())
            keys['bundle'] = bundle
            keys['root'] = root
            cls._cache = keys
        return cls._cache

    @classmethod
    def crypto(cls, identities=('ops',), signing='signing', **overrides):
        keys = cls.get()
        data = {
            'public_keys': f"file:{keys['bundle']}",
            'recipients': [keys['ops']['fpr'], keys['recovery']['fpr']],
            'signing_key': keys[signing]['fpr'],
            'signing_secret_key': f"file:{keys[signing]['sec']}",
            'identities': [f"file:{keys[i]['sec']}" for i in identities],
            **overrides,
        }
        return CryptoConfig.from_dict(data)


class ConfigTest(unittest.TestCase):

    def test_parse_duration(self):
        self.assertEqual(parse_duration('24h'), timedelta(hours=24))
        self.assertEqual(parse_duration('30 days'), timedelta(days=30))
        self.assertEqual(parse_duration(2), timedelta(hours=2))
        with self.assertRaises(ConfigError):
            parse_duration('forever')

    def test_secret_references(self):
        os.environ['NBB_TEST_SECRET'] = 'value'
        self.assertEqual(resolve_secret('env:NBB_TEST_SECRET'), 'value')
        with self.assertRaises(ConfigError):
            resolve_secret('literal')
        with self.assertRaises(ConfigError):
            resolve_secret('env:NBB_TEST_UNSET_VARIABLE')

    def test_default_policy_and_fallback_instance(self):
        config = Config.from_dict({'targets': {'nas': {'backend': 'local', 'path': '/tmp/x'}}},
                                  fallback_instance_seed='seed')
        self.assertFalse(config.instance_id_pinned)
        self.assertEqual(list(config.policies), ['default'])
        policy = config.policies['default']
        self.assertEqual(policy.schedule, '0 1 * * *')
        self.assertEqual(policy.target, 'nas')
        self.assertEqual(policy.retention.keep_last, 7)

    def test_policy_validation(self):
        targets = {'a': {'backend': 'local', 'path': '/tmp/a'}}
        with self.assertRaises(ConfigError):
            Config.from_dict({'instance_id': 'x', 'targets': targets})
        with self.assertRaises(ConfigError):
            Config.from_dict({'targets': targets, 'policies': {'p': {'schedule': '* *'}}}, fallback_instance_seed='s')
        with self.assertRaises(ConfigError):
            Config.from_dict({'targets': targets, 'policies': {'p': {'components': ['redis']}}},
                             fallback_instance_seed='s')
        config = Config.from_dict({
            'instance_id': '7b0e5f2c-3c1a-4d6e-9f1a-2b3c4d5e6f70', 'targets': targets,
            'policies': {'hourly': {'schedule': 'hourly', 'components': ['database'],
                                    'retention': {'keep_last': 24, 'min_age': '1h'}}},
        })
        self.assertEqual(config.policies['hourly'].schedule, '0 * * * *')
        self.assertEqual(config.policies['hourly'].retention.min_age, timedelta(hours=1))
        self.assertEqual(config.policies['hourly'].retention.keep_daily, 14)

    def test_crypto_refuses_literals_and_short_fingerprints(self):
        with self.assertRaises(ConfigError):
            CryptoConfig.from_dict({'signing_secret_key': '-----BEGIN PGP PRIVATE KEY BLOCK-----'})
        with self.assertRaises(ConfigError):
            CryptoConfig.from_dict({'recipients': ['DEADBEEF']})


class LayoutTest(unittest.TestCase):

    def test_ulid_sorts_by_time(self):
        from datetime import UTC, datetime
        a = layout.new_ulid(datetime(2026, 1, 1, tzinfo=UTC))
        b = layout.new_ulid(datetime(2026, 1, 2, tzinfo=UTC))
        self.assertLess(a, b)
        self.assertTrue(layout.is_ulid(a))
        self.assertEqual(layout.ulid_time(a).date().isoformat(), '2026-01-01')
        self.assertEqual(layout.set_prefix('i', a), f'i/sets/2026/01/{a}')
        self.assertEqual(layout.parse_set_key(f'i/sets/2026/01/{a}/manifest.json'), ('i', a, 'manifest.json'))


@unittest.skipUnless(shutil.which('gpg'), 'gpg not installed')
class GPGTest(unittest.TestCase):

    def test_encrypt_decrypt_sign_verify(self):
        crypto = KeyMaterial.crypto()
        with Keyring(crypto, signing=True, identities=True) as keyring:
            ciphertext = keyring.encrypt_bytes(b'secret data')
            self.assertNotIn(b'secret data', ciphertext)
            self.assertEqual(keyring.decrypt_bytes(ciphertext), b'secret data')
            signature = keyring.sign_detached(b'header')
            self.assertEqual(keyring.verify_detached(b'header', signature), crypto.signing_key)
            with self.assertRaises(GPGError):
                keyring.verify_detached(b'tampered', signature)

    def test_recovery_key_alone_decrypts(self):
        with Keyring(KeyMaterial.crypto(), signing=True) as writer:
            ciphertext = writer.encrypt_bytes(b'data')
        with Keyring(KeyMaterial.crypto(identities=('recovery',)), identities=True) as reader:
            self.assertEqual(reader.decrypt_bytes(ciphertext), b'data')

    def test_wrong_identity_names_recipients(self):
        with Keyring(KeyMaterial.crypto(), signing=True) as writer:
            ciphertext = writer.encrypt_bytes(b'data')
        with Keyring(KeyMaterial.crypto(identities=('stranger',)), identities=True) as reader:
            with self.assertRaisesRegex(GPGError, 'No mounted identity'):
                reader.decrypt_bytes(ciphertext)

    def test_signature_by_unpinned_key_fails(self):
        keys = KeyMaterial.get()
        stranger = KeyMaterial.crypto(signing='stranger')
        with Keyring(stranger, signing=True) as keyring:
            signature = keyring.sign_detached(b'header')
        with Keyring(KeyMaterial.crypto()) as keyring:
            # The stranger key is not pinned, so it was removed from the keyring: verification cannot succeed
            with self.assertRaises(GPGError):
                keyring.verify_detached(b'header', signature)
        with Keyring(KeyMaterial.crypto(trusted_signers=[keys['stranger']['fpr']])) as keyring:
            self.assertEqual(keyring.verify_detached(b'header', signature), keys['stranger']['fpr'])
            with self.assertRaisesRegex(GPGError, 'not a pinned signer'):
                keyring.verify_detached(b'header', signature, signers=[keys['signing']['fpr']])

    def test_missing_pinned_key(self):
        keys = KeyMaterial.get()
        crypto = CryptoConfig.from_dict({
            'public_keys': f"file:{keys['ops']['pub']}", 'recipients': [keys['ops']['fpr']],
            'signing_key': keys['signing']['fpr'],
        })
        with self.assertRaisesRegex(GPGError, 'not found'):
            Keyring(crypto)

    def test_non_bsi_key_refused(self):
        import subprocess
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        base = ['gpg', '--homedir', home, '--batch', '--pinentry-mode', 'loopback', '--passphrase', '']
        subprocess.run(base + ['--quick-gen-key', 'ed <e@x>', 'ed25519', 'sign,cert', 'never'], check=True,
                       capture_output=True)
        listing = subprocess.run(base + ['--list-keys', '--with-colons'], capture_output=True, text=True).stdout
        fpr = next(line.split(':')[9] for line in listing.splitlines() if line.startswith('fpr:'))
        subprocess.run(base + ['--quick-add-key', fpr, 'cv25519', 'encr', 'never'], check=True, capture_output=True)
        public = subprocess.run(base + ['--export', '--armor'], capture_output=True).stdout
        path = os.path.join(home, 'ed.asc')
        with open(path, 'wb') as f:
            f.write(public)
        keys = KeyMaterial.get()
        with open(keys['bundle'], 'rb') as f:
            bundle = f.read()
        with open(path, 'ab') as f:
            f.write(bundle)
        crypto = CryptoConfig.from_dict({
            'public_keys': f'file:{path}', 'recipients': [fpr], 'signing_key': keys['signing']['fpr'],
        })
        with self.assertRaisesRegex(GPGError, 'algorithm profile'):
            Keyring(crypto)


class _FailingSource(pipeline.BytesSource):
    def close(self):
        raise pipeline.PipelineError('pg_dump exited with status 1')


class _RejectingTap:
    def write(self, chunk):
        pass

    def finish(self):
        raise pipeline.PipelineError('dump check failed')

    def abort(self):
        pass


@unittest.skipUnless(shutil.which('gpg'), 'gpg not installed')
class SetFormatTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='nbb-sets-')
        self.addCleanup(shutil.rmtree, self.root, True)
        config = Config.from_dict({'targets': {'t': {'backend': 'local', 'path': self.root}}},
                                  fallback_instance_seed='x')
        self.target = config.targets['t']
        self.backend = get_backend(self.target)
        self.instance = config.instance_id

    def write_set(self, keyring, payload=b'dump bytes ' * 1000):
        set_id = layout.new_ulid()
        key = layout.object_key(self.instance, set_id, layout.OBJECT_NAMES['database'])
        record = pipeline.upload_component(keyring, self.backend, key, pipeline.BytesSource(payload))
        header = {
            'set_id': set_id, 'instance_id': self.instance, 'objects': {'db.pgdump.enc': record.as_dict()},
        }
        raw = manifest.build(header, {'baseline': {'tables': {'public.t': 1}}}, keyring)
        self.backend.put_bytes(layout.object_key(self.instance, set_id, layout.MANIFEST), raw)
        self.backend.put_bytes(layout.object_key(self.instance, set_id, layout.COMPLETE),
                               manifest.complete_marker(raw))
        return set_id, payload

    def test_roundtrip(self):
        with Keyring(KeyMaterial.crypto(), signing=True) as keyring:
            set_id, payload = self.write_set(keyring)
            listing = sets.resolve(self.backend, self.instance, 'latest')
            self.assertEqual(listing.set_id, set_id)
            loaded = sets.load_manifest(self.backend, self.instance, set_id)
            self.assertTrue(all(r['ok'] for r in sets.level1(self.backend, loaded, keyring)))
        with Keyring(KeyMaterial.crypto(), identities=True) as keyring:
            manifest.verify(loaded, keyring)
            self.assertEqual(manifest.decrypt_body(loaded, keyring)['baseline']['tables'], {'public.t': 1})
            spool = os.path.join(self.root, 'spool')
            paths = sets.fetch_and_decrypt(self.backend, keyring, loaded, ['database'], spool)
            with open(paths['database'], 'rb') as f:
                self.assertEqual(f.read(), payload)

    def test_incomplete_set_is_invisible(self):
        with Keyring(KeyMaterial.crypto(), signing=True) as keyring:
            set_id, _ = self.write_set(keyring)
        os.unlink(os.path.join(self.root, layout.object_key(self.instance, set_id, layout.COMPLETE)))
        self.assertEqual(sets.list_sets(self.backend, self.instance, include_incomplete=False), [])
        with self.assertRaises(sets.SetError):
            sets.load_manifest(self.backend, self.instance, set_id)

    def test_replaced_manifest_detected(self):
        with Keyring(KeyMaterial.crypto(), signing=True) as keyring:
            set_id, _ = self.write_set(keyring)
        path = os.path.join(self.root, layout.object_key(self.instance, set_id, layout.MANIFEST))
        with open(path, 'rb') as f:
            document = json.loads(f.read())
        document['header']['objects']['db.pgdump.enc']['size'] += 1
        os.chmod(path, 0o600)
        with open(path, 'wb') as f:
            f.write(json.dumps(document).encode())
        with self.assertRaisesRegex(manifest.ManifestError, '_COMPLETE'):
            sets.load_manifest(self.backend, self.instance, set_id)

    def test_tampered_header_fails_signature(self):
        with Keyring(KeyMaterial.crypto(), signing=True) as keyring:
            set_id, _ = self.write_set(keyring)
            loaded = sets.load_manifest(self.backend, self.instance, set_id)
            loaded.header['objects']['db.pgdump.enc']['size'] += 1
            results = sets.level1(self.backend, loaded, keyring)
            self.assertFalse(next(r for r in results if r['check'] == 'signature')['ok'])

    def test_tampered_body_fails(self):
        with Keyring(KeyMaterial.crypto(), signing=True) as keyring:
            set_id, _ = self.write_set(keyring)
            loaded = sets.load_manifest(self.backend, self.instance, set_id)
            loaded.body_ciphertext = loaded.body_ciphertext[:-1] + b'\x00'
            with self.assertRaisesRegex(manifest.ManifestError, 'body'):
                manifest.verify(loaded, keyring)

    def test_modified_and_truncated_object_detected(self):
        with Keyring(KeyMaterial.crypto(), signing=True) as keyring:
            set_id, _ = self.write_set(keyring)
        loaded = sets.load_manifest(self.backend, self.instance, set_id)
        path = os.path.join(self.root, layout.object_key(self.instance, set_id, 'db.pgdump.enc'))
        os.chmod(path, 0o600)
        with open(path, 'r+b') as f:
            f.seek(100)
            f.write(b'\xff')
        spool = os.path.join(self.root, 'spool')
        with Keyring(KeyMaterial.crypto(), identities=True) as keyring:
            with self.assertRaisesRegex(sets.SetError, 'SHA-384'):
                sets.fetch_and_decrypt(self.backend, keyring, loaded, ['database'], spool)
            self.assertEqual(os.listdir(spool), [])
            with open(path, 'r+b') as f:
                f.truncate(50)
            results = sets.level1(self.backend, loaded, keyring)
            self.assertFalse(next(r for r in results if r['check'] == 'db.pgdump.enc: size')['ok'])

    def test_swapped_objects_detected(self):
        with Keyring(KeyMaterial.crypto(), signing=True) as keyring:
            first, _ = self.write_set(keyring, b'first' * 100)
            second, _ = self.write_set(keyring, b'second' * 100)
        a = os.path.join(self.root, layout.object_key(self.instance, first, 'db.pgdump.enc'))
        b = os.path.join(self.root, layout.object_key(self.instance, second, 'db.pgdump.enc'))
        os.chmod(a, 0o600)
        shutil.copyfile(b, a)
        loaded = sets.load_manifest(self.backend, self.instance, first)
        with Keyring(KeyMaterial.crypto(), identities=True) as keyring:
            with self.assertRaises(sets.SetError):
                sets.fetch_and_decrypt(self.backend, keyring, loaded, ['database'],
                                       os.path.join(self.root, 'spool'))

    def test_failed_source_never_commits(self):
        key = layout.object_key(self.instance, layout.new_ulid(), 'db.pgdump.enc')
        with Keyring(KeyMaterial.crypto(), signing=True) as keyring:
            with self.assertRaises(pipeline.PipelineError):
                pipeline.upload_component(keyring, self.backend, key, _FailingSource(b'partial'))
            self.assertIsNone(self.backend.stat(key))
            with self.assertRaises(pipeline.PipelineError):
                pipeline.upload_component(keyring, self.backend, key, pipeline.BytesSource(b'x'),
                                          taps=[_RejectingTap()])
            self.assertIsNone(self.backend.stat(key))

    def test_command_source_failure(self):
        key = layout.object_key(self.instance, layout.new_ulid(), 'db.pgdump.enc')
        source = pipeline.CommandSource([sys.executable, '-c', 'import sys; sys.stdout.write("x"*99999); sys.exit(3)'])
        with Keyring(KeyMaterial.crypto(), signing=True) as keyring:
            with self.assertRaisesRegex(pipeline.PipelineError, 'status 3'):
                pipeline.upload_component(keyring, self.backend, key, source)
        self.assertIsNone(self.backend.stat(key))

    def test_tar_source(self):
        import io
        import tarfile

        import zstandard
        files = {'a.txt': b'alpha', 'dir/b.bin': os.urandom(3000)}
        entries = [(name, len(data), lambda d=data: io.BytesIO(d)) for name, data in files.items()]
        source = pipeline.TarSource(entries)
        data = source.open().read()
        source.close()
        with tarfile.open(fileobj=io.BytesIO(zstandard.ZstdDecompressor().decompressobj().decompress(data)),
                          mode='r:') as tar:
            self.assertEqual({m.name: tar.extractfile(m).read() for m in tar.getmembers()}, files)
            self.assertTrue(all(m.uid == 0 and m.uname == '' for m in tar.getmembers()))
        self.assertEqual(source.files, 2)
