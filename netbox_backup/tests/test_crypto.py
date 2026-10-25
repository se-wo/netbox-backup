import os
import tempfile

from django.test import SimpleTestCase

from netbox_backup.engine import crypto


class CryptoTest(SimpleTestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.plain = os.path.join(self.dir.name, 'plain')
        self.enc = os.path.join(self.dir.name, 'enc')
        self.out = os.path.join(self.dir.name, 'out')

    def tearDown(self):
        self.dir.cleanup()

    def roundtrip(self, data):
        with open(self.plain, 'wb') as f:
            f.write(data)
        crypto.encrypt_file(self.plain, self.enc, 'secret')
        self.assertTrue(crypto.is_encrypted(self.enc))
        crypto.decrypt_file(self.enc, self.out, 'secret')
        with open(self.out, 'rb') as f:
            self.assertEqual(f.read(), data)

    def test_roundtrip_sizes(self):
        for size in (0, 1, crypto.CHUNK_SIZE - 1, crypto.CHUNK_SIZE, crypto.CHUNK_SIZE + 1, 3 * crypto.CHUNK_SIZE):
            with self.subTest(size=size):
                self.roundtrip(os.urandom(size))

    def test_wrong_key(self):
        self.roundtrip(b'hello')
        with self.assertRaisesRegex(crypto.DecryptionError, 'wrong encryption key'):
            crypto.decrypt_file(self.enc, self.out, 'not-the-key')

    def test_truncation_detected(self):
        self.roundtrip(os.urandom(2 * crypto.CHUNK_SIZE + 10))
        with open(self.enc, 'rb') as f:
            data = f.read()
        # Drop the final chunk entirely: every remaining chunk is intact but the file must still be rejected
        final_len = 4 + 10 + crypto.TAG_SIZE
        with open(self.enc, 'wb') as f:
            f.write(data[:-final_len])
        with self.assertRaises(crypto.DecryptionError):
            crypto.decrypt_file(self.enc, self.out, 'secret')

    def test_tampering_detected(self):
        self.roundtrip(os.urandom(1000))
        with open(self.enc, 'r+b') as f:
            f.seek(100)
            byte = f.read(1)
            f.seek(100)
            f.write(bytes([byte[0] ^ 0xFF]))
        with self.assertRaises(crypto.DecryptionError):
            crypto.decrypt_file(self.enc, self.out, 'secret')

    def test_trailing_data_detected(self):
        self.roundtrip(b'abc')
        with open(self.enc, 'ab') as f:
            f.write(b'junk')
        with self.assertRaises(crypto.DecryptionError):
            crypto.decrypt_file(self.enc, self.out, 'secret')

    def test_fingerprint_stable_and_distinct(self):
        self.assertEqual(crypto.key_fingerprint('a'), crypto.key_fingerprint('a'))
        self.assertNotEqual(crypto.key_fingerprint('a'), crypto.key_fingerprint('b'))
