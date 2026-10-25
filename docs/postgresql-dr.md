# PostgreSQL DR and point-in-time recovery

Point-in-time recovery is outside the plugin's v1 scope. On a shared PaaS server, PITR comes from the provider,
and only for NetBox: extract the NetBox database from a temporary provider clone and load it through the normal
restore. The shared server itself is never restored. pgBackRest or WAL-G remain options for self-hosted servers;
the plugin already monitors a pgBackRest stanza.

## Tiers

| Tier | RPO | Restores to | Owned by |
|---|---|---|---|
| Logical sets (this plugin) | the schedule interval | any PostgreSQL at or above the source major, any host or cloud | plugin |
| pgBackRest / WAL-G | seconds (last archived WAL) | the same major, by time, LSN or immediate | the database host; the plugin monitors |
| Provider PITR (Azure Flexible Server, RDS, Cloud SQL) | provider-defined, typically minutes | the same provider | the provider; NetBox is extracted from a clone |

Media RPO is always the set schedule: no database backup, provider PITR included, captures uploaded files.

## Shared PaaS: a point in time without touching the shared server

Until the plugin's provider-clone load (`restore load --from-dsn`, planned) exists, the procedure is:

1. The platform team creates a point-in-time clone on a new, temporary server, e.g.
   `az postgres flexible-server restore --restore-time ...`. Production is not changed. Record the clone's actual
   recovery point, not the requested one.
2. Make the clone reachable from an ops container (firewall rules and private endpoints are not copied).
3. Dump only the NetBox database from the clone and load it into the restore slot:
   `pg_dump -Fc -d "<clone DSN>/netbox" -f netbox.pitr.dump`, then `pg_restore --no-owner --exit-on-error -d
   netbox_b netbox.pitr.dump` into the marked slot. Or take a set from it with a NetBox container whose
   `DATABASES` point at the clone, and restore that set.
4. Continue with the normal switch and `nbbackup restore finish`. Media for that moment comes from the nearest set.
5. The platform team deletes the clone.

The clone copies the whole shared server, every tenant's data included. That makes it a platform runbook with
data-governance sign-off, not something the plugin triggers.

## Pairing with pgBackRest (self-hosted servers)

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
cd /opt/netbox/netbox && python manage.py migrate && python manage.py nbbackup catalog sync
sudo -u postgres pgbackrest --stanza=netbox --type=full backup     # avoid the timeline-fork error [058]
sudo systemctl start netbox netbox-rq
```

`nbbackup catalog sync` re-reads the catalog from storage afterwards, so sets taken after the recovery point are
listed again.

A database that comes up cleanly is not proof the data is there. Compare its counts with a set from around the same
time: `nbbackup show <set>` prints the set's row total and newest `last_updated`.

## Logical restores across PostgreSQL versions

A logical backup restores into any PostgreSQL major version that NetBox supports, which makes it the tool for
PostgreSQL upgrades and host migrations:

```bash
python manage.py nbbackup run                                # on the old server
# point DATABASES at the new, empty database on the new server
python manage.py nbbackup restore load latest --in-place     # an empty database needs no --confirm
```

The `pg_restore` binary must be at least as new as the `pg_dump` that made the dump, and at least the new
server's version. `restore plan` checks both.
