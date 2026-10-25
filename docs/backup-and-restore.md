# Backup and Restore in Depth

The long-form companion to [Backup Essentials](backup-essentials.md).

## Storage layout

```
<storage root>/
└── <instance_id>/                         one directory per NetBox installation
    └── 20261025T130926Z-full-2a0b/        one directory per backup
        ├── database.dump[.enc]            pg_dump custom format
        ├── media.tar.gz[.enc]
        ├── scripts.tar.gz[.enc]
        ├── reports.tar.gz[.enc]
        ├── configuration.tar.gz[.enc]
        └── manifest.json                  written LAST: the commit marker
```

* **The manifest is the source of truth.** The *Backups* list in the UI is a cache of the manifests, rebuilt by
  `netbox_backup sync`, by the housekeeping job, and after every restore. A database restore therefore never
  loses track of backups taken after the restored point.
* A directory without `manifest.json` is an **incomplete** upload (the process died). Retention deletes it after
  24 hours.
* `manifest.json` is never encrypted. It holds metadata only: names, sizes, checksums, row counts, versions
  and migrations. It contains no data and no secrets.

### Instance IDs

`instance_id` defaults to `netbox-<first 12 hex chars of sha256(SECRET_KEY)>`. Installations with different
secret keys never see each other's backups, even in a shared bucket. A DR host built with the original
configuration sees the original backups. Set `instance_id` explicitly to keep it stable across a `SECRET_KEY`
rotation. Restoring a foreign instance's backup requires `--instance <id>` or `--allow-foreign-instance`: a
"successful" restore of the wrong installation's data is the failure this guard exists for.

## How a backup is made

1. The row is created in the catalog with status *running*, and the storage target is probed (write, read,
   delete a probe object).
2. A PostgreSQL advisory lock is taken, so only one backup or restore runs at a time across all workers.
3. A `REPEATABLE READ` transaction is opened and **its snapshot is exported** (`pg_export_snapshot()`). Within
   that snapshot the plugin records:
   * an exact `count(*)` for every table,
   * every applied Django migration.

   `pg_dump --snapshot=<id>` then dumps **the same snapshot**, so the row counts in the manifest describe exactly
   the data in the dump, even while NetBox is in use.
4. For full backups, file trees are archived. The backup storage directory and work directory are excluded if
   they sit inside one of the trees. Trees served by a non-filesystem `STORAGES` backend are skipped with a
   warning.
5. Each artifact is SHA-256 hashed, then optionally encrypted and hashed again.
6. **Pre-upload verification**: `pg_restore --list` must show a `TABLE DATA` entry for every table with rows,
   and every archive must be readable. A backup that would contain nothing fails here instead of reporting
   success.
7. The artifacts are uploaded and their stored sizes checked, then `manifest.json` is uploaded.
8. On any failure the partial directory is deleted and the row is marked *failed* with the error.

The status is *Completed with warnings* when something was deliberately left out, for example configuration
without encryption, a missing directory, or remote media storage. The warnings are listed on the backup.

## Encryption

`encryption_key` is a passphrase. Every artifact gets a random 16-byte salt, and a 256-bit key is derived with
scrypt (N=2¹⁵, r=8, p=1). Data is encrypted in 1 MiB AES-256-GCM chunks. The chunk counter and a *final* flag are
bound into the associated data, so reordering, truncation and appended data are all detected, not only bit flips.

The manifest records a **key fingerprint** (a separate scrypt derivation), so a restore with the wrong key is
refused before anything is downloaded. Lose the key and the backups are unrecoverable. There is no key escrow.

Encryption needs the `cryptography` package (`pip install netbox-backup[encryption]`). Server-side encryption on
S3 (`server_side_encryption: 'aws:kms'`) can be used as well or instead.

## Verification levels

| Level | Checks | Cost |
|---|---|---|
| *(pre-upload)* | dump TOC covers every non-empty table, archives readable | always, during backup |
| `quick` | manifest valid; every artifact present in storage with the recorded size | a few HEAD requests |
| `deep` | quick + download, SHA-256 of stored bytes, decrypt, SHA-256 of plaintext, dump TOC, archive listing | full download |
| `restore` | deep + `pg_restore` into a scratch database `<db>_nbbk_verify_<rand>` and compare **every** table's row count | full download + restore; needs `CREATEDB` |

Run verification from the backup's page (*Verify*), with `POST /api/plugins/backup/backups/<id>/verify/`, or
with `netbox_backup verify <name> --level restore`. The result and the per-check log are stored on the backup.
The housekeeping job drops scratch databases left behind by an interrupted test restore.

## Retention

The housekeeping job (hourly by default) and every backup run apply this policy:

