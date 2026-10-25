# netbox-backup

Scheduled, encrypted, verified backups and disaster recovery for open-source [NetBox](https://github.com/netbox-community/netbox).

NetBox Enterprise backs up at the platform layer (Velero for the install, pgBackRest for the database). OSS NetBox
runs on VMs, Docker Compose and Helm alike, so this plugin works at the application layer instead. It keeps the
restore outcomes Enterprise offers and removes its silent-failure modes. The design baseline is an external, shared
PaaS PostgreSQL such as Azure Database for PostgreSQL Flexible Server: everything works with database-owner rights,
and the plugin never alters NetBox's schema.

| | |
|---|---|
| **Backs up** | The NetBox database (`pg_dump` inside an exported snapshot), media and scripts read through Django `STORAGES`, and the configuration |
| **Stores to** | Local disk / NFS / SMB, S3 and compatibles (AWS, MinIO, Ceph, Wasabi, Hetzner, IONOS), Azure Blob; more through the `netbox_backup.backends` entry point |
| **Encrypts** | GnuPG, streaming: AES-256 to two recipients (an operational key and an offline recovery key), with a signed manifest. There is no unencrypted mode |
| **Proves** | A set counts only after its checksums verify. The dump is checked row for row against the snapshot while it streams. A restore counts only after the baseline matches |
| **Immutability** | Bucket or container default retention in compliance mode. The worker can neither delete nor re-lock sets; pruning needs a separate credential |
| **Schedules** | Five-field cron per policy with a time zone; GFS retention plus a minimum age |
| **Restores** | Into a second, empty database (the restore slot) followed by a switch, or in place behind a verified safety set. Both can be rolled back |
| **Reports** | Health in the UI, a dashboard widget, the REST API, `nbbackup status`, Prometheus metrics, and an external dead-man's check |

Requires NetBox 4.6 or 4.7, PostgreSQL 15+, Python 3.12+, GnuPG 2.4+ and the PostgreSQL client tools at least as
new as the server. Scheduled backups need an RQ worker serving the plugin's queue.

## Quick start

```bash
source /opt/netbox/venv/bin/activate
pip install "netbox-backup[s3]"        # or [azure], or [all]; the core install pulls no cloud SDKs
cd /opt/netbox/netbox
```

Add `PLUGINS = ['netbox_backup']` to `configuration.py`, then create three key pairs: *ops* decrypts restores,
*recovery* is kept offline and alone decrypts the config component with the secrets, *signing* lives on the worker.
Each prints its fingerprint. Generate the recovery key on an offline machine and bring only its public key here.

```bash
python manage.py nbbackup keys generate --role ops --name "netbox-backup ops" --out /etc/nbb/ops
python manage.py nbbackup keys generate --role signing --name "netbox-backup signing" \
    --passphrase-env NBB_SIGNING_PASSPHRASE --out /etc/nbb/signing
# offline: nbbackup keys generate --role recovery --name "netbox-backup recovery" --passphrase-env ... --out /media/usb
cat /etc/nbb/*/*.pub.asc /media/usb/*.pub.asc > /etc/nbb/pubkeys.asc
# Move the ops secret key into the secret store; only the signing key stays on this host.
```

Then complete the configuration:

```python
PLUGINS = ['netbox_backup']

PLUGINS_CONFIG = {
    'netbox_backup': {
        'instance_id': '7b0e5f2c-3c1a-4d6e-9f1a-2b3c4d5e6f70',   # uuidgen; pin it, DR needs it
        'targets': {
            'offsite': {
                'backend': 's3', 'bucket': 'netbox-backups', 'prefix': 'prod', 'region': 'eu-central-1',
                'credentials': {'access_key': 'env:NBB_S3_KEY', 'secret_key': 'file:/run/secrets/nbb_s3'},
                'retention_days': 35,   # the bucket's default retention; probe verifies it
            },
        },
        'policies': {
            'daily': {'target': 'offsite', 'schedule': 'daily'},   # 01:00 in TIME_ZONE, every component
        },
        'crypto': {
            'public_keys': 'file:/etc/nbb/pubkeys.asc',
            'recipients': ['<ops fingerprint>'],
            'recovery_key': '<recovery fingerprint>',   # also gets everything; alone gets the config component
            'signing_key': '<signing fingerprint>',
            'signing_secret_key': 'file:/etc/nbb/signing/<signing fingerprint>.sec.asc',
            'signing_passphrase': 'env:NBB_SIGNING_PASSPHRASE',
            # 'identities': ['file:/run/secrets/nbb_ops.key'],   # mount only for restores
        },
    },
}
```

```bash
python manage.py migrate
python manage.py rqworker netbox_backup.netbox_backup &       # the plugin's own queue (a systemd unit in production)
python manage.py nbbackup target probe                        # retention lock, lifecycle rule, create-only writes
python manage.py nbbackup run daily                           # first set, verified at level 1
python manage.py nbbackup status
```

Then open **Backup → Status** in the NetBox UI.

## Restoring

Restores run from an ops shell or a one-shot container, never from the web process:

```bash
manage.py nbbackup restore plan latest           # preflight only: recommends --slot or --in-place, and says why
manage.py nbbackup restore load latest --slot    # loads into the marked slot; NetBox keeps running meanwhile
# stop web and workers, point DATABASES['default']['NAME'] at the slot, start one ops container
manage.py nbbackup restore finish                # migrate, reset jobs/queues/sessions, write media
```

Without a second database, `restore load latest --in-place --confirm <database>` first takes a safety set and
verifies it, then replaces the live database. `restore rollback` undoes either kind, and `restore finalize` gives
the space back once the rollback window has closed. When no NetBox image can boot at all, the standalone
`netbox-backup restore-db` loads a set using only the configuration file.

## Documentation

* [Backup essentials](docs/backup-essentials.md): set up once, check it, rehearse a restore, recover onto a new host
* [Backup and restore in depth](docs/backup-and-restore.md): set format, crypto, verification, the restore workflow, security
* [Configuration reference](docs/configuration.md)
* [CLI, REST API and metrics reference](docs/reference.md)
* [PostgreSQL DR and point-in-time recovery](docs/postgresql-dr.md)
* [Troubleshooting](docs/troubleshooting.md)
* [Enterprise parity](docs/enterprise-parity.md): how each NetBox Enterprise backup behaviour maps to this plugin

## Development

```bash
git clone https://github.com/netbox-community/netbox.git && cd netbox && pip install -r requirements.txt
pip install -e /path/to/netbox-backup[all] moto
cd netbox
PYTHONPATH=/path/to/netbox-backup/testing NETBOX_CONFIGURATION=netbox_backup_testing \
    python manage.py test netbox_backup
```

The suite runs real `pg_dump`/`pg_restore` and `gpg` round trips against the test database, with S3 tested against
moto. With `NBB_TEST_AZURITE=1` and Azurite on port 10000, the Azure Blob contract tests run as well. Restore tests
create and drop a second database, so the test role needs `CREATEDB`; it does not need superuser rights.

## License

Apache 2.0
