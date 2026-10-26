"""
rclone, for the long tail: FTPS, WebDAV, B2, Swift and 70+ more providers through one subprocess.

The rclone binary is pinned by its SHA-256 (binary_sha256): a replaced binary fails every call. Its configuration
comes from a referenced rclone.conf, ideally encrypted (rclone config encryption) with the password in
credentials.config_password. Uploads always spool to a private local file first, because rclone can only stream to
some remotes, then go up with "copyto --immutable" after a check that the key does not exist yet, so writes are
create-only as far as the remote allows. Immutability is the provider's; level-1 checks are size only.
"""
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile

from ..core.config import is_reference, resolve_secret
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

# rclone exit codes for a missing directory or file
NOT_FOUND = (3, 4)
TIMEOUT = 6 * 3600
_verified = {}


def _parse_time(value):
    from datetime import datetime
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


class RcloneWriter(Writer):
    """
    Spools to a 0600 file, then uploads it on commit.
    """
    def __init__(self, backend, key):
        super().__init__(key)
        self.backend = backend
        fd, self.spool = tempfile.mkstemp(prefix='nbb-rclone-', dir=backend.spool_dir)
        self.file = os.fdopen(fd, 'wb')

    def write(self, data):
        self.file.write(data)
        self.size += len(data)

    def commit(self):
        try:
            self.file.close()
            if self.backend.stat(self.key) is not None:
                raise ObjectExists(f"Object exists: {self.key}")
            self.backend.rclone('copyto', '--immutable', self.spool, self.backend.remote_path(self.key))
            info = self.backend.stat(self.key)
            if info is None or info.size != self.size:
                raise StorageError(f"{self.key}: uploaded {self.size} bytes, the remote reports "
                                   f"{info.size if info else 'nothing'}")
        finally:
            self.closed = True
            with contextlib.suppress(OSError):
                os.unlink(self.spool)
        return ObjectInfo(key=self.key, size=self.size)

    def abort(self):
        self.closed = True
        with contextlib.suppress(OSError):
            self.file.close()
        with contextlib.suppress(OSError):
            os.unlink(self.spool)


