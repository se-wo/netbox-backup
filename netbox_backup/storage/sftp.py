"""
SFTP, for air-gapped installs and NAS targets.

The server's host key must be pinned (known_host): an unknown or changed key fails the connection, there is no
trust on first use. Writes go to "<key>.<random>.part" created exclusively, are fsynced where the server supports
it (fsync@openssh.com), and are renamed to the final name with SFTP's plain rename, which refuses an existing target
(OpenSSH links, then unlinks), so writes are create-only. SFTP has no immutability: use a WORM filer, or treat the
target as a convenience copy. Level-1 checks are size only.

asyncssh is asynchronous; the driver runs it on one background event loop per process and keeps one connection per
server and user, so a long job (or a journal written step by step) reuses it.
"""
import asyncio
import contextlib
import posixpath
import threading
import time
import uuid
from datetime import UTC, datetime

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

PART_SUFFIX = '.part'
TIMEOUT = 300

_loop = None
_loop_lock = threading.Lock()
_connections = {}


def _run(coro, timeout=TIMEOUT):
    global _loop
    with _loop_lock:
        if _loop is None or not _loop.is_running():
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True, name='nbb-sftp').start()
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)


def _known_hosts(entries):
    """
    asyncssh's (trusted host keys, trusted CA keys, revoked keys) from known_hosts-style key lines; a line starting
    with @cert-authority pins a CA that signs host certificates.
    """
    import asyncssh
    keys, cas = [], []
    for line in [entries] if isinstance(entries, str) else entries:
        line = line.strip()
        if line.startswith('@cert-authority'):
            cas.append(asyncssh.import_public_key(line.split(None, 1)[1]))
        elif line:
            keys.append(asyncssh.import_public_key(line))
    return keys, cas, []


class SFTPWriter(Writer):
    def __init__(self, backend, key):
        super().__init__(key)
        self.backend = backend
        self.path = backend.path(key)
        self.part = f'{self.path}.{uuid.uuid4().hex[:8]}{PART_SUFFIX}'
        self.file = backend.run(self._open())

    async def _open(self):
        import asyncssh
        sftp = await self.backend.sftp()
        await sftp.makedirs(posixpath.dirname(self.path), exist_ok=True)
        if await sftp.exists(self.path):
            raise ObjectExists(f"Object exists: {self.key}")
        return await sftp.open(self.part, asyncssh.FXF_WRITE | asyncssh.FXF_CREAT | asyncssh.FXF_EXCL,
                               asyncssh.SFTPAttrs(permissions=0o600), encoding=None)

    def write(self, data):
        self.backend.run(self.file.write(bytes(data)))
        self.size += len(data)

    async def _commit(self):
        import asyncssh
        sftp = await self.backend.sftp()
        try:
            with contextlib.suppress(asyncssh.SFTPOpUnsupported):
                await self.file.fsync()
        finally:
            await self.file.close()
        try:
            await sftp.rename(self.part, self.path)
        except asyncssh.SFTPError:
            exists = await sftp.exists(self.path)
            with contextlib.suppress(asyncssh.SFTPError):
                await sftp.remove(self.part)
            if exists:
                raise ObjectExists(f"Object exists: {self.key}") from None
            raise

    def commit(self):
        try:
            self.backend.run(self._commit())
        except BaseException:
            self.closed = True
            raise
        self.closed = True
        return ObjectInfo(key=self.key, size=self.size)

    async def _abort(self):
        import asyncssh
        with contextlib.suppress(asyncssh.Error, OSError):
            await self.file.close()
        with contextlib.suppress(asyncssh.Error, OSError):
            await (await self.backend.sftp()).remove(self.part)

    def abort(self):
        self.closed = True
        with contextlib.suppress(Exception):
            self.backend.run(self._abort(), timeout=60)


