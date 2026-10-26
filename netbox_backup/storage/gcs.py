"""
Google Cloud Storage.

Objects are written as resumable uploads in 8 MiB chunks with the precondition ifGenerationMatch=0, so a write never
replaces an existing object; until the final chunk is sent nothing is visible, and an abandoned session expires
after a week. GCS reports the whole-object MD5 of every non-composite object, which the writer computes locally,
so level-1 checks compare our own value with the server's. Immutability comes from the bucket's retention policy,
which probe requires to be locked.
"""
import base64
import hashlib
import json

from ..core.config import resolve_secret
from .base import (
    Capabilities,
    DeleteError,
    ObjectExists,
    ObjectInfo,
    ObjectNotFound,
    ProbeReport,
    StorageBackend,
    StorageError,
    Writer,
)

MiB = 1024 * 1024
CHUNK = 8 * MiB  # a multiple of 256 KiB, as resumable uploads require


def _md5_b64_to_hex(value):
    return base64.b64decode(value).hex() if value else None


class GCSWriter(Writer):
    def __init__(self, backend, key, size_hint):
        super().__init__(key)
        self.backend = backend
        self.blob = backend.bucket.blob(backend.full_key(key), chunk_size=CHUNK)
        self.md5 = hashlib.md5(usedforsecurity=False)
        self.writer = None
        # ifGenerationMatch=0 is the guarantee; this early check also fails fast before any byte is sent
        if backend.stat(key) is not None:
            raise ObjectExists(f"Object exists: {key}")

    def write(self, data):
        if self.writer is None:
            self.writer = self.backend.call(self.blob.open, 'wb', chunk_size=CHUNK, ignore_flush=True,
                                            if_generation_match=0, content_type='application/octet-stream')
        self.md5.update(data)
        self.size += len(data)
        self.backend.call(self.writer.write, data)

    def commit(self):
        try:
            if self.writer is None:
                self.write(b'')
            self.backend.call(self.writer.close)
        except ObjectExists:
            self.closed = True
            raise ObjectExists(f"Object exists: {self.key}") from None
        except BaseException:
            self.abort()
            raise
        self.closed = True
        generation = str(self.blob.generation) if self.blob.generation else None
        return ObjectInfo(key=self.key, size=self.size, checksum=self.md5.hexdigest(), version=generation)

    def abort(self):
        # An unfinished resumable session leaves nothing visible; cancel it (the service also drops it after a week)
        self.closed = True
        if self.writer is not None and not self.writer.closed:
            try:
                self.writer.terminate()
            except Exception:
                pass


