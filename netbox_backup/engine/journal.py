"""
The restore journal. Written to a local file and to storage after every step, so "nbbackup restore status" shows
where a run stopped, even from a fresh container. Storage writes are create-only, so each step is its own object:

    <instance_id>/journal/<restore_id>/<NNN>-<step>.json
    <instance_id>/journal/<restore_id>/identities.json.enc   (users and tokens before the restore, encrypted)

The newest step object is the journal's current state.
"""
import json
import os
import re

from django.utils import timezone

from .. import conf
from ..core import layout
from ..storage import ObjectExists, ObjectNotFound, StorageError, get_backend


def _prefix(instance_id, restore_id):
    return f'{instance_id}/journal/{restore_id}/'


class Journal:

    def __init__(self, data, target_name):
        self.data = data
        self.target_name = target_name
        # Off while a database is being emptied and loaded: a mirrored row would break the baseline comparison
        self.mirror = True

    @classmethod
    def create(cls, set_id, strategy, target_name, **fields):
        restore_id = layout.new_ulid()
        data = {
            'restore_id': restore_id, 'set_id': set_id, 'strategy': strategy, 'target': target_name,
            'instance_id': conf.instance_id(), 'status': 'planned', 'steps': [], 'warnings': [],
            'started': timezone.now().isoformat(), 'seq': 0, **fields,
        }
        return cls(data, target_name)

    @property
    def restore_id(self):
        return self.data['restore_id']

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def local_path(self):
        return os.path.join(conf.spool_dir('journal'), f'{self.restore_id}.json')

    def step(self, name, status='done', **detail):
        self.data['steps'].append({'step': name, 'status': status, 'at': timezone.now().isoformat(), **detail})
        self.save(name)

    def update(self, status=None, **fields):
        if status:
            self.data['status'] = status
        self.data.update(fields)

    def warn(self, message):
        self.data['warnings'].append(message)

    def save(self, label='state'):
        self.data['seq'] += 1
        self.data['updated'] = timezone.now().isoformat()
        payload = json.dumps(self.data, indent=1, sort_keys=True, default=str).encode()
        path = self.local_path()
        fd = os.open(f'{path}.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
        os.replace(f'{path}.tmp', path)
        try:
            backend = get_backend(conf.get_config().target(self.target_name))
            key = f"{_prefix(self.data['instance_id'], self.restore_id)}{self.data['seq']:03d}-" \
                  f"{re.sub(r'[^a-z0-9-]', '-', label.lower())}.json"
            backend.put_bytes(key, payload)
        except (StorageError, ObjectExists) as e:
            # The local journal still records the step; status reports storage as stale
            self.data.setdefault('storage_errors', []).append(str(e))
        if self.mirror:
            self._mirror()

    def save_identities(self, keyring, snapshot):
        """
        Users and tokens before the restore: locally (0600) and encrypted in storage.
        """
        payload = json.dumps(snapshot, default=str).encode()
        path = os.path.join(conf.spool_dir('journal'), f'{self.restore_id}.identities.json')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
        try:
            backend = get_backend(conf.get_config().target(self.target_name))
            backend.put_bytes(f"{_prefix(self.data['instance_id'], self.restore_id)}identities.json.enc",
                              keyring.encrypt_bytes(payload))
        except StorageError as e:
            self.warn(f"identities snapshot not stored remotely: {e}")

    def load_identities(self, keyring=None):
        path = os.path.join(conf.spool_dir('journal'), f'{self.restore_id}.identities.json')
        if os.path.exists(path):
            with open(path, 'rb') as f:
                return json.loads(f.read())
        if keyring is None:
            return None
        try:
            backend = get_backend(conf.get_config().target(self.target_name))
            data = backend.get_bytes(f"{_prefix(self.data['instance_id'], self.restore_id)}identities.json.enc")
        except (ObjectNotFound, StorageError):
            return None
        return json.loads(keyring.decrypt_bytes(data))

    def _mirror(self):
        """
        Mirror into RestoreRun when the connected database has the plugin's tables.
        """
        from django.db import connection
        from django.utils.dateparse import parse_datetime
        try:
            if 'netbox_backup_restorerun' not in connection.introspection.table_names():
                return
            from ..models import RestoreRun
            RestoreRun.objects.update_or_create(restore_id=self.restore_id, defaults={
                'set_id': self.data['set_id'], 'source_instance_id': self.data.get('source_instance_id', ''),
                'components': self.data.get('components', []), 'strategy': self.data['strategy'],
                'status': self.data['status'], 'steps': self.data['steps'], 'plan': self.data.get('plan', {}),
                'slot_database': self.data.get('slot_database') or '',
                'previous_database': self.data.get('previous_database') or '',
                'safety_set_id': self.data.get('safety_set_id') or '',
                'rollback_until': parse_datetime(self.data['rollback_until']) if self.data.get('rollback_until')
                else None,
                'user': self.data.get('user', ''), 'started': parse_datetime(self.data['started']),
                'finished': parse_datetime(self.data['finished']) if self.data.get('finished') else None,
            })
        except Exception:
            pass  # The database may be empty, unmigrated or being replaced; the journal files are authoritative


def load(restore_id=None, statuses=None):
    """
    The newest journal (or a given one) from the local spool, falling back to storage.
    """
    journals = list_journals()
    if restore_id:
        journals = [j for j in journals if j.restore_id == restore_id.upper()]
    if statuses:
        journals = [j for j in journals if j['status'] in statuses]
    return journals[0] if journals else None


def list_journals():
    config = conf.get_config()
    found = {}
    directory = conf.spool_dir('journal')
    for name in os.listdir(directory):
        if name.endswith('.json') and not name.endswith('.identities.json'):
            with open(os.path.join(directory, name), 'rb') as f:
                data = json.loads(f.read())
            found[data['restore_id']] = Journal(data, data['target'])
    for target_name, target in config.targets.items():
        try:
            backend = get_backend(target)
            latest = {}
            for info in backend.list(f'{config.instance_id}/journal/'):
                parts = info.key.split('/')
                if len(parts) == 4 and re.match(r'^\d{3}-', parts[3]):
                    if parts[2] not in latest or parts[3] > latest[parts[2]]:
                        latest[parts[2]] = parts[3]
            for restore_id, name in latest.items():
                data = json.loads(backend.get_bytes(f'{config.instance_id}/journal/{restore_id}/{name}'))
                local = found.get(restore_id)
                if local is None or local['seq'] < data['seq']:
                    found[restore_id] = Journal(data, target_name)
        except StorageError:
            continue
    return sorted(found.values(), key=lambda j: j.restore_id, reverse=True)
