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
            'recipients': [keys['ops']['fpr']],
            'recovery_key': keys['recovery']['fpr'],
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
        config = Config.from_dict({'targets': {'nas': {'backend': 'local', 'path': '/tmp/x'}}})
        self.assertFalse(config.instance_id_pinned)
        self.assertIsNone(config.instance_id)
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
            Config.from_dict({'targets': targets, 'policies': {'p': {'schedule': '* *'}}})
        with self.assertRaises(ConfigError):
            Config.from_dict({'targets': targets, 'policies': {'p': {'components': ['redis']}}})
        config = Config.from_dict({
            'instance_id': '7b0e5f2c-3c1a-4d6e-9f1a-2b3c4d5e6f70', 'targets': targets,
            'policies': {'hourly': {'schedule': 'hourly', 'components': ['database'],
                                    'retention': {'keep_last': 24, 'min_age': '1h'}}},
        })
        self.assertEqual(config.policies['hourly'].schedule, '0 * * * *')
        self.assertEqual(config.policies['hourly'].retention.min_age, timedelta(hours=1))
        self.assertEqual(config.policies['hourly'].retention.keep_daily, 14)

    def test_signing_key_cannot_be_a_recipient(self):
        fpr = 'A' * 40
        with self.assertRaisesRegex(ConfigError, 'signing_key'):
            CryptoConfig.from_dict({'recipients': [fpr], 'signing_key': fpr})
        with self.assertRaisesRegex(ConfigError, 'signing_key'):
            CryptoConfig.from_dict({'recipients': ['B' * 40], 'recovery_key': fpr, 'signing_key': fpr})

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
            'recovery_key': keys['ops']['fpr'],
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
            'public_keys': f'file:{path}', 'recipients': [fpr], 'recovery_key': keys['recovery']['fpr'],
            'signing_key': keys['signing']['fpr'],
        })
        with self.assertRaisesRegex(GPGError, 'algorithm profile'):
            Keyring(crypto)


def _gpg_home():
    import subprocess
    home = tempfile.mkdtemp(prefix='nbb-gpgtest-')
    base = ['gpg', '--homedir', home, '--batch', '--pinentry-mode', 'loopback', '--passphrase', '']

    def run(*args, faked=None, input=None):
        extra = [f'--faked-system-time={faked}'] if faked else []
        return subprocess.run(base + extra + list(args), capture_output=True, check=True, input=input).stdout
    return home, run


def _primary_fpr(run):
    listing = run('--list-keys', '--with-colons').decode()
    return next(line.split(':')[9] for line in listing.splitlines() if line.startswith('fpr:'))