class _Stream:
    def __init__(self, proc, key):
        self.proc = proc
        self.key = key

    def read(self, size=-1):
        return self.proc.stdout.read(-1 if size is None or size < 0 else size)

    def readable(self):
        return True

    def close(self):
        if self.proc.poll() is None:
            # Closed early (or fully read): don't leave rclone running
            self.proc.stdout.close()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=30)
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait()
        else:
            self.proc.stdout.close()
        self.proc.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class RcloneBackend(StorageBackend):
    name = 'rclone'
    capabilities = Capabilities(
        streaming_write=False, create_only=True, default_retention=False, versioned=False, object_checksum=None,
    )

    def __init__(self, target, prune=False):
        super().__init__(target, prune=prune)
        self.remote = self.options.get('remote')
        if not self.remote or ':' not in self.remote:
            raise StorageError(f"Target {target.name!r}: 'remote' is required for the rclone backend, e.g. "
                               f"'b2:bucket/netbox'")
        self.expected_sha256 = str(self.options.get('binary_sha256') or '').lower()
        if not self.expected_sha256:
            raise StorageError(f"Target {target.name!r}: 'binary_sha256' is required: pin the rclone binary "
                               f"(sha256sum $(which rclone))")
        self.binary = shutil.which(self.options.get('binary', 'rclone'))
        if not self.binary:
            raise StorageError(f"rclone binary {self.options.get('binary', 'rclone')!r} not found")
        self.creds = target.prune_credentials if prune and target.prune_credentials else target.credentials
        self.spool_dir = self.options.get('spool_dir')
        if self.spool_dir:
            os.makedirs(self.spool_dir, mode=0o700, exist_ok=True)

    def describe(self):
        return f"rclone {self.remote_path('')}"

    def remote_path(self, key):
        full = self.full_key(key) if key else self.target.prefix
        if not full:
            return self.remote
        return f"{self.remote}{'' if self.remote.endswith((':', '/')) else '/'}{full}"

    def _check_binary(self):
        stat = os.stat(self.binary)
        cache_key = (self.binary, stat.st_mtime_ns, stat.st_size)
        if cache_key not in _verified:
            digest = hashlib.sha256()
            with open(self.binary, 'rb') as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b''):
                    digest.update(chunk)
            _verified[cache_key] = digest.hexdigest()
        if _verified[cache_key] != self.expected_sha256:
            raise StorageError(f"{self.binary} has SHA-256 {_verified[cache_key]}, not the pinned "
                               f"{self.expected_sha256}: refusing to run it")

    @contextlib.contextmanager
    def _environment(self):
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': tempfile.gettempdir()}
        directory = None
        try:
            config = self.creds.get('config')
            if config:
                if is_reference(config) and config.startswith('file:'):
                    env['RCLONE_CONFIG'] = config[len('file:'):]
                else:
                    directory = tempfile.mkdtemp(prefix='nbb-rclone-',
                                                 dir='/dev/shm' if os.path.isdir('/dev/shm') else None)
                    path = os.path.join(directory, 'rclone.conf')
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'w') as f:
                        f.write(resolve_secret(config))
                    env['RCLONE_CONFIG'] = path
            if self.creds.get('config_password'):
                env['RCLONE_CONFIG_PASS'] = resolve_secret(self.creds['config_password'])
            yield env
        finally:
            if directory:
                shutil.rmtree(directory, ignore_errors=True)

    def _args(self, *args):
        return [self.binary, '--ask-password=false', '--retries', '3', *self.options.get('flags', ()), *args]

    def rclone(self, *args, ok=(0,), timeout=TIMEOUT):
        self._check_binary()
        with self._environment() as env:
            try:
                result = subprocess.run(self._args(*args), capture_output=True, env=env, timeout=timeout)
            except subprocess.TimeoutExpired as e:
                raise StorageError(f"rclone {args[0]} timed out") from e
        if result.returncode not in ok:
            message = result.stderr.decode(errors='replace').strip().splitlines()
            raise StorageError(f"rclone {args[0]} exited with {result.returncode}: "
                               f"{message[-1] if message else ''}")
        return result

    # Interface

    def open_write(self, key, size_hint=None):
        self.full_key(key)  # validates the key before anything is spooled
        return RcloneWriter(self, key)

    def open_read(self, key, version=None):
        if self.stat(key) is None:
            raise ObjectNotFound(f"Object not found: {key}")
        self._check_binary()
        with self._environment() as env:
            # The config file only needs to exist while rclone starts and reads it
            proc = subprocess.Popen(self._args('cat', self.remote_path(key)), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env)
        return _Stream(proc, key)

    def stat(self, key):
        result = self.rclone('lsjson', '--stat', '--no-mimetype', self.remote_path(key), ok=(0, *NOT_FOUND))
        if result.returncode in NOT_FOUND:
            return None
        entry = json.loads(result.stdout or 'null')
        if not entry or entry.get('IsDir'):
            return None
        return ObjectInfo(key=key, size=entry['Size'], last_modified=_parse_time(entry.get('ModTime')))

    def list(self, prefix, include_versions=False):
        # Prefix semantics as on S3: list the directory the prefix ends in, keep the keys that start with it
        directory = prefix.rsplit('/', 1)[0] if '/' in prefix else ''
        result = self.rclone('lsjson', '-R', '--files-only', '--no-mimetype', self.remote_path(directory),
                             ok=(0, *NOT_FOUND))
        if result.returncode in NOT_FOUND:
            return
        for entry in sorted(json.loads(result.stdout or '[]'), key=lambda e: e['Path']):
            key = f"{directory}/{entry['Path']}" if directory else entry['Path']
            if key.startswith(prefix):
                yield ObjectInfo(key=key, size=entry['Size'], last_modified=_parse_time(entry.get('ModTime')))

    def delete(self, keys):
        if not (self.prune or self.options.get('allow_prune')):
            return [DeleteError(k if isinstance(k, str) else k[0], None, 'deletes need the prune runner')
                    for k in keys]
        errors = []
        for item in keys:
            key = item if isinstance(item, str) else item[0]
            try:
                self.rclone('deletefile', self.remote_path(key), ok=(0, *NOT_FOUND))
            except StorageError as e:
                errors.append(DeleteError(key, None, str(e)))
        return errors

    def abort_incomplete(self, older_than):
        return 0  # Uploads spool locally and go up in one rclone call; nothing is left half-written remotely

    def probe(self, under=''):
        report = ProbeReport()
        try:
            self._check_binary()
            version = self.rclone('version').stdout.decode(errors='replace').splitlines()[0]
            report.check('rclone binary pinned', True, f'{self.binary}: {version}')
        except StorageError as e:
            report.check('rclone binary pinned', False, str(e))
            return report
        key = self.probe_key(under)
        try:
            self._probe_roundtrip(report, key)
        finally:
            with contextlib.suppress(StorageError):
                self.rclone('deletefile', self.remote_path(key), ok=(0, *NOT_FOUND))
        report.check('default retention', False, 'immutability depends on the provider behind rclone; it is not '
                                                 'verified', required=False)
        return report
