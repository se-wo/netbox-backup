import os
import time
import uuid
from datetime import UTC, datetime

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


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class LocalWriter(Writer):
    """
    Writes to "<key>.part" (exclusive create), then fsyncs the file, hard-links it to the final name (which fails if
    that name exists) and fsyncs the directory. A crash leaves only a .part file behind.
    """
    def __init__(self, key, path):
        super().__init__(key)
        self.path = path
        self.part = f'{path}.{uuid.uuid4().hex[:8]}{PART_SUFFIX}'
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        if os.path.exists(path):
            raise ObjectExists(f"Object exists: {key}")
        self.fd = os.open(self.part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    def write(self, data):
        view = memoryview(data)
        while view:
            written = os.write(self.fd, view)
            view = view[written:]
        self.size += len(data)

    def commit(self):
        try:
            os.fsync(self.fd)
        finally:
            os.close(self.fd)
            self.closed = True
        try:
            try:
                os.link(self.part, self.path)
            except FileExistsError:
                raise ObjectExists(f"Object exists: {self.key}") from None
            except OSError:
                # Filesystems without hard links (some SMB mounts): best-effort create-only rename
                if os.path.exists(self.path):
                    raise ObjectExists(f"Object exists: {self.key}") from None
                os.rename(self.part, self.path)
        finally:
            if os.path.exists(self.part):
                os.unlink(self.part)
        _fsync_dir(os.path.dirname(self.path))
        return ObjectInfo(key=self.key, size=self.size)

    def abort(self):
        if not self.closed:
            os.close(self.fd)
            self.closed = True
        if os.path.exists(self.part):
            os.unlink(self.part)


class LocalBackend(StorageBackend):
    """
    A directory on local disk, an NFS export or an SMB mount. No immutability of its own: use a WORM filer for that.
    """
    name = 'local'
    capabilities = Capabilities(
        streaming_write=True, create_only=True, default_retention=False, versioned=False, object_checksum=None,
    )

    def __init__(self, target, prune=False):
        super().__init__(target, prune=prune)
        path = self.options.get('path')
        if not path:
            raise StorageError(f"Target {target.name!r}: 'path' is required for the local backend")
        self.root = os.path.abspath(path)

    def describe(self):
        return os.path.join(self.root, self.target.prefix) + os.sep

    def _path(self, key):
        path = os.path.abspath(os.path.join(self.root, self.full_key(key)))
        if path != self.root and not path.startswith(self.root + os.sep):
            raise StorageError(f"Key escapes the target root: {key}")
        return path

    def open_write(self, key, size_hint=None):
        return LocalWriter(key, self._path(key))

    def open_read(self, key, version=None):
        try:
            return open(self._path(key), 'rb')
        except FileNotFoundError:
            raise ObjectNotFound(f"Object not found: {key}") from None

    def stat(self, key):
        try:
            st = os.stat(self._path(key))
        except FileNotFoundError:
            return None
        return ObjectInfo(key=key, size=st.st_size, last_modified=datetime.fromtimestamp(st.st_mtime, UTC))

    def list(self, prefix, include_versions=False):
        base = self._path(prefix) if prefix else self._path('')
        if os.path.isfile(base):
            info = self.stat(prefix)
            if info:
                yield info
            return
        for root, dirs, files in os.walk(base):
            dirs.sort()
            for name in sorted(files):
                if name.endswith(PART_SUFFIX):
                    continue
                path = os.path.join(root, name)
                key = self.relative_key(os.path.relpath(path, self.root).replace(os.sep, '/'))
                st = os.stat(path)
                yield ObjectInfo(key=key, size=st.st_size, last_modified=datetime.fromtimestamp(st.st_mtime, UTC))

    def delete(self, keys):
        if not (self.prune or self.options.get('allow_prune')):
            return [DeleteError(k if isinstance(k, str) else k[0], None, 'deletes need the prune runner')
                    for k in keys]
        errors = []
        for item in keys:
            key = item if isinstance(item, str) else item[0]
            path = self._path(key)
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError as e:
                errors.append(DeleteError(key, None, e.strerror))
                continue
            # Remove now-empty set directories, never the target root
            parent = os.path.dirname(path)
            while parent.startswith(self._path('') + os.sep):
                try:
                    os.rmdir(parent)
                except OSError:
                    break
                parent = os.path.dirname(parent)
        return errors

    def abort_incomplete(self, older_than):
        cutoff = time.time() - older_than.total_seconds()
        removed = 0
        for root, _, files in os.walk(self._path('')):
            for name in files:
                path = os.path.join(root, name)
                if name.endswith(PART_SUFFIX) and os.stat(path).st_mtime < cutoff:
                    os.unlink(path)
                    removed += 1
        return removed

    def probe(self, under=''):
        report = ProbeReport()
        try:
            os.makedirs(self._path(''), mode=0o700, exist_ok=True)
        except OSError as e:
            report.check('target directory', False, f"{self._path('')}: {e.strerror}")
            return report
        report.check('target directory', os.access(self._path(''), os.W_OK), self._path(''))
        key = self.probe_key(under)
        try:
            self._probe_roundtrip(report, key)
        finally:
            # Nothing is locked on a filesystem, so the probe object can go right away
            if os.path.exists(self._path(key)):
                os.unlink(self._path(key))
        report.check('default retention', False, 'filesystem targets have no immutability; use a WORM filer',
                     required=False)
        return report