* successful backups older than `retention` are deleted, **except** the newest `keep_last` of each kind;
* failed or missing entries older than `retention` are removed from the catalog;
* incomplete uploads older than 24 hours are deleted from storage.

`keep_last` means a stopped schedule never prunes an instance down to nothing. Pre-restore safety backups follow
the same rules. Deleting a backup in the UI or API also deletes it from storage, once the transaction commits.
Preview a prune with `netbox_backup prune --dry-run`.

## How a restore works

`netbox_backup restore <name|prefix|latest|latest-full|latest-database>`

1. **Preflight** (also available alone as `--dry-run`). Nothing is changed:
   * the manifest format is supported, and the backup is not marked failed;
   * the backup belongs to this instance (or `--allow-foreign-instance`);
   * **code compatibility**: every migration recorded in the backup must be known to the running code. Squashed
     migrations are handled. If the backup was taken by newer NetBox or plugin code, the restore is refused:
     install that version first. Migrations from plugins that are not installed are a warning only. If the
     backup is *older*, the missing migrations are applied after the restore;
   * the encryption key fingerprint matches;
   * `pg_restore` is new enough for the dump and the server;
   * no other sessions are connected to the database (stop gunicorn/uwsgi and netbox-rq, or pass
     `--terminate-connections`);
   * the file-tree targets are local directories.
2. **Confirmation**: type the backup name, or pass `--yes` for automation.
3. **Safety backup** of the current state (`trigger: pre-restore`). This is a full backup if files are being
   restored, otherwise a database backup. It is skipped automatically when the target database is empty (new
   host), or explicitly with `--no-safety-backup`.
4. **Fetch**: every needed artifact is downloaded, checksummed and decrypted **before** anything is touched.
5. **Database**: Django's connection is closed and the advisory lock taken. The schema is dropped and recreated
   (falling back to `DROP OWNED BY CURRENT_USER` when the role does not own the schema). Then `pg_restore
   --no-owner --no-privileges --exit-on-error [--jobs N]` runs, and **every table's row count is compared with
   the manifest**.
6. **Files**: each archive is fully extracted next to its target, the target's contents are replaced, and the
   target directory itself is kept (it may be a mount point). Extraction uses tarfile's `data` filter, which
   blocks absolute paths, `..` and device files.
7. **Migrate**, if the backup predates the code (`--no-migrate` to skip).
8. **Finalize**: the Django cache is cleared, and background-job rows that the restored database still shows as
   queued or running (but which no longer exist in the task queue) are marked *errored*. Otherwise NetBox would
   consider scheduled jobs, including scheduled backups, already queued and never run them again. The catalog is
   re-synced from storage, and a **Restore** record is written into the restored database. It holds the status,
   components, versions, safety backup name, row-count mismatches and the full log.

   **Restart `netbox-rq` after every restore** so the system job schedules are re-created. Until then the
   **ScheduleConfigured** condition warns.

### Restore status

| Status | Meaning |
|---|---|
| Completed | Everything restored; all row counts match. |
| Completed with warnings | Restored and verified, but preflight raised warnings (e.g. migrations applied, plugin missing). |
| **Recovery required** | The restore ran, but the database **does not contain what the backup recorded**. Do not return the instance to service. The record lists each mismatching table. The **RecoveryRequired** condition stays in *error* until a later restore succeeds. |
| Failed | The restore stopped. If the database step had started, the database may be partially restored: restore the safety backup (its name is in the log). |

A healthy-looking NetBox after a restore is not proof the data came back. The row-count comparison is.

### Partial restores

`--components database` restores only the database, and `--components media,scripts` restores only files. Use
`--extract-config DIR` to also write the backed-up configuration files to `DIR`. Configuration is never applied
automatically.

## Manual restore without the plugin

Every artifact is a standard format:

```bash
python manage.py netbox_backup download <name> /restore     # verifies and decrypts
pg_restore --no-owner --no-privileges --clean --if-exists --dbname=netbox /restore/database.dump
tar -xzf /restore/media.tar.gz -C /opt/netbox/netbox/media
```

Without NetBox at all, decryption is plain AES-GCM over the format documented in `netbox_backup/engine/crypto.py`.

## Scheduling details

Scheduled backups are NetBox **system jobs**. They are registered when `netbox-rq` starts, and they show under
*Operations → Jobs*:

| Job | Interval setting |
|---|---|
| Scheduled database backup | `database_backup_interval` |
| Scheduled full backup | `full_backup_interval` |
| Backup housekeeping | `housekeeping_interval` |

After changing an interval, restart `netbox-rq`. A queued job whose interval no longer matches the configuration
runs once more as a no-op and stops recurring. With several workers, only one backup runs at a time (advisory
lock); the others skip with a log entry.
