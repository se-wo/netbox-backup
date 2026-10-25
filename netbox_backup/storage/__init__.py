from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from .base import Storage, StorageError

__all__ = ('Storage', 'StorageError', 'get_storage')

BACKENDS = {
    'local': 'netbox_backup.storage.local.LocalStorage',
    's3': 'netbox_backup.storage.s3.S3Storage',
}


def get_storage(config=None):
    """
    Instantiate the configured storage backend. "backend" may be a built-in name or a dotted path to a
    Storage subclass.
    """
    from .. import conf
    config = dict(config or conf.storage_config())
    backend = config.pop('backend', 'local')
    try:
        cls = import_string(BACKENDS.get(backend, backend))
    except ImportError as e:
        raise ImproperlyConfigured(f"netbox_backup: unknown storage backend {backend!r}") from e
    try:
        return cls(**config)
    except TypeError as e:
        raise ImproperlyConfigured(f"netbox_backup: invalid options for storage backend {backend!r}: {e}") from e
