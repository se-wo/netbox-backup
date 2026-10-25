"""
The set manifest (manifest.json, schema version 1).

Two parts: a signed plaintext header and an encrypted body. Listing and integrity checks need only the header;
the body (versions, migration state, baseline, creator) needs a decryption identity.

    {
      "format": "netbox-backup/manifest",
      "schema": 1,
      "header": {...},                # set_id, instance_id, created_at, recipients, objects, body_sha384, ...
      "signature": "-----BEGIN PGP SIGNATURE-----...",   # detached, over the canonical header bytes
      "body": "<base64 of the gpg-encrypted body JSON>"
    }

The header carries the encrypted body's SHA-384, so the signature covers the body too. _COMPLETE, written last,
holds the SHA-384 of manifest.json, so a replaced manifest is detected.
"""
import base64
import hashlib
import json
from dataclasses import dataclass

FORMAT = 'netbox-backup/manifest'
SCHEMA = 1


class ManifestError(Exception):
    pass


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def sha384(data):
    return hashlib.sha384(data).hexdigest()


@dataclass
class Manifest:
    header: dict
    signature: bytes
    body_ciphertext: bytes
    raw: bytes

    @property
    def set_id(self):
        return self.header['set_id']

    @property
    def instance_id(self):
        return self.header['instance_id']

    @property
    def objects(self):
        return self.header.get('objects', {})

    @property
    def sha384(self):
        return sha384(self.raw)


def build(header, body, keyring):
    """
    Encrypt the body, sign the header and serialize manifest.json. Returns its bytes.
    """
    body_ciphertext = keyring.encrypt_bytes(canonical(body))
    header = {**header, 'body_sha384': sha384(body_ciphertext)}
    signature = keyring.sign_detached(canonical(header))
    document = {
        'format': FORMAT,
        'schema': SCHEMA,
        'header': header,
        'signature': signature.decode('ascii'),
        'body': base64.b64encode(body_ciphertext).decode('ascii'),
    }
    return json.dumps(document, indent=1, sort_keys=True).encode()


def parse(raw):
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise ManifestError(f"manifest.json is not valid JSON: {e}") from e
    if not isinstance(document, dict) or document.get('format') != FORMAT:
        raise ManifestError("Not a netbox-backup manifest")
    if document.get('schema') != SCHEMA:
        raise ManifestError(f"Unsupported manifest schema {document.get('schema')!r}; upgrade netbox-backup")
    try:
        return Manifest(
            header=document['header'],
            signature=document['signature'].encode('ascii'),
            body_ciphertext=base64.b64decode(document['body']),
            raw=raw,
        )
    except (KeyError, ValueError, AttributeError) as e:
        raise ManifestError(f"Malformed manifest: {e}") from e


def verify(manifest, keyring, signers=None):
    """
    Check the header signature against the pinned signers and the body hash against the header. Returns the
    signer's fingerprint.
    """
    signer = keyring.verify_detached(canonical(manifest.header), manifest.signature, signers=signers)
    if sha384(manifest.body_ciphertext) != manifest.header.get('body_sha384'):
        raise ManifestError("Manifest body does not match the signed header (body_sha384)")
    return signer


def decrypt_body(manifest, keyring):
    """
    Decrypt the body. Call verify() first: the body is trusted only through the signed header.
    """
    try:
        return json.loads(keyring.decrypt_bytes(manifest.body_ciphertext))
    except ValueError as e:
        raise ManifestError(f"Manifest body is not valid JSON: {e}") from e


def complete_marker(manifest_raw):
    return (sha384(manifest_raw) + '\n').encode()


def check_complete(manifest_raw, marker):
    expected = complete_marker(manifest_raw).strip()
    if marker.strip() != expected:
        raise ManifestError("_COMPLETE does not match manifest.json: the manifest was replaced or truncated")