class _Reader:
    def __init__(self, backend, file):
        self.backend = backend
        self.file = file

    def read(self, size=-1):
        return self.backend.run(self.file.read(-1 if size is None or size < 0 else size))

    def readable(self):
        return True

    def close(self):
        with contextlib.suppress(Exception):
            self.backend.run(self.file.close(), timeout=60)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class SFTPBackend(StorageBackend):
    name = 'sftp'
    capabilities = Capabilities(
        streaming_write=True, create_only=True, default_retention=False, versioned=False, object_checksum=None,
    )

    def __init__(self, target, prune=False):
        super().__init__(target, prune=prune)
        try:
            import asyncssh  # noqa: F401
        except ImportError as e:
            raise StorageError("The sftp backend requires asyncssh: pip install 'netbox-backup[sftp]'") from e
        self.host = self.options.get('host')
        self.port = int(self.options.get('port', 22))
        self.root = posixpath.normpath(self.options.get('path') or '.')
        if not self.host:
            raise StorageError(f"Target {target.name!r}: 'host' is required for the sftp backend")
        if not self.options.get('known_host'):
            raise StorageError(f"Target {target.name!r}: 'known_host' is required: pin the server's host key "
                               f"(e.g. the output of ssh-keyscan -t ed25519 {self.host}, without the host name)")
        self.creds = target.prune_credentials if prune and target.prune_credentials else target.credentials
        if not self.creds.get('username'):
            raise StorageError(f"Target {target.name!r}: credentials.username is required for the sftp backend")

    def describe(self):
        return f"sftp://{self.host}:{self.port}{posixpath.join(self.root, self.target.prefix)}/"

    # Connection

    def run(self, coro, timeout=TIMEOUT):
        import asyncssh
        try:
            return _run(coro, timeout)
        except (ObjectExists, ObjectNotFound, StorageError):
            raise
        except asyncssh.SFTPNoSuchFile as e:
            raise ObjectNotFound(str(e)) from None
        except asyncssh.HostKeyNotVerifiable as e:
            raise StorageError(f"SFTP {self.host}: host key does not match the pinned known_host ({e})") from e
        except (asyncssh.Error, OSError, TimeoutError) as e:
            _connections.pop(self._identity(), None)
            raise StorageError(f"SFTP {self.host}: {e}") from e

    def _identity(self):
        known = self.options['known_host']
        return (self.host, self.port, self.creds.get('username'), self.creds.get('key'), self.creds.get('password'),
                known if isinstance(known, str) else tuple(known))

    async def sftp(self):
        import asyncssh
        identity = self._identity()
        cached = _connections.get(identity)
        if cached is not None and not cached[0].is_closed():
            return cached[1]
        kwargs = {'username': resolve_secret(self.creds['username']),
                  'known_hosts': _known_hosts(self.options['known_host']), 'agent_path': None}
        if self.creds.get('key'):
            passphrase = resolve_secret(self.creds['passphrase']) if self.creds.get('passphrase') else None
            key = asyncssh.import_private_key(resolve_secret(self.creds['key']), passphrase)
            cert = asyncssh.import_certificate(resolve_secret(self.creds['certificate'])) \
                if self.creds.get('certificate') else None
            kwargs['client_keys'] = [(key, cert) if cert else key]
        else:
            kwargs['client_keys'] = None
        if self.creds.get('password'):
            kwargs['password'] = resolve_secret(self.creds['password'])
        conn = await asyncssh.connect(self.host, self.port, connect_timeout=30, keepalive_interval=30,
                                      **kwargs)
        client = await conn.start_sftp_client()
        _connections[identity] = (conn, client)
        return client

    def path(self, key):
        return posixpath.join(self.root, self.full_key(key))

    # Interface

    def open_write(self, key, size_hint=None):
        return SFTPWriter(self, key)

    def open_read(self, key, version=None):
        async def _open():
            return await (await self.sftp()).open(self.path(key), 'rb', encoding=None)
        try:
            return _Reader(self, self.run(_open()))
        except ObjectNotFound:
            raise ObjectNotFound(f"Object not found: {key}") from None

    def stat(self, key):
        async def _stat():
            return await (await self.sftp()).stat(self.path(key))
        try:
            attrs = self.run(_stat())
        except ObjectNotFound:
            return None
        return ObjectInfo(key=key, size=attrs.size, last_modified=datetime.fromtimestamp(attrs.mtime, UTC)
                          if attrs.mtime else None)

    async def _walk(self, path):
        import asyncssh
        sftp = await self.sftp()
        try:
            entries = [e async for e in sftp.scandir(path)]
        except asyncssh.SFTPNoSuchFile:
            return []
        files = []
        for entry in sorted(entries, key=lambda e: e.filename):
            if entry.filename in ('.', '..'):
                continue
            full = posixpath.join(path, entry.filename)
            if entry.attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY:
                files += await self._walk(full)
            elif entry.attrs.type == asyncssh.FILEXFER_TYPE_REGULAR and not entry.filename.endswith(PART_SUFFIX):
                files.append((full, entry.attrs))
        return files

    def _list_files(self, prefix=''):
        base = self.path(prefix) if prefix else self.path('')

        async def _list():
            sftp = await self.sftp()
            if prefix and await sftp.isfile(base):
                return [(base, await sftp.stat(base))]
            return await self._walk(base)
        return self.run(_list())

    def list(self, prefix, include_versions=False):
        for path, attrs in self._list_files(prefix):
            key = self.relative_key(posixpath.relpath(path, self.root))
            yield ObjectInfo(key=key, size=attrs.size,
                             last_modified=datetime.fromtimestamp(attrs.mtime, UTC) if attrs.mtime else None)

    def delete(self, keys):
        if not (self.prune or self.options.get('allow_prune')):
            return [DeleteError(k if isinstance(k, str) else k[0], None, 'deletes need the prune runner')
                    for k in keys]

        async def _delete(path):
            import asyncssh
            sftp = await self.sftp()
            try:
                await sftp.remove(path)
            except asyncssh.SFTPNoSuchFile:
                return
            # Remove now-empty set directories, never the target root
            parent = posixpath.dirname(path)
            top = self.path('')
            while parent.startswith(top + '/'):
                try:
                    await sftp.rmdir(parent)
                except asyncssh.SFTPError:
                    break
                parent = posixpath.dirname(parent)

        errors = []
        for item in keys:
            key = item if isinstance(item, str) else item[0]
            try:
                self.run(_delete(self.path(key)))
            except StorageError as e:
                errors.append(DeleteError(key, None, str(e)))
        return errors

    def abort_incomplete(self, older_than):
        cutoff = time.time() - older_than.total_seconds()

        async def _abort():
            import asyncssh
            sftp = await self.sftp()
            removed = 0
            for path, attrs in await self._walk_parts(self.path('')):
                if (attrs.mtime or 0) < cutoff:
                    with contextlib.suppress(asyncssh.SFTPError):
                        await sftp.remove(path)
                        removed += 1
            return removed
        return self.run(_abort())

    async def _walk_parts(self, path):
        import asyncssh
        sftp = await self.sftp()
        try:
            entries = [e async for e in sftp.scandir(path)]
        except asyncssh.SFTPNoSuchFile:
            return []
        parts = []
        for entry in entries:
            if entry.filename in ('.', '..'):
                continue
            full = posixpath.join(path, entry.filename)
            if entry.attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY:
                parts += await self._walk_parts(full)
            elif entry.filename.endswith(PART_SUFFIX):
                parts.append((full, entry.attrs))
        return parts

    def probe(self, under=''):
        report = ProbeReport()

        async def _prepare():
            sftp = await self.sftp()
            await sftp.makedirs(self.path(''), exist_ok=True)
        try:
            self.run(_prepare())
            report.check('server reachable, host key pinned', True, self.describe())
        except StorageError as e:
            report.check('server reachable, host key pinned', False, str(e))
            return report
        key = self.probe_key(under)
        try:
            self._probe_roundtrip(report, key)
        finally:
            # Nothing is locked on SFTP, so the probe object can go right away
            async def _cleanup():
                import asyncssh
                with contextlib.suppress(asyncssh.SFTPError):
                    await (await self.sftp()).remove(self.path(key))
            with contextlib.suppress(StorageError):
                self.run(_cleanup())
        report.check('default retention', False, 'SFTP has no immutability; use a WORM filer', required=False)
        report.abort_incomplete_days = 1
        return report
