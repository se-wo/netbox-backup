# PostgreSQL Disaster Recovery

## Two layers

| | Logical backups (this plugin) | Physical backups + WAL (pgBackRest) |
|---|---|---|
| Recovery point | the moment of each backup | **any point in time** within the WAL archive |
| Granularity | per database | whole PostgreSQL cluster |
| Portable across PG major versions | **yes** | no |
| Restorable without the original server config | **yes** | needs matching PostgreSQL setup |
| Includes media, scripts, configuration | **yes** (full backups) | no |
| Runs where | on the NetBox host, as the NetBox DB user | on the database server, as `postgres` |
| Verifiable by NetBox | **yes**: row counts, test restores | status only (see below) |

For most NetBox installs, frequent logical backups (for example `database_backup_interval: 60`) are enough. An
hour of lost edits is the worst case, and recovery is one command.

If you need a recovery point objective of minutes, or a rewind to just before a bad bulk edit, add pgBackRest on
the database server **as well**. Keep the plugin's full backups for files, configuration and portability.

## Pairing with pgBackRest

NetBox Enterprise 2.3+ runs pgBackRest for its built-in database (weekly full, daily incremental, continuous WAL
archiving). The same setup on a community install looks like this, on the **database server**:

`/etc/pgbackrest/pgbackrest.conf`

```ini
[global]
repo1-type=s3
repo1-s3-bucket=netbox-pgbackrest
repo1-s3-endpoint=s3.eu-central-1.amazonaws.com
repo1-s3-region=eu-central-1
repo1-path=/prod
repo1-retention-full=4
repo1-cipher-type=aes-256-cbc
repo1-cipher-pass=<long random string>
start-fast=y

[netbox]
pg1-path=/var/lib/postgresql/16/main
```

`postgresql.conf`

```ini
archive_mode = on
archive_command = 'pgbackrest --stanza=netbox archive-push %p'
wal_level = replica
```

Then:

```bash
sudo -u postgres pgbackrest --stanza=netbox stanza-create
sudo -u postgres pgbackrest --stanza=netbox check
# cron: weekly full, daily incremental
0 1 * * 0   postgres pgbackrest --stanza=netbox --type=full backup
0 1 * * 1-6 postgres pgbackrest --stanza=netbox --type=incr backup
```

Use a **separate bucket or prefix per cluster**. pgBackRest happily restores another cluster's repository.

### Monitoring it from NetBox

If `pgbackrest` is installed on the NetBox host and can read the repository (same `pgbackrest.conf`, read-only
credentials are enough), add:

```python
'pgbackrest': {'stanza': 'netbox', 'max_age': '8 days'},   # optional: 'binary', 'config'
```

The Status page and API then report a **PointInTimeRecovery** condition from `pgbackrest info`. It is an error
if the stanza is broken or has no backups, and a warning if there is no WAL archive (point-in-time recovery is
unavailable) or the newest backup is older than `max_age`.

### Point-in-time restore

```bash
sudo systemctl stop netbox netbox-rq
sudo systemctl stop postgresql
sudo -u postgres pgbackrest --stanza=netbox --delta \
     --type=time "--target=2026-10-25 09:41:00+00" --target-action=promote restore
sudo systemctl start postgresql
cd /opt/netbox/netbox && python manage.py migrate && python manage.py netbox_backup sync
sudo systemctl start netbox netbox-rq
```

`netbox_backup sync` re-reads the catalog from storage afterwards, so backups taken after the recovery point
are listed again.

Before relying on a point-in-time restore, compare row counts against a logical backup from around the same
time. `netbox_backup show <name> --json | jq .database.row_counts` gives the expected counts. A database that
comes up cleanly is not proof the data is there.

## Logical restores across PostgreSQL versions

A logical backup restores into any PostgreSQL major version that NetBox supports, which makes it the tool for
PostgreSQL upgrades and host migrations:

```bash
python manage.py netbox_backup create --verify deep          # on the old server
# point DATABASES at the new, empty PostgreSQL server
python manage.py netbox_backup restore latest
```

The `pg_restore` binary must be at least as new as the `pg_dump` that made the dump, and at least the new
server's version. Preflight checks both.
