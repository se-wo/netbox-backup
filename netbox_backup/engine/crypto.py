"""
Streaming authenticated encryption for backup artifacts.

File format (all integers big-endian):

    MAGIC (8) | salt (16) | nonce_prefix (8) | chunk*

    chunk := length (4) | AES-256-GCM ciphertext+tag

Each chunk is encrypted with nonce = nonce_prefix || counter (4) and associated data
header || counter (8) || final (1). The final flag on the last chunk makes truncation detectable, and the counter
makes reordering detectable. The key is derived from the configured passphrase with scrypt and a per-file salt.
"""
import hashlib
import os
import struct

MAGIC = b'NBBKENC1'
CHUNK_SIZE = 1024 * 1024
SALT_SIZE = 16
PREFIX_SIZE = 8
TAG_SIZE = 16
SCRYPT_PARAMS = {'n': 2 ** 15, 'r': 8, 'p': 1}


class DecryptionError(Exception):
    pass


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:
        raise RuntimeError(
            "Backup encryption requires the 'cryptography' package: pip install 'netbox-backup[encryption]'"
        ) from e
    return AESGCM


def derive_key(passphrase, salt):
    return hashlib.scrypt(passphrase.encode(), salt=salt, dklen=32, maxmem=64 * 1024 * 1024, **SCRYPT_PARAMS)


def key_fingerprint(passphrase):
    """
    Short, non-reversible identifier of a passphrase, recorded in manifests so a restore can tell an operator
    that they supplied the wrong key before attempting to decrypt anything.
    """
    return derive_key(passphrase, b'netbox-backup-key-fingerprint')[:6].hex()


def is_encrypted(path):
    with open(path, 'rb') as f:
        return f.read(len(MAGIC)) == MAGIC


def encrypt_file(src, dest, passphrase):
    AESGCM = _aesgcm()
    salt = os.urandom(SALT_SIZE)
    prefix = os.urandom(PREFIX_SIZE)
    header = MAGIC + salt + prefix
    aead = AESGCM(derive_key(passphrase, salt))
    with open(src, 'rb') as fin, open(dest, 'wb') as fout:
        fout.write(header)
        counter = 0
        chunk = fin.read(CHUNK_SIZE)
        while True:
            following = fin.read(CHUNK_SIZE) if chunk else b''
            final = not following
            nonce = prefix + struct.pack('>I', counter)
            aad = header + struct.pack('>QB', counter, int(final))
            ciphertext = aead.encrypt(nonce, chunk, aad)
            fout.write(struct.pack('>I', len(ciphertext)))
            fout.write(ciphertext)
            if final:
                break
            chunk = following
            counter += 1
            if counter >= 2 ** 32:
                raise ValueError("File too large to encrypt")


def decrypt_file(src, dest, passphrase):
    AESGCM = _aesgcm()
    from cryptography.exceptions import InvalidTag

    with open(src, 'rb') as fin, open(dest, 'wb') as fout:
        header = fin.read(len(MAGIC) + SALT_SIZE + PREFIX_SIZE)
        if len(header) != len(MAGIC) + SALT_SIZE + PREFIX_SIZE or not header.startswith(MAGIC):
            raise DecryptionError("Not a netbox-backup encrypted file")
        salt = header[len(MAGIC):len(MAGIC) + SALT_SIZE]
        prefix = header[len(MAGIC) + SALT_SIZE:]
        aead = AESGCM(derive_key(passphrase, salt))
        counter = 0
        while True:
            length_bytes = fin.read(4)
            if len(length_bytes) != 4:
                raise DecryptionError("Encrypted file is truncated (missing final chunk)")
            (length,) = struct.unpack('>I', length_bytes)
            if length < TAG_SIZE or length > CHUNK_SIZE + TAG_SIZE:
                raise DecryptionError("Encrypted file is corrupt (invalid chunk length)")
            ciphertext = fin.read(length)
            if len(ciphertext) != length:
                raise DecryptionError("Encrypted file is truncated")
            nonce = prefix + struct.pack('>I', counter)
            plaintext = None
            for final in (0, 1):
                try:
                    plaintext = aead.decrypt(nonce, ciphertext, header + struct.pack('>QB', counter, final))
                    break
                except InvalidTag:
                    continue
            if plaintext is None:
                raise DecryptionError("Decryption failed: wrong encryption key or corrupted file")
            fout.write(plaintext)
            if final:
                if fin.read(1):
                    raise DecryptionError("Encrypted file has trailing data after the final chunk")
                return
            counter += 1
