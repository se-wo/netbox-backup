"""
Archiving and restoring the file trees NetBox keeps outside the database (media, scripts, reports) and the
configuration files needed to bring a restored database back to life.
"""
import importlib
import os
import shutil
import tarfile
import tempfile

from django.conf import settings

from .. import conf


def _filesystem_location(alias, fallback):
    """
    Local directory behind a Django storage alias, or (None, warning) if it is not filesystem-backed.
    """
    from django.core.files.storage import FileSystemStorage, InvalidStorageError, storages
    try:
        storage = storages[alias]
    except InvalidStorageError:
        return fallback, None
    if isinstance(storage, FileSystemStorage):
        return storage.location, None
    return None, (
        f"storage '{alias}' uses {type(storage).__module__}.{type(storage).__name__}; back it up with that "
        f"service's own tooling"
    )


def file_trees():
    """
    Map of tree name -> (path, warning). A warning is set when a tree cannot be backed up from the filesystem.
    """
    trees = {}
    for name in conf.get('include_files') or []:
        if name == 'media':
            path, warning = _filesystem_location('default', settings.MEDIA_ROOT)
        elif name == 'scripts':
            path, warning = _filesystem_location('scripts', getattr(settings, 'SCRIPTS_ROOT', None))
        elif name == 'reports':
            path, warning = getattr(settings, 'REPORTS_ROOT', None), None
        else:
            path, warning = None, f"Unknown file tree '{name}' in include_files"
        if path is None and warning is None:
            warning = f"No path configured for '{name}'"
        trees[name] = (path, warning)
    return trees


def _excluded_paths():
    excluded = []
    storage = conf.storage_config()
    if storage.get('backend') == 'local' and storage.get('path'):
        excluded.append(os.path.realpath(storage['path']))
    if conf.work_dir():
        excluded.append(os.path.realpath(conf.work_dir()))
    return excluded


def archive_tree(source, dest):
    """
    Write source (a directory) to dest as a gzip'd tarball with paths relative to source. Returns (files, bytes).
    """
    excluded = _excluded_paths()
    files = total = 0
    source = os.path.realpath(source)
    with tarfile.open(dest, 'w:gz', compresslevel=int(conf.get('compression_level'))) as tar:
        for root, dirs, filenames in os.walk(source):
            dirs[:] = sorted(d for d in dirs if os.path.realpath(os.path.join(root, d)) not in excluded)
            rel_root = os.path.relpath(root, source)
            if rel_root != '.':
                tar.add(root, arcname=rel_root, recursive=False)
            for filename in sorted(filenames):
                path = os.path.join(root, filename)
                if os.path.islink(path) or os.path.isfile(path):
                    arcname = os.path.normpath(os.path.join(rel_root, filename))
                    tar.add(path, arcname=arcname, recursive=False)
                    if not os.path.islink(path):
                        files += 1
                        total += os.path.getsize(path)
    return files, total


def list_archive(path):
    with tarfile.open(path, 'r:gz') as tar:
        return tar.getnames()


def restore_tree(archive, target):
    """
    Replace the contents of target with the archive. The archive is fully extracted next to target first, so a
    corrupt archive never leaves a half-emptied directory behind. target itself is kept (it may be a mount point).
    """
    target = os.path.realpath(target)
    os.makedirs(target, exist_ok=True)
    staging = tempfile.mkdtemp(prefix='.netbox-backup-restore-', dir=os.path.dirname(target))
    try:
        with tarfile.open(archive, 'r:gz') as tar:
            tar.extractall(staging, filter='data')
        for entry in os.listdir(target):
            path = os.path.join(target, entry)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
        for entry in os.listdir(staging):
            shutil.move(os.path.join(staging, entry), os.path.join(target, entry))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def configuration_files():
    """
    Files that make up the running NetBox configuration: the configuration module (NETBOX_CONFIGURATION),
    ldap_config.py beside it if present, and any extra files configured by the operator.
    """
    module = importlib.import_module(os.getenv('NETBOX_CONFIGURATION', 'netbox.configuration'))
    paths = []
    if getattr(module, '__file__', None):
        paths.append(module.__file__)
        ldap = os.path.join(os.path.dirname(module.__file__), 'ldap_config.py')
        if os.path.isfile(ldap):
            paths.append(ldap)
    for path in conf.get('extra_configuration_files') or []:
        paths.append(path)
    return paths


def archive_configuration(dest):
    """
    Bundle configuration files into dest. Members are stored by basename under "config/". Returns the list of
    files included and a list of warnings for files that could not be read.
    """
    included, warnings = [], []
    with tarfile.open(dest, 'w:gz') as tar:
        for path in configuration_files():
            if os.path.isfile(path):
                tar.add(path, arcname=os.path.join('config', os.path.basename(path)), recursive=False)
                included.append(path)
            else:
                warnings.append(f"Configuration file not found: {path}")
    return included, warnings
