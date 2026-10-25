"""
S3 and S3-compatible stores (AWS, MinIO, Ceph RGW, Wasabi, Hetzner, IONOS).

Uploads stream in parts that start at 8 MiB and grow, so neither memory nor the 10,000-part limit depends on an
accurate size hint. Every request carries Content-MD5, and the ETag is computed locally from the part digests so
level-1 checks compare our own value with the server's. Immutability comes from the bucket's default retention in
compliance mode; this driver never sets or extends locks.
"""
import base64
import hashlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from ..core.config import resolve_secret
from .base import Capabilities, DeleteError, ObjectExists, ObjectInfo, ObjectNotFound, ProbeReport, StorageBackend, \
    StorageError, Writer, md5_etag

MiB = 1024 * 1024
MIN_PART = 8 * MiB
MAX_PARTS = 10000


def _client_error_code(error):
    return getattr(error, 'response', {}).get('Error', {}).get('Code', '')


def part_size_for(size_hint, part_number=1):
    """
    Part size for a given part: at least 8 MiB, large enough for the hinted size to fit in 9,000 parts, and doubling
    every 1,000 parts so an object bigger than its hint still fits below the 10,000-part limit.
    """
    base = MIN_PART
    if size_hint:
        base = max(MIN_PART, -(-size_hint // 9000))
        base = -(-base // MiB) * MiB
    return base * (2 ** ((part_number - 1) // 1000))


class S3Writer(Writer):
    def __init__(self, backend, key, size_hint):
        super().__init__(key)
        self.backend = backend
        self.client = backend.client
        self.bucket = backend.bucket
        self.full_key = backend.full_key(key)
        self.size_hint = size_hint
        self.buffer = bytearray()
        self.upload_id = None
        self.parts = {}
        self.digests = {}
        self.concurrency = int(backend.options.get('concurrency', 2))
        self.pool = None
        self.slots = threading.BoundedSemaphore(self.concurrency)
        self.futures = []
        self.part_number = 0

    def _md5(self, data):
        digest = hashlib.md5(data, usedforsecurity=False).digest()
        return digest, base64.b64encode(digest).decode()

    def write(self, data):
        self.buffer += data
        self.size += len(data)
        while len(self.buffer) >= part_size_for(self.size_hint, self.part_number + 1):
            size = part_size_for(self.size_hint, self.part_number + 1)
            chunk, self.buffer = bytes(self.buffer[:size]), self.buffer[size:]
            self._upload_part(chunk)

    def _upload_part(self, chunk):
        if self.upload_id is None:
            response = self.backend.call(
                self.client.create_multipart_upload, Bucket=self.bucket, Key=self.full_key,
                **self.backend.put_args(),
            )
            self.upload_id = response['UploadId']
            self.pool = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix='nbb-s3')
        self.part_number += 1
        if self.part_number > MAX_PARTS:
            raise StorageError(f"{self.key}: more than {MAX_PARTS} parts")
        number = self.part_number
        # Bounded in-flight parts: memory stays at part size x (concurrency + 1)
        self.slots.acquire()
        self.futures.append(self.pool.submit(self._send_part, number, chunk))

    def _send_part(self, number, chunk):
        try:
            digest, b64 = self._md5(chunk)
            response = self.backend.call(
                self.client.upload_part, Bucket=self.bucket, Key=self.full_key, UploadId=self.upload_id,
                PartNumber=number, Body=chunk, ContentMD5=b64, ContentLength=len(chunk),
            )
            self.parts[number] = response['ETag']
            self.digests[number] = digest
        finally:
            self.slots.release()

    def commit(self):
        try:
            if self.upload_id is None:
                data = bytes(self.buffer)
                digest, b64 = self._md5(data)
                try:
                    self.backend.call(
                        self.client.put_object, Bucket=self.bucket, Key=self.full_key, Body=data, ContentMD5=b64,
                        ContentLength=len(data), IfNoneMatch='*', **self.backend.put_args(),
                    )
                except ObjectExists:
                    raise ObjectExists(f"Object exists: {self.key}") from None
                checksum = md5_etag([digest], multipart=False)
            else:
                if self.buffer:
                    self._upload_part(bytes(self.buffer))
                    self.buffer = bytearray()
                for future in self.futures:
                    future.result()
                parts = [{'ETag': self.parts[n], 'PartNumber': n} for n in sorted(self.parts)]
                try:
                    self.backend.call(
                        self.client.complete_multipart_upload, Bucket=self.bucket, Key=self.full_key,
                        UploadId=self.upload_id, MultipartUpload={'Parts': parts}, IfNoneMatch='*',
                    )
                except ObjectExists:
                    self.abort()
                    raise ObjectExists(f"Object exists: {self.key}") from None
                checksum = md5_etag([self.digests[n] for n in sorted(self.digests)], multipart=True)
        except BaseException:
            if not self.closed:
                self.abort()
            raise
        finally:
            if self.pool:
                self.pool.shutdown(wait=True)
        self.closed = True
        return ObjectInfo(key=self.key, size=self.size, checksum=checksum)

    def abort(self):
        self.closed = True
        self.buffer = bytearray()
        if self.pool:
            for future in self.futures:
                future.cancel()
            self.pool.shutdown(wait=True)
        if self.upload_id:
            try:
                self.client.abort_multipart_upload(Bucket=self.bucket, Key=self.full_key, UploadId=self.upload_id)
            except Exception:
                pass  # The bucket's lifecycle rule reclaims it after a day


class _Body:
    """
    File-like wrapper around a botocore StreamingBody, usable with shutil.copyfileobj and as a context manager.
    """
    def __init__(self, body):
        self.body = body

    def read(self, size=-1):
        return self.body.read(None if size is None or size < 0 else size)

    def readable(self):
        return True

    def close(self):
        self.body.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class S3Backend(StorageBackend):
    name = 's3'

    def __init__(self, target, prune=False):
        super().__init__(target, prune=prune)
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as e:
            raise StorageError("The s3 backend requires boto3: pip install 'netbox-backup[s3]'") from e
        self.bucket = self.options.get('bucket')
        if not self.bucket:
            raise StorageError(f"Target {target.name!r}: 'bucket' is required for the s3 backend")
        creds = target.prune_credentials if prune and target.prune_credentials else target.credentials
        session_kwargs = {}
        if creds.get('access_key'):
            session_kwargs['aws_access_key_id'] = resolve_secret(creds['access_key'])
            session_kwargs['aws_secret_access_key'] = resolve_secret(creds.get('secret_key'))
            if creds.get('session_token'):
                session_kwargs['aws_session_token'] = resolve_secret(creds['session_token'])
        elif creds.get('profile'):
            session_kwargs['profile_name'] = resolve_secret(creds['profile'])
        checksum_mode = self.options.get('checksum_mode', 'when_supported')
        if checksum_mode not in ('when_supported', 'when_required'):
            raise StorageError(f"Target {target.name!r}: checksum_mode must be when_supported or when_required")
        config = BotoConfig(
            request_checksum_calculation=checksum_mode,
            response_checksum_validation=checksum_mode,
            retries={'max_attempts': 5, 'mode': 'standard'},
            s3={'addressing_style': self.options.get('addressing_style', 'auto')},
        )
        session = boto3.session.Session(**session_kwargs)
        self.client = session.client(
            's3', region_name=self.options.get('region'), endpoint_url=self.options.get('endpoint_url'),
            verify=self.options.get('verify', True), config=config,
        )
        self.sse = self.options.get('sse')
        # SSE-KMS and SSE-C ETags are not MD5s; size-only level-1 checks are the honest result there
        checksum = None if self.sse == 'aws:kms' else 'etag-md5'
        self.capabilities = Capabilities(
            streaming_write=True, create_only=True, default_retention=True, versioned=True,
            object_checksum=checksum,
        )

    def describe(self):
        return f"s3://{self.bucket}/{self.target.prefix + '/' if self.target.prefix else ''}"

    def put_args(self):
        args = {}
        if self.sse:
            args['ServerSideEncryption'] = self.sse
            if self.options.get('kms_key_id'):
                args['SSEKMSKeyId'] = self.options['kms_key_id']
        if self.options.get('storage_class'):
            args['StorageClass'] = self.options['storage_class']
        return args

    def call(self, func, **kwargs):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            return func(**kwargs)
        except ClientError as e:
            code = _client_error_code(e)
            if code in ('PreconditionFailed', 'ConditionalRequestConflict'):
                raise ObjectExists(kwargs.get('Key', '')) from None
            if code in ('NoSuchKey', '404', 'NotFound'):
                raise ObjectNotFound(kwargs.get('Key', '')) from None
            raise StorageError(f"S3 {code or 'error'}: {e}") from e
        except BotoCoreError as e:
            raise StorageError(f"S3: {e}") from e

    def open_write(self, key, size_hint=None):
        return S3Writer(self, key, size_hint)

    def open_read(self, key, version=None):
        kwargs = {'Bucket': self.bucket, 'Key': self.full_key(key)}
        if version:
            kwargs['VersionId'] = version
        try:
            response = self.call(self.client.get_object, **kwargs)
        except ObjectNotFound:
            raise ObjectNotFound(f"Object not found: {key}") from None
        return _Body(response['Body'])

    def stat(self, key):
        try:
            head = self.call(self.client.head_object, Bucket=self.bucket, Key=self.full_key(key))
        except ObjectNotFound:
            return None
        return ObjectInfo(
            key=key, size=head['ContentLength'], checksum=head.get('ETag', '').strip('"') or None,
            version=head.get('VersionId'), retain_until=head.get('ObjectLockRetainUntilDate'),
            last_modified=head.get('LastModified'),
        )

    def list(self, prefix, include_versions=False):
        full_prefix = self.full_key(prefix) if prefix else (self.target.prefix + '/' if self.target.prefix else '')
        if include_versions:
            paginator = self.client.get_paginator('list_object_versions')
            for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
                for v in page.get('Versions', []):
                    yield ObjectInfo(
                        key=self.relative_key(v['Key']), size=v['Size'], checksum=v.get('ETag', '').strip('"'),
                        version=v.get('VersionId'), last_modified=v.get('LastModified'), is_latest=v.get('IsLatest'),
                    )
                for m in page.get('DeleteMarkers', []):
                    yield ObjectInfo(
                        key=self.relative_key(m['Key']), size=0, version=m.get('VersionId'),
                        last_modified=m.get('LastModified'), is_latest=m.get('IsLatest'), delete_marker=True,
                    )
            return
        paginator = self.client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get('Contents', []):
                yield ObjectInfo(
                    key=self.relative_key(obj['Key']), size=obj['Size'], checksum=obj.get('ETag', '').strip('"'),
                    last_modified=obj.get('LastModified'),
                )

    def delete(self, keys):
        errors = []
        objects = []
        for item in keys:
            key, version = (item, None) if isinstance(item, str) else item
            if version:
                objects.append({'Key': self.full_key(key), 'VersionId': version})
                continue
            versions = [v for v in self.list(key, include_versions=True) if v.key == key]
            if not versions:
                objects.append({'Key': self.full_key(key)})
            objects += [{'Key': self.full_key(key), 'VersionId': v.version} for v in versions]
        for start in range(0, len(objects), 1000):
            batch = objects[start:start + 1000]
            try:
                response = self.call(
                    self.client.delete_objects, Bucket=self.bucket, Delete={'Objects': batch, 'Quiet': True},
                )
            except StorageError as e:
                errors += [DeleteError(self.relative_key(o['Key']), o.get('VersionId'), str(e)) for o in batch]
                continue
            for err in response.get('Errors', []):
                errors.append(DeleteError(
                    self.relative_key(err['Key']), err.get('VersionId'), f"{err.get('Code')}: {err.get('Message')}",
                ))
        return errors

    def abort_incomplete(self, older_than):
        cutoff = datetime.now(UTC) - older_than
        removed = 0
        prefix = self.target.prefix + '/' if self.target.prefix else ''
        paginator = self.client.get_paginator('list_multipart_uploads')
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for upload in page.get('Uploads', []):
                if upload['Initiated'] < cutoff:
                    self.client.abort_multipart_upload(
                        Bucket=self.bucket, Key=upload['Key'], UploadId=upload['UploadId'],
                    )
                    removed += 1
        return removed

    def probe(self):
        report = ProbeReport()
        try:
            versioning = self.call(self.client.get_bucket_versioning, Bucket=self.bucket)
            report.versioned = versioning.get('Status') == 'Enabled'
            report.check('bucket reachable', True, self.describe())
        except StorageError as e:
            report.check('bucket reachable', False, str(e))
            return report
        report.check('versioning', report.versioned, 'object lock requires a versioned bucket',
                     required=bool(self.target.retention_days))

        # Default retention in compliance mode: the only source of immutability
        try:
            lock = self.call(self.client.get_object_lock_configuration, Bucket=self.bucket)
            rule = lock.get('ObjectLockConfiguration', {}).get('Rule', {}).get('DefaultRetention', {})
            days = rule.get('Days') or (rule.get('Years', 0) * 365) or None
            report.default_retention_days = days
            report.retention_locked = rule.get('Mode') == 'COMPLIANCE'
        except StorageError:
            report.retention_locked = False
        if self.target.retention_days:
            report.check(
                'default retention (compliance mode)',
                report.retention_locked and (report.default_retention_days or 0) >= self.target.retention_days,
                f"bucket: {report.default_retention_days or 'none'} day(s), "
                f"{'COMPLIANCE' if report.retention_locked else 'not compliance mode'}; "
                f"target expects {self.target.retention_days}",
            )
        else:
            report.check('default retention', bool(report.retention_locked),
                         'no retention_days configured; sets on this target are not immutable', required=False)

        # Lifecycle rule that aborts incomplete multipart uploads after one day
        try:
            lifecycle = self.call(self.client.get_bucket_lifecycle_configuration, Bucket=self.bucket)
            days = [r['AbortIncompleteMultipartUpload']['DaysAfterInitiation']
                    for r in lifecycle.get('Rules', [])
                    if r.get('Status') == 'Enabled' and 'AbortIncompleteMultipartUpload' in r]
            report.abort_incomplete_days = min(days) if days else None
        except StorageError:
            report.abort_incomplete_days = None
        report.check(
            'lifecycle aborts incomplete uploads', report.abort_incomplete_days == 1,
            f"AbortIncompleteMultipartUpload after {report.abort_incomplete_days or 'never'} day(s); expected 1",
        )
        self._probe_roundtrip(report, f'.probe/{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}')
        return report
