"""
The file components: media and scripts are read through Django's STORAGES aliases, so S3- or blob-backed media is
backed up correctly; config bundles the files that bring a restored database back to life.
"""
import importlib
import json
import os
import platform
import socket
from importlib import metadata

from django.apps import apps
from django.conf import settings
from django.db import models
from psycopg import sql

from .. import __version__
from ..core.pipeline import BytesSource, TarSource

STORAGE_ALIASES = {'media': 'default', 'scripts': 'scripts'}


def get_storage(component):
    from django.core.files.storage import storages
    return storages[STORAGE_ALIASES[component]]


def walk(storage, path=''):
    """
    Every file below path in a Django storage, as names relative to its root, sorted.
    """
    try:
        dirs, files = storage.listdir(path)
    except FileNotFoundError:
        return
    for name in sorted(files):
        yield f'{path}/{name}' if path else name
    for name in sorted(dirs):
        if name.startswith('.nbb-quarantine'):
            continue
        yield from walk(storage, f'{path}/{name}' if path else name)


def referenced_files(cursor):
    """
    File paths the database references, per component, read inside the dump's snapshot: every concrete FileField
    on the default storage (image attachments and the like) for media, and the ManagedFile rows of the scripts
    root for scripts.
    """
    from extras.storage import ScriptFileSystemStorage
    refs = {'media': set(), 'scripts': set()}
    for model in apps.get_models():
        if model._meta.proxy or not model._meta.managed:
            continue
        for field in model._meta.concrete_fields:
            if not isinstance(field, models.FileField) or isinstance(field.storage, ScriptFileSystemStorage):
                continue
            cursor.execute(sql.SQL("SELECT {col} FROM {table} WHERE {col} <> ''").format(
                col=sql.Identifier(field.column), table=sql.Identifier(model._meta.db_table),
            ))
            refs['media'].update(row[0] for row in cursor.fetchall() if row[0])
    cursor.execute("SELECT to_regclass('core_managedfile') IS NOT NULL")
    if cursor.fetchone()[0]:
        cursor.execute("SELECT file_path FROM core_managedfile WHERE file_root IN ('scripts', 'reports')")
        refs['scripts'].update(row[0] for row in cursor.fetchall())
    return refs


class StorageTree:
    """
    A file component as a TarSource plus the check against the files the database references. A referenced file
    that is missing is looked up once more, then reported in media_missing; the set gets a warning.
    """
    def __init__(self, component, referenced, level=3):
        self.component = component
        self.storage = get_storage(component)
        self.referenced = set(referenced)
        self.names = list(walk(self.storage))
        self.missing = []
        present = set(self.names)
        for name in sorted(self.referenced - present):
            # Retry once: a file may have been written between the snapshot and the listing
            if self.storage.exists(name):
                self.names.append(name)
            else:
                self.missing.append(name)
        entries = ((name, self.storage.size(name), lambda n=name: self.storage.open(n, 'rb')) for name in self.names)
        self.source = TarSource(entries, level=level)

    def summary(self):
        return {
            'files': self.source.files,
            'bytes': self.source.bytes,
            'referenced': len(self.referenced),
            'missing': len(self.missing),
        }


def pip_freeze():
    return sorted(f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions() if d.metadata['Name'])


def installed_plugins():
    result = {}
    for name in getattr(settings, 'PLUGINS', []):
        try:
            app = apps.get_app_config(name.rsplit('.', 1)[-1])
            result[name] = getattr(app, 'version', '') or ''
        except LookupError:
            result[name] = ''
    return result


def configuration_files():
    """
    configuration.py (NETBOX_CONFIGURATION), ldap_config.py beside it, and extra_configuration_files.
    """
    from .. import conf
    module = importlib.import_module(os.getenv('NETBOX_CONFIGURATION', 'netbox.configuration'))
    paths = []
    if getattr(module, '__file__', None):
        paths.append(module.__file__)
        ldap = os.path.join(os.path.dirname(module.__file__), 'ldap_config.py')
        if os.path.isfile(ldap):
            paths.append(ldap)
    paths += list(conf.raw().get('extra_configuration_files') or [])
    return paths


def config_source():
    """
    The config component: configuration files verbatim, secrets included (the object is encrypted to the ops and
    offline recovery keys, and a set already holds every password hash and token), plus the plugin list and pip
    freeze. Returns (source, summary).
    """
    files = {}
    missing = []
    for path in configuration_files():
        try:
            with open(path, encoding='utf-8') as f:
                files[path] = f.read()
        except OSError:
            missing.append(path)
    document = {
        'netbox_version': settings.VERSION,
        'plugin_version': __version__,
        'hostname': socket.gethostname(),
        'python': platform.python_version(),
        'plugins': installed_plugins(),
        'pip_freeze': pip_freeze(),
        'files': files,
        'note': 'Contains secrets (SECRET_KEY, API_TOKEN_PEPPERS, database password). Handle like production.',
    }
    data = json.dumps(document, indent=1, sort_keys=True).encode()
    summary = {'files': len(files), 'bytes': len(data), 'missing': missing}
    return BytesSource(data), summary