class _Reader:
    def __init__(self, backend, reader):
        self.backend = backend
        self.reader = reader

    def read(self, size=-1):
        return self.backend.call(self.reader.read, -1 if size is None or size < 0 else size)

    def readable(self):
        return True

    def close(self):
        self.reader.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class GCSBackend(StorageBackend):
    name = 'gcs'
    capabilities = Capabilities(
        streaming_write=True, create_only=True, default_retention=True, versioned=True, object_checksum='md5',
    )

    def __init__(self, target, prune=False):
        super().__init__(target, prune=prune)
        try:
            from google.cloud import storage
        except ImportError as e:
            raise StorageError("The gcs backend requires google-cloud-storage: pip install 'netbox-backup[gcs]'") from e
        self.bucket_name = self.options.get('bucket')
        if not self.bucket_name:
            raise StorageError(f"Target {target.name!r}: 'bucket' is required for the gcs backend")
        creds = target.prune_credentials if prune and target.prune_credentials else target.credentials
        endpoint = self.options.get('endpoint_url')
        kwargs = {'project': self.options.get('project')}
        if endpoint:
            kwargs['client_options'] = {'api_endpoint': endpoint}
        if creds.get('service_account_json'):
            from google.oauth2 import service_account
            info = json.loads(resolve_secret(creds['service_account_json']))
            kwargs['credentials'] = service_account.Credentials.from_service_account_info(
                info, scopes=['https://www.googleapis.com/auth/devstorage.read_write'],
            )
            kwargs['project'] = kwargs['project'] or info.get('project_id')
        elif endpoint and self.options.get('anonymous'):
            from google.auth.credentials import AnonymousCredentials
            kwargs['credentials'] = AnonymousCredentials()
            kwargs['project'] = kwargs['project'] or 'netbox-backup'
        # Otherwise: application default credentials (workload identity, GOOGLE_APPLICATION_CREDENTIALS)
        try:
            self.client = storage.Client(**kwargs)
        except Exception as e:
            raise StorageError(f"GCS: {e}") from e
        self.bucket = self.client.bucket(self.bucket_name)
        self.endpoint = (endpoint or 'https://storage.googleapis.com').rstrip('/')

    def describe(self):
        return f"gs://{self.bucket_name}/{self.target.prefix + '/' if self.target.prefix else ''}"

    def call(self, func, *args, **kwargs):
        from google.api_core import exceptions as gexc
        try:
            return func(*args, **kwargs)
        except gexc.PreconditionFailed:
            raise ObjectExists(str(args[0]) if args else '') from None
        except gexc.NotFound as e:
            raise ObjectNotFound(str(e).splitlines()[0]) from None
        except (StorageError, NotImplementedError):
            raise
        except Exception as e:
            # Resumable uploads raise InvalidResponse carrying the HTTP response
            status = getattr(getattr(e, 'response', None), 'status_code', None) or getattr(e, 'code', None)
            if status == 412:
                raise ObjectExists('') from None
            if status == 404:
                raise ObjectNotFound('') from None
            if isinstance(e, (gexc.GoogleAPIError, OSError)) or status is not None \
                    or type(e).__module__.startswith(('google.', 'requests', 'urllib3')):
                raise StorageError(f"GCS: {str(e).splitlines()[0] if str(e) else type(e).__name__}") from e
            raise

    def _info(self, blob, key=None):
        retain = blob.retention_expiration_time
        return ObjectInfo(
            key=key or self.relative_key(blob.name), size=blob.size or 0, checksum=_md5_b64_to_hex(blob.md5_hash),
            version=str(blob.generation) if blob.generation else None, retain_until=retain,
            last_modified=blob.updated, is_latest=blob.time_deleted is None,
            checksum_unavailable=None if blob.md5_hash else 'composite object: GCS reports no MD5',
        )

    def open_write(self, key, size_hint=None):
        return GCSWriter(self, key, size_hint)

    def open_read(self, key, version=None):
        if version:
            blob = self.bucket.blob(self.full_key(key), generation=int(version))
        else:
            # Pin the generation that exists now, so the whole read sees one object
            blob = self.call(self.bucket.get_blob, self.full_key(key))
            if blob is None:
                raise ObjectNotFound(f"Object not found: {key}")
            blob = self.bucket.blob(self.full_key(key), generation=blob.generation)
        try:
            return _Reader(self, self.call(blob.open, 'rb', chunk_size=CHUNK))
        except ObjectNotFound:
            raise ObjectNotFound(f"Object not found: {key}") from None

    def stat(self, key):
        blob = self.call(self.bucket.get_blob, self.full_key(key))
        return self._info(blob, key) if blob is not None else None

    def list(self, prefix, include_versions=False):
        full_prefix = self.full_key(prefix) if prefix else (self.target.prefix + '/' if self.target.prefix else '')
        blobs = self.call(lambda: list(self.client.list_blobs(self.bucket, prefix=full_prefix,
                                                               versions=include_versions)))
        for blob in blobs:
            yield self._info(blob)

    def delete(self, keys):
        errors = []
        for item in keys:
            key, version = (item, None) if isinstance(item, str) else item
            if version:
                generations = [int(version)]
            else:
                generations = [int(v.version) for v in self.list(key, include_versions=True)
                               if v.key == key and v.version] or [None]
            for generation in generations:
                blob = self.bucket.blob(self.full_key(key), generation=generation)
                try:
                    self.call(blob.delete)
                except ObjectNotFound:
                    continue
                except StorageError as e:
                    errors.append(DeleteError(key, str(generation) if generation else None, str(e)))
        return errors

    def abort_incomplete(self, older_than):
        return 0  # Resumable sessions that never finalized expire after a week and were never visible

    def copy_from(self, source, key, size):
        """
        Server-side rewrite from another GCS bucket, create-only. The new object's MD5 is the source's.
        """
        if not isinstance(source, GCSBackend) or source.endpoint != self.endpoint:
            raise NotImplementedError
        src = source.bucket.blob(source.full_key(key))
        dst = self.bucket.blob(self.full_key(key))
        token = None
        while True:
            token, _, _ = self.call(dst.rewrite, src, token=token, if_generation_match=0)
            if token is None:
                break
        return {'part_size': None}

    def probe(self, under=''):
        report = ProbeReport()
        try:
            self.call(self.bucket.reload)
            report.check('bucket reachable', True, self.describe())
        except StorageError as e:
            report.check('bucket reachable', False, str(e))
            return report
        report.versioned = bool(self.bucket.versioning_enabled)
        period = self.bucket.retention_period
        report.default_retention_days = int(period // 86400) if period else None
        report.retention_locked = bool(self.bucket.retention_policy_locked)
        if self.target.retention_days:
            report.check(
                'bucket retention policy (locked)',
                report.retention_locked and (report.default_retention_days or 0) >= self.target.retention_days,
                f"bucket: {report.default_retention_days or 'none'} day(s), "
                f"{'locked' if report.retention_locked else 'not locked'}; target expects "
                f"{self.target.retention_days}",
            )
        else:
            report.check('default retention', bool(period), 'no retention_days configured; sets on this target are '
                                                            'not immutable', required=False)
        report.abort_incomplete_days = 7
        self._probe_roundtrip(report, self.probe_key(under))
        return report