@unittest.skipUnless(shutil.which('gpg'), 'gpg not installed')
class SignerAndSubkeyTest(unittest.TestCase):
    """
    VALIDSIG is also emitted for expired and revoked keys; only GOODSIG with VALIDSIG from a pinned, valid key counts.
    """

    def crypto_with(self, extra_public, signer):
        keys = KeyMaterial.get()
        bundle = os.path.join(tempfile.mkdtemp(), 'bundle.asc')
        with open(bundle, 'wb') as out, open(keys['bundle'], 'rb') as f:
            out.write(f.read() + extra_public)
        return CryptoConfig.from_dict({
            'public_keys': f'file:{bundle}', 'recipients': [keys['ops']['fpr']],
            'recovery_key': keys['recovery']['fpr'], 'signing_key': signer,
        })

    def test_expired_signing_key_rejected(self):
        home, run = _gpg_home()
        self.addCleanup(shutil.rmtree, home, True)
        then = '20200101T000000!'
        run('--quick-gen-key', 'expired <e@x>', 'brainpoolP384r1', 'sign,cert', '1d', faked=then)
        fpr = _primary_fpr(run)
        run('--quick-add-key', fpr, 'brainpoolP384r1', 'encr', '1d', faked=then)
        signature = run('--local-user', f'{fpr}!', '--digest-algo', 'SHA384', '--armor', '--detach-sign',
                        '--output', '-', faked=then, input=b'header')
        with Keyring(self.crypto_with(run('--export', '--armor', fpr), fpr)) as keyring:
            with self.assertRaisesRegex(GPGError, 'EXPKEYSIG|failed'):
                keyring.verify_detached(b'header', signature)

    def test_revoked_signing_key_rejected(self):
        home, run = _gpg_home()
        self.addCleanup(shutil.rmtree, home, True)
        run('--quick-gen-key', 'revoked <r@x>', 'brainpoolP384r1', 'sign,cert', 'never')
        fpr = _primary_fpr(run)
        signature = run('--local-user', f'{fpr}!', '--digest-algo', 'SHA384', '--armor', '--detach-sign',
                        '--output', '-', input=b'header')
        with open(os.path.join(home, 'openpgp-revocs.d', f'{fpr}.rev'), 'rb') as f:
            revocation = f.read().replace(b':-----BEGIN', b'-----BEGIN')
        run('--import', input=revocation)
        with Keyring(self.crypto_with(run('--export', '--armor', fpr), fpr)) as keyring:
            with self.assertRaisesRegex(GPGError, 'REVKEYSIG|failed'):
                keyring.verify_detached(b'header', signature)

    def test_out_of_profile_subkey_rejected(self):
        home, run = _gpg_home()
        self.addCleanup(shutil.rmtree, home, True)
        run('--quick-gen-key', 'mixed <m@x>', 'brainpoolP384r1', 'sign,cert', 'never')
        fpr = _primary_fpr(run)
        run('--quick-add-key', fpr, 'brainpoolP384r1', 'encr', 'never')
        run('--quick-add-key', fpr, 'cv25519', 'encr', 'never')  # newest: gpg would pick it
        keys = KeyMaterial.get()
        crypto = self.crypto_with(run('--export', '--armor', fpr), keys['signing']['fpr'])
        crypto = CryptoConfig.from_dict({
            'public_keys': crypto.public_keys, 'recipients': [fpr], 'recovery_key': keys['recovery']['fpr'],
            'signing_key': keys['signing']['fpr'],
        })
        with self.assertRaisesRegex(GPGError, 'algorithm profile'):
            Keyring(crypto)

    def test_config_component_needs_the_recovery_key(self):
        keys = KeyMaterial.get()
        with Keyring(KeyMaterial.crypto(), signing=True) as writer:
            ciphertext = writer.encrypt_bytes(b'SECRET_KEY = "x"', recipients=[keys['recovery']['fpr']])
        with Keyring(KeyMaterial.crypto(identities=('ops',)), identities=True) as qa:
            with self.assertRaisesRegex(GPGError, 'No mounted identity'):
                qa.decrypt_bytes(ciphertext)
        with Keyring(KeyMaterial.crypto(identities=('recovery',)), identities=True) as recovery:
            self.assertEqual(recovery.decrypt_bytes(ciphertext), b'SECRET_KEY = "x"')
            self.assertEqual(recovery.last_decryption_key, keys['recovery']['fpr'])


class SecretsTest(unittest.TestCase):

    def test_extract_and_env(self):
        from netbox_backup.core import secrets
        found = secrets.from_configuration(
            "import os\nSECRET_KEY = 'abc'\nAPI_TOKEN_PEPPERS = {1: 'p1'}\n"
            "DATABASES = {'default': {'PASSWORD': os.environ['DB_PASSWORD']}}\n"
        )
        self.assertEqual(found['SECRET_KEY'], 'abc')
        self.assertIs(found['DB_PASSWORD'], secrets.UNKNOWN)
        lines = secrets.env_lines(found)
        self.assertIn('SECRET_KEY="abc"', lines)
        self.assertIn('API_TOKEN_PEPPER_1="p1"', lines)
        self.assertTrue(any(line.startswith('# DB_PASSWORD') for line in lines))
        differ, unknown = secrets.differences(found, {'SECRET_KEY': 'abc', 'API_TOKEN_PEPPERS': {1: 'other'}})
        self.assertEqual(differ, ['API_TOKEN_PEPPERS[1]'])
        self.assertEqual(unknown, ['DB_PASSWORD'])


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
        config = Config.from_dict({'instance_id': '6d1f6c3a-0b5e-4a8e-9a3c-2f1e0d9c8b7a',
                                   'targets': {'t': {'backend': 'local', 'path': self.root}}})
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
            loaded.body_ciphertext = loaded.body_ciphertext[:-1] + bytes([loaded.body_ciphertext[-1] ^ 0xff])
            with self.assertRaisesRegex(manifest.ManifestError, 'body'):
                manifest.verify(loaded, keyring)

    def test_modified_and_truncated_object_detected(self):
        with Keyring(KeyMaterial.crypto(), signing=True) as keyring:
            set_id, _ = self.write_set(keyring)
        loaded = sets.load_manifest(self.backend, self.instance, set_id)
        path = os.path.join(self.root, layout.object_key(self.instance, set_id, 'db.pgdump.enc'))
        os.chmod(path, 0o600)
        with open(path, 'r+b') as f:  # flip the byte: writing a constant is a no-op when it already holds it
            f.seek(100)
            byte = f.read(1)
            f.seek(100)
            f.write(bytes([byte[0] ^ 0xff]))
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
