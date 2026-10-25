# Backup Essentials

The short version: set backups up once, check them, and know how to restore onto a new host.
[Backup and Restore in Depth](backup-and-restore.md) covers the details.

## What is protected

| Kind | Contents | Default schedule |
|---|---|---|
| **Database** backup | PostgreSQL dump of the NetBox database (all core and plugin tables) | Daily, **on** |
| **Full** backup | Database + `MEDIA_ROOT` + `SCRIPTS_ROOT` + `REPORTS_ROOT` + configuration files* | **Off** |

\* Configuration (`configuration.py`, `ldap_config.py`, any `extra_configuration_files`) holds `SECRET_KEY`,
`API_TOKEN_PEPPERS` and database credentials. It is included only when encryption is on, unless you set
`include_configuration: True`.

Not protected: Redis (cache and queue only, safe to lose), static files (regenerate with `collectstatic`), the
Python environment, and media held in remote object storage (`STORAGES` with django-storages). Back those up with
the object store's own versioning or replication.

## 1. Set up once

1. Install the plugin and the PostgreSQL client tools on the NetBox host. The client must be the **same major
   version as the server, or newer** (`pg_dump --version`).
2. Choose **off-host storage**. A backup on the same disk as the database is not a disaster-recovery backup. Use
   an S3 bucket or a mounted NFS share. Give each NetBox instance its own bucket or prefix.
3. Generate an encryption key with `manage.py netbox_backup genkey`. Store it **in your password manager as
   well** as in `configuration.py`, because the backups cannot be restored without it.
4. Enable full backups, for example `'full_backup_interval': 1440`.
5. Run `manage.py migrate`, restart `netbox` and `netbox-rq`, then take a first backup:

   ```bash
   python manage.py netbox_backup create --full --verify restore
   ```

## 2. Check regularly

```bash
python manage.py netbox_backup status          # exit code: 0 ok/info, 1 warning, 2 error
```

You can also open **Backup & Recovery → Status** in the UI, or call `GET /api/plugins/backup/status/`. Wire the
exit code or the API into your monitoring. The conditions that matter most:

* **BackupAvailable**: a successful backup exists and is not stale (older than 2× the shortest interval).
* **LastBackupSucceeded**: the most recent run did not fail.
* **RestoreTested**: at least one backup has passed a *test restore*. Schedule one regularly from the UI
  (*Verify → Test restore*) or with `netbox_backup verify latest --level restore`.
* **StorageIsolated**: no other instance writes to the same location.

A backup that reports *Completed* is not proven restorable until it has passed a test restore. Test-restore
verification restores the dump into a scratch database and compares every table's row count with the manifest.

## 3. Restore onto a new host

1. Install the **same NetBox version** as the backup (or newer) with the same plugins, following the normal
   NetBox installation guide up to and including creating an empty database. Do **not** run `migrate`.
2. Install this plugin with the **same `PLUGINS_CONFIG['netbox_backup']`**: same storage, same `encryption_key`,
   and the same `SECRET_KEY`, or the same explicit `instance_id`. The instance ID is derived from `SECRET_KEY`
   unless set explicitly, and a restore only offers that instance's backups.

   If the original `configuration.py` is lost, fetch it from a full backup on any host that has the key:

   ```bash
   python manage.py netbox_backup download latest-full /tmp/recovered   # decrypts into /tmp/recovered
   tar -xzf /tmp/recovered/configuration.tar.gz -C /tmp/recovered
   ```

3. Restore:

   ```bash
   python manage.py netbox_backup list
   python manage.py netbox_backup restore latest-full --dry-run
   python manage.py netbox_backup restore latest-full
   ```

   An empty target database is detected automatically; no safety backup is taken in that case.
4. Run `manage.py collectstatic --no-input`, then start `netbox` and `netbox-rq`.
5. Log in and spot-check. The restore record (**Backup & Recovery → Restores**) shows the row-count verification.

## Restoring on the same host (rollback)

```bash
sudo systemctl stop netbox netbox-rq
python manage.py netbox_backup restore <name>
sudo systemctl start netbox netbox-rq
```

A safety backup of the current state is taken first. Its name is printed, so the restore can be undone with
`netbox_backup restore <safety-backup-name>`.
