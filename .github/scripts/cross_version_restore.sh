#!/usr/bin/env bash
#
# Upgrade-by-restore: take a set on an older NetBox release and restore it into an empty, unmigrated database on a
# newer one, then check the data, the media files and the migration state came across.
#
# Required environment:
#   OLD_NETBOX / NEW_NETBOX    NetBox checkouts (repository roots)
#   OLD_PYTHON / NEW_PYTHON    Python interpreters with each release's requirements and this plugin installed
#   PLUGIN_DIR                 this repository
# Optional: PGHOST, PGPORT, PGUSER, PGPASSWORD for createdb/dropdb
set -euo pipefail

: "${OLD_NETBOX:?}" "${NEW_NETBOX:?}" "${OLD_PYTHON:?}" "${NEW_PYTHON:?}" "${PLUGIN_DIR:?}"

WORK="$(mktemp -d)"
OLD_DB=nbb_cross_old
NEW_DB=nbb_cross_new
MEDIA_FILE=image-attachments/cross-version-check.txt
export NBB_CROSS_WORK="$WORK"
export NETBOX_CONFIGURATION=nbb_cross
export PYTHONPATH="$WORK/conf:$PLUGIN_DIR/testing"
export PYTHONWARNINGS=ignore

# Same static configuration on both sides: one local target, one key for encryption, signing and decryption
mkdir -p "$WORK/conf" "$WORK/keys" "$WORK/storage"
cat > "$WORK/conf/nbb_cross.py" <<'PY'
import glob
import os

from netbox_backup_testing import *  # noqa: F401,F403

work = os.environ['NBB_CROSS_WORK']
keys = sorted(glob.glob(f'{work}/keys/*.pub.asc'))
fpr = os.path.basename(keys[0]).split('.')[0] if keys else None
PLUGINS_CONFIG = {
    'netbox_backup': {
        'instance_id': '5f1c2d3e-4b5a-4c6d-8e7f-90a1b2c3d4e5',
        'targets': {'local': {'backend': 'local', 'path': f'{work}/storage'}},
        'crypto': {
            'public_keys': f'file:{keys[0]}' if keys else None,
            'recipients': [fpr] if fpr else [],
            'signing_key': fpr,
            'signing_secret_key': f'file:{work}/keys/{fpr}.sec.asc' if fpr else None,
            'identities': [f'file:{work}/keys/{fpr}.sec.asc'] if fpr else [],
        },
        'spool_dir': f'{work}/spool',
    },
}
PY

manage() {  # manage <old|new> <args...>
    local side=$1; shift
    if [[ $side == old ]]; then
        (cd "$OLD_NETBOX/netbox" && NETBOX_DB_NAME=$OLD_DB "$OLD_PYTHON" manage.py "$@")
    else
        (cd "$NEW_NETBOX/netbox" && NETBOX_DB_NAME=$NEW_DB "$NEW_PYTHON" manage.py "$@")
    fi
}

counts() {  # Prints a JSON object of object counts that must survive the upgrade
    manage "$1" shell --no-imports -c "
import json
from dcim.models import Device, DeviceType, Site
from users.models import User
print('COUNTS=' + json.dumps({
    'sites': Site.objects.count(),
    'devices': Device.objects.count(),
    'device_types': DeviceType.objects.count(),
    'tagged_sites': Site.objects.filter(tags__slug='cross-version').count(),
    'users': User.objects.count(),
}, sort_keys=True))
" | sed -n 's/^COUNTS=//p'
}

cleanup() {
    dropdb --if-exists "$OLD_DB" || true
    dropdb --if-exists "$NEW_DB" || true
    rm -rf "$WORK"
}
trap cleanup EXIT
dropdb --if-exists "$OLD_DB" || true
dropdb --if-exists "$NEW_DB" || true
createdb "$OLD_DB"
createdb "$NEW_DB"

echo "::group::Old release: migrate, seed and back up"
manage old nbbackup keys generate --name 'cross-version <ci@example.com>' --out "$WORK/keys"
manage old migrate --no-input -v0
manage old shell --no-imports -c "
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from extras.models import Tag
from users.models import User
User.objects.create_superuser('cross-admin', 'cross@example.com', 'x')
tag = Tag.objects.create(name='cross-version', slug='cross-version')
manufacturer = Manufacturer.objects.create(name='Acme', slug='acme')
device_type = DeviceType.objects.create(manufacturer=manufacturer, model='X1', slug='x1')
role = DeviceRole.objects.create(name='Core', slug='core')
for i in range(12):
    site = Site.objects.create(name=f'Site {i}', slug=f'site-{i}')
    if i % 2:
        site.tags.add(tag)
    for j in range(3):
        Device.objects.create(name=f'dev-{i}-{j}', site=site, device_type=device_type, role=role)
"
old_media=$(manage old shell --no-imports -c "from django.conf import settings; print('MEDIA=' + settings.MEDIA_ROOT)" | sed -n 's/^MEDIA=//p')
mkdir -p "$old_media/$(dirname $MEDIA_FILE)"
echo "media from the old release" > "$old_media/$MEDIA_FILE"
expected=$(counts old)
echo "Expected: $expected"
manage old nbbackup run default
echo "::endgroup::"

echo "::group::New release: restore into an empty database"
new_media=$(manage new shell --no-imports -c "from django.conf import settings; print('MEDIA=' + settings.MEDIA_ROOT)" | sed -n 's/^MEDIA=//p')
rm -f "$new_media/$MEDIA_FILE"
manage new nbbackup list
manage new nbbackup restore plan latest
manage new nbbackup restore load latest --in-place
echo "::endgroup::"

echo "::group::New release: checks"
status=0
actual=$(counts new)
echo "Restored: $actual"
if [[ "$actual" != "$expected" ]]; then
    echo "::error title=Cross-version restore::object counts differ: expected $expected, got $actual"
    status=1
fi
if ! grep -q "media from the old release" "$new_media/$MEDIA_FILE" 2>/dev/null; then
    echo "::error title=Cross-version restore::media file was not restored"
    status=1
fi
if ! manage new migrate --check -v0; then
    echo "::error title=Cross-version restore::migrations are still pending after the restore"
    status=1
fi
restore_status=$(manage new shell --no-imports -c "
from netbox_backup.models import RestoreRun
print('RESTORE=' + RestoreRun.objects.get().status)
" | sed -n 's/^RESTORE=//p')
echo "Restore record: $restore_status"
if [[ "$restore_status" != "finished" ]]; then
    echo "::error title=Cross-version restore::unexpected restore record: $restore_status"
    status=1
fi
rm -f "$old_media/$MEDIA_FILE" "$new_media/$MEDIA_FILE"
echo "::endgroup::"
exit $status
