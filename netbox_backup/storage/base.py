"""
The storage driver interface.

Every backend implements one small streaming interface and declares what it can do; the job engine adapts to the
capabilities instead of assuming S3. Keys are '/'-separated and relative to the target's prefix.

Writes are create-only: an existing key is never overwritten, so two installs can't claim one path and a
replaced object is an error, not a silent success. A writer becomes visible only on commit(); abort() (or a crash)
leaves at most an incomplete upload that abort_incomplete() reclaims.
"""
import hashlib
import posixpath
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import BinaryIO, NamedTuple

__all__ = (
    'Capabilities', 'DeleteError', 'ObjectExists', 'ObjectInfo', 'ObjectNotFound', 'ProbeReport', 'StorageBackend',
    'StorageError', 'Writer',
)


class StorageError(Exception):
    pass


class ObjectExists(StorageError):
    pass


class ObjectNotFound(StorageError):
    pass


class Capabilities(NamedTuple):
    streaming_write: bool        # accepts bytes of unknown length (else: spool first)
    create_only: bool            # refuses to overwrite an existing key (If-None-Match, O_EXCL)
    default_retention: bool      # bucket/container default lock in compliance mode, verified by probe
    versioned: bool              # deletes leave versions behind; listing can include them
    object_checksum: str | None  # checksum the server reports and the writer computes locally; None = size only


@dataclass
class ObjectInfo:
    key: str
    size: int
    checksum: str | None = None
    version: str | None = None
    retain_until: datetime | None = None
    last_modified: datetime | None = None
    is_latest: bool = True
    delete_marker: bool = False


@dataclass
class DeleteError:
    key: str
    version: str | None
    reason: str


@dataclass
class ProbeReport:
    ok: bool = True
    checks: list = field(default_factory=list)
    object_checksum: str | None = None
    create_only: bool = False
    default_retention_days: int | None = None
    retention_locked: bool | None = None
    abort_incomplete_days: int | None = None
    versioned: bool | None = None

    def check(self, name, ok, detail='', required=True):
        self.checks.append({'check': name, 'ok': bool(ok), 'detail': detail, 'required': required})
        if required and not ok:
            self.ok = False
        return ok

    def as_dict(self):
        return {
            'ok': self.ok, 'checks': self.checks, 'object_checksum': self.object_checksum,
            'create_only': self.create_only, 'default_retention_days': self.default_retention_days,
            'retention_locked': self.retention_locked, 'abort_incomplete_days': self.abort_incomplete_days,
            'versioned': self.versioned,
        }


class Writer:
    """
    A create-only upload in progress. write() accepts any number of chunks; commit() makes the object visible and
    returns its ObjectInfo, with checksum set to the locally computed value of the backend's checksum type.
    """
    def __init__(self, key):
        self.key = key
        self.size = 0
        self.closed = False

    def write(self, data):
        raise NotImplementedError

    def commit(self) -> ObjectInfo:
        raise NotImplementedError

    def abort(self):
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.closed:
            self.abort()


class StorageBackend:
    name: str = None
    capabilities: Capabilities = Capabilities(False, False, False, False, None)

    def __init__(self, target, prune=False):
        """
        target is a TargetConfig. With prune=True the driver uses the target's prune credentials, which are the
        only ones allowed to delete.
        """
        self.target = target
        self.prune = prune
        self.options = dict(target.options)

    # Keys

    @staticmethod
    def join(*parts):
        parts = [str(p).strip('/') for p in parts if p]
        return posixpath.join(*parts) if parts else ''

    def full_key(self, key):
        key = str(key).lstrip('/')
        if any(part in ('..', '.') for part in key.split('/')):
            raise StorageError(f"Invalid key: {key}")
        return self.join(self.target.prefix, key)

    def relative_key(self, full_key):
        prefix = self.target.prefix.strip('/')
        if prefix and full_key.startswith(prefix + '/'):
            return full_key[len(prefix) + 1:]
        return full_key

    def describe(self):
        """
        Human-readable, credential-free location of this target, e.g. s3://bucket/prefix/.
        """
        raise NotImplementedError

    # Interface

    def open_write(self, key: str, size_hint: int | None = None) -> Writer:
        raise NotImplementedError

    def open_read(self, key: str, version: str | None = None) -> BinaryIO:
        raise NotImplementedError

    def stat(self, key: str) -> ObjectInfo | None:
        raise NotImplementedError

    def list(self, prefix: str, include_versions: bool = False) -> Iterator[ObjectInfo]:
        raise NotImplementedError

    def delete(self, keys: Sequence) -> list:
        """
        Delete objects (prune runner only). Items are keys or (key, version) tuples; on versioned targets an
        unversioned key deletes every version. Returns a DeleteError per object that could not be deleted.
        """
        raise NotImplementedError

    def abort_incomplete(self, older_than: timedelta) -> int:
        raise NotImplementedError

    def probe(self) -> ProbeReport:
        raise NotImplementedError

    # Conveniences built on the interface

    def put_bytes(self, key, data) -> ObjectInfo:
        with self.open_write(key, size_hint=len(data)) as writer:
            writer.write(data)
            return writer.commit()

    def get_bytes(self, key, version=None) -> bytes:
        with self.open_read(key, version=version) as f:
            return f.read()

    def exists(self, key) -> bool:
        return self.stat(key) is not None

    def _probe_roundtrip(self, report, key):
        """
        Shared part of probe(): write, read back, compare the reported checksum, and try an overwrite.
        """
        payload = f'netbox-backup probe {key}\n'.encode()
        try:
            written = self.put_bytes(key, payload)
            report.check('write', True, key)
        except StorageError as e:
            report.check('write', False, str(e))
            return
        try:
            report.check('read back', self.get_bytes(key) == payload)
        except StorageError as e:
            report.check('read back', False, str(e))
        info = self.stat(key)
        report.check('stat', info is not None and info.size == len(payload))
        if self.capabilities.object_checksum and info is not None:
            matches = info.checksum is not None and info.checksum == written.checksum
            report.object_checksum = self.capabilities.object_checksum if matches else None
            report.check(
                f'server reports {self.capabilities.object_checksum}', matches,
                '' if matches else 'level-1 checks fall back to size only', required=False,
            )
        try:
            self.put_bytes(key, b'overwrite')
            report.create_only = False
            report.check('create-only writes', False, 'an existing object was overwritten')
        except ObjectExists:
            report.create_only = True
            report.check('create-only writes', True)


def md5_etag(part_digests, multipart):
    """
    S3's ETag, computed locally from the MD5 digests of the uploaded parts: the plain MD5 for a single PutObject,
    else MD5 over the concatenated part digests plus the part count. Valid for SSE-S3 and unencrypted objects;
    probe detects stores where it isn't.
    """
    if not multipart:
        return part_digests[0].hex()
    combined = hashlib.md5(b''.join(part_digests), usedforsecurity=False).hexdigest()
    return f'{combined}-{len(part_digests)}'
