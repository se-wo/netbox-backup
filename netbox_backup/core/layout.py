"""
Object layout and set identifiers.

    <prefix>/<instance_id>/instance.json
    <prefix>/<instance_id>/sets/<YYYY>/<MM>/<set_id>/manifest.json
    <prefix>/<instance_id>/sets/<YYYY>/<MM>/<set_id>/db.pgdump.enc
    <prefix>/<instance_id>/sets/<YYYY>/<MM>/<set_id>/media.tar.zst.enc
    <prefix>/<instance_id>/sets/<YYYY>/<MM>/<set_id>/scripts.tar.zst.enc
    <prefix>/<instance_id>/sets/<YYYY>/<MM>/<set_id>/config.json.enc
    <prefix>/<instance_id>/sets/<YYYY>/<MM>/<set_id>/_COMPLETE
    <prefix>/<instance_id>/journal/<restore_id>.json
    <prefix>/<instance_id>/reports/...

set_id is a ULID, so keys sort by time on every backend. The storage driver adds <prefix>.
"""
import os
import re
import time
from datetime import UTC, datetime

INSTANCE_FILE = 'instance.json'
MANIFEST = 'manifest.json'
COMPLETE = '_COMPLETE'

OBJECT_NAMES = {
    'database': 'db.pgdump.enc',
    'media': 'media.tar.zst.enc',
    'scripts': 'scripts.tar.zst.enc',
    'config': 'config.json.enc',
}
COMPONENT_BY_OBJECT = {v: k for k, v in OBJECT_NAMES.items()}

_CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
ULID_RE = re.compile(r'^[0-9A-HJKMNP-TV-Z]{26}$')


def new_ulid(when=None):
    """
    A ULID: 48-bit millisecond timestamp and 80 random bits, Crockford base32, 26 characters.
    """
    ms = int((when.timestamp() if when else time.time()) * 1000)
    value = (ms << 80) | int.from_bytes(os.urandom(10), 'big')
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return ''.join(reversed(chars))


def ulid_time(ulid):
    value = 0
    for char in ulid.upper():
        value = value * 32 + _CROCKFORD.index(char)
    return datetime.fromtimestamp((value >> 80) / 1000, UTC)


def is_ulid(value):
    return bool(ULID_RE.match(str(value).upper()))


def instance_key(instance_id):
    return f'{instance_id}/{INSTANCE_FILE}'


def sets_prefix(instance_id):
    return f'{instance_id}/sets/'


def set_prefix(instance_id, set_id):
    when = ulid_time(set_id)
    return f'{instance_id}/sets/{when:%Y}/{when:%m}/{set_id}'


def object_key(instance_id, set_id, name):
    return f'{set_prefix(instance_id, set_id)}/{name}'


def journal_key(instance_id, restore_id):
    return f'{instance_id}/journal/{restore_id}.json'


def parse_set_key(key):
    """
    Split a key below sets/ into (instance_id, set_id, object name), or None if it isn't one.
    """
    match = re.match(r'^([^/]+)/sets/\d{4}/\d{2}/([0-9A-Z]{26})/([^/]+)$', key)
    return (match.group(1), match.group(2), match.group(3)) if match else None
