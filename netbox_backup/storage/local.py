import os
import shutil
import tempfile

from .base import Storage, StorageError


class LocalStorage(Storage):
    """
    Stores backups in a directory on the local filesystem (or a mounted NFS/SMB share).
    """
    name = 'local'

    def __init__(self, path, **config):
        super().__init__(path=path, **config)
        self.root = os.path.abspath(path)

    def describe(self):
        return {'backend': 'local', 'path': self.root}

    def _path(self, key):
        path = os.path.abspath(os.path.join(self.root, key))
        if path != self.root and not path.startswith(self.root + os.sep):
            raise StorageError(f"Key escapes storage root: {key}")
        return path

    def _ensure_parent(self, path):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)

    def put_file(self, key, local_path):
        dest = self._path(key)
        self._ensure_parent(dest)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix='.upload-')
        try:
            with os.fdopen(fd, 'wb') as out, open(local_path, 'rb') as src:
                shutil.copyfileobj(src, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, dest)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def get_file(self, key, local_path):
        src = self._path(key)
        if not os.path.isfile(src):
            raise StorageError(f"Object not found: {key}")
        shutil.copyfile(src, local_path)

    def put_bytes(self, key, data):
        dest = self._path(key)
        self._ensure_parent(dest)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix='.upload-')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, dest)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def get_bytes(self, key):
        path = self._path(key)
        if not os.path.isfile(path):
            raise StorageError(f"Object not found: {key}")
        with open(path, 'rb') as f:
            return f.read()

    def exists(self, key):
        return os.path.isfile(self._path(key))

    def size(self, key):
        return os.path.getsize(self._path(key))

    def list_children(self, prefix=''):
        path = self._path(prefix) if prefix else self.root
        if not os.path.isdir(path):
            return []
        return sorted(e.name for e in os.scandir(path) if e.is_dir() and not e.name.startswith('.'))

    def delete_prefix(self, prefix):
        path = self._path(prefix)
        if path == self.root:
            raise StorageError("Refusing to delete the storage root")
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.unlink(path)

    def open_read(self, key):
        path = self._path(key)
        if not os.path.isfile(path):
            raise StorageError(f"Object not found: {key}")
        return open(path, 'rb')

    def check(self):
        try:
            os.makedirs(self.root, mode=0o700, exist_ok=True)
        except OSError as e:
            raise StorageError(f"Cannot create {self.root}: {e}") from e
        super().check()
