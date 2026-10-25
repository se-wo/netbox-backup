"""
Azure Blob Storage.

Blobs are uploaded as staged blocks with a transactional MD5 each and committed create-only. Block size follows the
size hint, because the SDK default caps a blob near 195 GiB. Immutability comes from the container's default
version-level immutability policy (not available on hierarchical-namespace accounts), which probe verifies on a
real blob. Azure reports no whole-blob checksum for block uploads, so level-1 checks are size only.
"""
import base64
import uuid
from datetime import UTC, datetime, timedelta

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
MIN_BLOCK = 8 * MiB
MAX_BLOCKS = 50000


def block_size_for(size_hint, block_number=1):
    base = MIN_BLOCK
    if size_hint:
        base = max(MIN_BLOCK, -(-size_hint // (45000 * MiB)) * MiB)
    return min(base * (2 ** ((block_number - 1) // 5000)), 4000 * MiB)


class AzureWriter(Writer):
    def __init__(self, backend, key, size_hint):
        super().__init__(key)
        self.backend = backend
        self.blob = backend.container.get_blob_client(backend.full_key(key))
        self.size_hint = size_hint
        self.buffer = bytearray()
        self.blocks = []

    def write(self, data):
        self.buffer += data
        self.size += len(data)
        while len(self.buffer) >= block_size_for(self.size_hint, len(self.blocks) + 1):
            size = block_size_for(self.size_hint, len(self.blocks) + 1)
            chunk, self.buffer = bytes(self.buffer[:size]), self.buffer[size:]
            self._stage(chunk)

    def _stage(self, chunk):
        if len(self.blocks) >= MAX_BLOCKS:
            raise StorageError(f"{self.key}: more than {MAX_BLOCKS} blocks")
        block_id = base64.b64encode(f'{len(self.blocks):08d}'.encode()).decode()
        self.backend.call(self.blob.stage_block, block_id, chunk, length=len(chunk), validate_content=True)
        self.blocks.append(block_id)

    def commit(self):
        from azure.core import MatchConditions
        from azure.storage.blob import BlobBlock
        try:
            if not self.blocks:
                self.backend.call(
                    self.blob.upload_blob, bytes(self.buffer), overwrite=False, validate_content=True,
                    standard_blob_tier=self.backend.tier,
                )
            else:
                if self.buffer:
                    self._stage(bytes(self.buffer))
                self.backend.call(
                    self.blob.commit_block_list, [BlobBlock(block_id=b) for b in self.blocks],
                    match_condition=MatchConditions.IfMissing, standard_blob_tier=self.backend.tier,
                )
        except ObjectExists:
            self.closed = True
            raise ObjectExists(f"Object exists: {self.key}") from None
        finally:
            self.buffer = bytearray()
        self.closed = True
        return ObjectInfo(key=self.key, size=self.size)

    def abort(self):
        # Uncommitted blocks are discarded by the service after seven days
        self.closed = True
        self.buffer = bytearray()


class AzureBlobBackend(StorageBackend):
    name = 'azure_blob'
    capabilities = Capabilities(
        streaming_write=True, create_only=True, default_retention=True, versioned=True, object_checksum=None,
    )

    def __init__(self, target, prune=False):
        super().__init__(target, prune=prune)
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError as e:
            raise StorageError(
                "The azure_blob backend requires azure-storage-blob: pip install 'netbox-backup[azure]'"
            ) from e
        container = self.options.get('container')
        if not container:
            raise StorageError(f"Target {target.name!r}: 'container' is required for the azure_blob backend")
        creds = target.prune_credentials if prune and target.prune_credentials else target.credentials
        account_url = self.options.get('account_url')
        if not account_url and self.options.get('account_name'):
            account_url = f"https://{self.options['account_name']}.blob.core.windows.net"
        kwargs = {}
        if self.options.get('verify') is False:
            kwargs['connection_verify'] = False
        if creds.get('connection_string'):
            service = BlobServiceClient.from_connection_string(resolve_secret(creds['connection_string']), **kwargs)
        else:
            if not account_url:
                raise StorageError(f"Target {target.name!r}: 'account_url' or 'account_name' is required")
            if creds.get('account_key'):
                credential = {
                    'account_name': self.options.get('account_name') or account_url.split('//')[1].split('.')[0],
                    'account_key': resolve_secret(creds['account_key']),
                }
            elif creds.get('sas_token'):
                credential = resolve_secret(creds['sas_token'])
            else:
                try:
                    from azure.identity import DefaultAzureCredential
                except ImportError as e:
                    raise StorageError(
                        "Managed or workload identity requires azure-identity: pip install 'netbox-backup[azure]'"
                    ) from e
                client_id = creds.get('client_id')
                credential = DefaultAzureCredential(
                    managed_identity_client_id=resolve_secret(client_id) if client_id else None,
                )
            service = BlobServiceClient(account_url, credential=credential, **kwargs)
        self.service = service
        self.container = service.get_container_client(container)
        self.container_name = container
        # Hot tier: Cool and Cold charge early-deletion fees, which a rolling retention always triggers
        from azure.storage.blob import StandardBlobTier
        self.tier = StandardBlobTier(self.options.get('tier', 'Hot'))

    def describe(self):
        return f"{self.container.url}/{self.target.prefix + '/' if self.target.prefix else ''}"

    def call(self, func, *args, **kwargs):
        from azure.core.exceptions import AzureError, ResourceExistsError, ResourceNotFoundError
        try:
            return func(*args, **kwargs)
        except ResourceExistsError:
            raise ObjectExists(str(args[0]) if args else '') from None
        except ResourceNotFoundError as e:
            raise ObjectNotFound(str(e).splitlines()[0]) from None
        except AzureError as e:
            if getattr(e, 'status_code', None) in (409, 412):
                raise ObjectExists(str(e).splitlines()[0]) from None
            raise StorageError(f"Azure: {str(e).splitlines()[0]}") from e

    def open_write(self, key, size_hint=None):
        return AzureWriter(self, key, size_hint)

    def open_read(self, key, version=None):
        blob = self.container.get_blob_client(self.full_key(key))
        try:
            downloader = self.call(blob.download_blob, version_id=version)
        except ObjectNotFound:
            raise ObjectNotFound(f"Object not found: {key}") from None
        return _Download(downloader)

    def stat(self, key):
        blob = self.container.get_blob_client(self.full_key(key))
        try:
            props = self.call(blob.get_blob_properties)
        except ObjectNotFound:
            return None
        policy = getattr(props, 'immutability_policy', None)
        return ObjectInfo(
            key=key, size=props.size, version=getattr(props, 'version_id', None),
            retain_until=getattr(policy, 'expiry_time', None) if policy else None, last_modified=props.last_modified,
        )

    def list(self, prefix, include_versions=False):
        full_prefix = self.full_key(prefix) if prefix else (self.target.prefix + '/' if self.target.prefix else '')
        include = ['versions', 'immutabilitypolicy'] if include_versions else ['immutabilitypolicy']
        try:
            blobs = list(self.container.list_blobs(name_starts_with=full_prefix, include=include))
        except Exception as e:
            raise StorageError(f"Azure: {str(e).splitlines()[0]}") from e
        for b in blobs:
            policy = getattr(b, 'immutability_policy', None)
            yield ObjectInfo(
                key=self.relative_key(b.name), size=b.size, version=getattr(b, 'version_id', None),
                retain_until=getattr(policy, 'expiry_time', None) if policy else None,
                last_modified=b.last_modified, is_latest=getattr(b, 'is_current_version', True) is not False,
            )

    def delete(self, keys):
        errors = []
        for item in keys:
            key, version = (item, None) if isinstance(item, str) else item
            targets = [version] if version else [
                v.version for v in self.list(key, include_versions=True) if v.key == key
            ] or [None]
            blob = self.container.get_blob_client(self.full_key(key))
            for v in targets:
                try:
                    self.call(blob.delete_blob, version_id=v) if v else self.call(blob.delete_blob)
                except ObjectNotFound:
                    continue
                except StorageError as e:
                    errors.append(DeleteError(key, v, str(e)))
        return errors

    def abort_incomplete(self, older_than):
        return 0  # The service garbage-collects uncommitted blocks after seven days

    def probe(self):
        report = ProbeReport()
        try:
            props = self.call(self.container.get_container_properties)
            report.check('container reachable', True, self.describe())
        except StorageError as e:
            report.check('container reachable', False, str(e))
            return report
        versioned = bool(getattr(props, 'immutable_storage_with_versioning_enabled', False))
        report.versioned = versioned
        key = f'.probe/{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}'
        self._probe_roundtrip(report, key)
        info = self.stat(key)
        if self.target.retention_days:
            # The default policy is visible only on a blob it was applied to
            blob = self.container.get_blob_client(self.full_key(key))
            policy = getattr(self.call(blob.get_blob_properties), 'immutability_policy', None) if info else None
            expiry = getattr(policy, 'expiry_time', None)
            mode = getattr(policy, 'policy_mode', None)
            days = round((expiry - datetime.now(UTC)) / timedelta(days=1)) if expiry else None
            report.default_retention_days = days
            report.retention_locked = str(mode).lower() == 'locked'
            report.check(
                'version-level immutability (locked)',
                versioned and report.retention_locked and (days or 0) >= self.target.retention_days,
                f"container default: {days or 'none'} day(s), policy {mode or 'none'}; "
                f"target expects {self.target.retention_days}",
            )
        else:
            report.check('default retention', False, 'no retention_days configured; sets are not immutable',
                         required=False)
        report.abort_incomplete_days = 7
        return report


class _Download:
    def __init__(self, downloader):
        self.chunks = downloader.chunks()
        self.buffer = b''

    def read(self, size=-1):
        if size is None or size < 0:
            data = self.buffer + b''.join(self.chunks)
            self.buffer = b''
            return data
        while len(self.buffer) < size:
            try:
                self.buffer += next(self.chunks)
            except StopIteration:
                break
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def readable(self):
        return True

    def close(self):
        self.chunks = iter(())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
