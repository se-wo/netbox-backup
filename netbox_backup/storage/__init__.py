"""
Storage drivers. Third-party packages add backends through the "netbox_backup.backends" entry point group, whose
entries name a StorageBackend subclass.
"""
from importlib.metadata import entry_points

from .base import Capabilities, ObjectExists, ObjectInfo, ObjectNotFound, ProbeReport, StorageBackend, StorageError

__all__ = (
    'BUILTIN_BACKENDS', 'Capabilities', 'ObjectExists', 'ObjectInfo', 'ObjectNotFound', 'ProbeReport',
    'StorageBackend', 'StorageError', 'backend_class', 'get_backend',
)

BUILTIN_BACKENDS = {
    'local': 'netbox_backup.storage.local:LocalBackend',
    's3': 'netbox_backup.storage.s3:S3Backend',
    'azure_blob': 'netbox_backup.storage.azure_blob:AzureBlobBackend',
    'gcs': 'netbox_backup.storage.gcs:GCSBackend',
    'sftp': 'netbox_backup.storage.sftp:SFTPBackend',
    'rclone': 'netbox_backup.storage.rclone:RcloneBackend',
}


def backend_class(name):
    import importlib
    path = BUILTIN_BACKENDS.get(name)
    if path is None:
        matches = [ep for ep in entry_points(group='netbox_backup.backends') if ep.name == name]
        if not matches:
            raise StorageError(f"Unknown storage backend {name!r}")
        return matches[0].load()
    module, _, attr = path.partition(':')
    return getattr(importlib.import_module(module), attr)


def get_backend(target, prune=False):
    """
    Instantiate the driver for a TargetConfig. prune=True selects the target's prune credentials.
    """
    return backend_class(target.backend)(target, prune=prune)
