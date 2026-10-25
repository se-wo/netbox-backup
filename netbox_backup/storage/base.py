import posixpath


class StorageError(Exception):
    pass


class Storage:
    """
    Minimal object-store abstraction. Keys are '/'-separated paths relative to the storage root.
    """
    name = None

    def __init__(self, **config):
        self.config = config

    @staticmethod
    def join(*parts):
        return posixpath.join(*[p.strip('/') for p in parts if p])

    def describe(self):
        """
        Human-readable, credential-free description of this storage target.
        """
        raise NotImplementedError

    def put_file(self, key, local_path):
        raise NotImplementedError

    def get_file(self, key, local_path):
        raise NotImplementedError

    def put_bytes(self, key, data):
        raise NotImplementedError

    def get_bytes(self, key):
        raise NotImplementedError

    def exists(self, key):
        raise NotImplementedError

    def size(self, key):
        raise NotImplementedError

    def list_children(self, prefix=''):
        """
        Return the names of the immediate "directories" beneath prefix.
        """
        raise NotImplementedError

    def delete_prefix(self, prefix):
        """
        Delete every object beneath prefix.
        """
        raise NotImplementedError

    def open_read(self, key):
        """
        Return a binary file-like object for streaming the object at key.
        """
        raise NotImplementedError

    def check(self):
        """
        Round-trip a probe object to prove the target is reachable and writable. Raises StorageError on failure.
        """
        key = '.netbox-backup-probe'
        payload = b'netbox-backup storage probe'
        try:
            self.put_bytes(key, payload)
            if self.get_bytes(key) != payload:
                raise StorageError("Probe object read back with different content")
            self.delete_prefix(key)
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(f"{type(e).__name__}: {e}") from e
