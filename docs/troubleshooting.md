# Troubleshooting

Start with `python manage.py netbox_backup status`. Every failed backup records its error on the backup and in
its job log (*Operations → Jobs*). Every restore writes its full log to the *Restores* record.

## Backups

### `pg_dump major version 14 is older than the PostgreSQL server (16)`

`pg_dump` cannot dump a newer server. Install the matching client (e.g. `postgresql-client-16`) and either put it
first on `PATH` or point the plugin at it:
`'pg_dump': '/usr/lib/postgresql/16/bin/pg_dump', 'pg_restore': '/usr/lib/postgresql/16/bin/pg_restore'`.

### `PostgreSQL client 'pg_dump' not found`

The client tools are missing on the host that runs **netbox-rq** (scheduled backups run in the worker, not in the
web process). In containers, add them to the image: `apt-get install postgresql-client-16`.

### `pg_dump exited with status 1: ... permission denied for table ...`

The NetBox database user does not own every table, for example after a manual restore performed as `postgres`.
Fix ownership: `REASSIGN OWNED BY postgres TO netbox;` (run in the NetBox database, after checking what else
`postgres` owns there).

### `Backup storage is not usable` / `StorageReachable: error`

The probe (write, read, delete `.netbox-backup-probe`) failed. Common causes:

* **local**: the directory is not writable by the user running NetBox **and** netbox-rq (often `netbox`), or an
  NFS share is not mounted.
* **S3**: wrong region or endpoint (`PermanentRedirect`, `AuthorizationHeaderMalformed`), missing
  `s3:PutObject` / `s3:DeleteObject`, clock skew (`RequestTimeTooSkewed`), or MinIO needing
  `'addressing_style': 'path'`.
* **S3 with a private CA**: set `'verify': '/etc/ssl/certs/internal-ca.pem'`.

### Backups never run on schedule

* Is `netbox-rq` running? The schedule lives in the worker.
* After changing an interval, **restart netbox-rq**.
* *Operations → Jobs* should show *Scheduled database backup* (and *Scheduled full backup*) in state
  *Scheduled*.
* If two backups start at the same time, the second is skipped (`Another backup or restore is already running`).
  This is expected.

### Scheduled backups stopped after a restore / `no job is queued for: database`

The restore replaced NetBox's job table with the backup's copy. Orphaned rows are marked *errored* during the
restore, but the schedules are only re-created when **netbox-rq restarts**:
`sudo systemctl restart netbox-rq`. The same warning appears after enabling a schedule without restarting the
worker.

### Backup completed *with warnings*

Read the warnings on the backup page. They are deliberate omissions:

* `Configuration not included`: enable `encryption_key`, or set `include_configuration: True`, or keep
  `SECRET_KEY` and `API_TOKEN_PEPPERS` safe elsewhere.
* `storage 'default' uses ...S3Storage`: media lives in object storage. Protect it with bucket versioning or
  replication.
* `Directory for 'reports' does not exist`: harmless if you have no reports. Remove it from `include_files`.

### `Dump is missing data for N table(s)`

Pre-upload verification found tables with rows but no `TABLE DATA` entry in the dump, usually because
`pg_dump_extra_args` contains `--exclude-table-data` or `--schema-only`. The backup is refused rather than stored
incomplete.

### Rows stuck in *Running*

The process died mid-backup (worker killed, OOM, host reboot). The next housekeeping run, or `netbox_backup sync`,
marks them *failed: Interrupted*. Partially uploaded directories are removed after 24 hours.

### The disk fills up during backups

The dump and archives are built in `work_dir` (default: system temp) before upload. Point `work_dir` at a volume
with space for roughly *database size + media size*. Also check that retention is pruning: `netbox_backup prune
--dry-run`.

## Verification

### `checksum mismatch (corrupted in storage?)`

The bytes in storage differ from what was uploaded: bit rot, a truncated copy, or tampering. Do not rely on this
backup. Check that other backups pass `verify --level deep`, and investigate the storage.

### `Decryption failed: wrong encryption key or corrupted file`

Either the key differs (preflight normally catches this through the key fingerprint) or the encrypted file was
modified. AES-GCM authentication makes the two indistinguishable by design.

### Test restore: `cannot create scratch database (needs CREATEDB)`

`--level restore` creates `<db>_nbbk_verify_<random>`. Grant it: `ALTER ROLE netbox CREATEDB;`. Alternatively,
run test restores on a separate staging NetBox pointed at the same storage, with `instance_id` and
`encryption_key` set to production's values.

### Test restore: `row counts do not match`

The dump does not contain what was counted at backup time. This should be impossible, because counts and dump
come from the same exported snapshot, so treat it as a serious finding. Check for custom `pg_dump_extra_args`, then
report a bug with the verification log.

## Restores

### `Backup belongs to instance 'X' but this installation is 'Y'`

The instance ID is derived from `SECRET_KEY` unless set explicitly. On a DR host, use the original `SECRET_KEY`
(or set the original `instance_id`). If you really do mean to load another installation's data, for example
production into staging, pass `--instance X`.

`netbox_backup list --instances` shows every instance ID in the storage location.

### `Backup was taken by newer code ... migration(s) are unknown here`

The backup contains schema this NetBox or plugin version does not have. Install the NetBox version shown, and
the same plugin versions (`netbox_backup show <name>` lists them), then retry. `--allow-newer` exists but almost
always produces a broken instance.

### `Backup contains data for apps that are not installed here: some_plugin`

The backup's database includes a plugin's tables. They are restored, but unused until the plugin is installed.
Install it and run `migrate`.

### `N other session(s) are connected to the database`

Stop `netbox` (gunicorn/uwsgi), `netbox-rq` and any housekeeping cron, then retry. Or pass
`--terminate-connections` if you are certain nothing else should be using the database.

### `pg_restore exited with status 1: ... must be owner of extension/schema`

The NetBox role cannot recreate the schema. Either grant it ownership of the database (`ALTER DATABASE netbox
OWNER TO netbox;`), or restore manually as a superuser (see *Manual restore* in
[Backup and Restore in Depth](backup-and-restore.md#manual-restore-without-the-plugin)).

### Restore status *Recovery required*

The restore finished, but row counts differ from the manifest. Open the *Restores* record for the per-table list.
Common causes:

* `--allow-newer`, or a migration that ran in between (the counts are compared *before* migrations, so this
  should not happen; please report it);
* `pg_restore` errors hidden by a custom wrapper binary.

Until resolved, the **RecoveryRequired** condition stays in *error*. Restore the safety backup to return to the
previous state, or restore a different backup.

### Restore *failed* halfway

The log says whether the database step had started. If it had, the database may be partially restored: restore
the safety backup named in the log (`netbox_backup restore <safety-name> --no-safety-backup`), or any other
backup.

### Logins or API tokens stop working after restoring onto a new host

* Sessions and password reset links depend on `SECRET_KEY`.
* **API tokens (v2) are hashed with `API_TOKEN_PEPPERS`**. Without the original peppers no token validates.

Retrieve the original configuration from a full backup with `netbox_backup download <full-name> /tmp/cfg` and
`tar -xzf /tmp/cfg/configuration.tar.gz -C /tmp/cfg`, then copy `SECRET_KEY` and `API_TOKEN_PEPPERS` across.

### Uploaded images or attachments are missing after a restore

You restored a **database** backup, or used `--components database`. Restore a **full** backup's `media`
component: `netbox_backup restore <full-name> --components media`.

## Getting help

Include the output of `netbox_backup status --json`, `netbox_backup show <name> --json` (it contains no secrets),
and the job or restore log when you open an issue.
