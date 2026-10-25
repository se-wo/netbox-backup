# NetBox Enterprise Feature Parity

This plugin was designed from the public NetBox Enterprise backup documentation:

* Backup Essentials: `netboxlabs.com/docs/enterprise/nbe-backup-essentials/`
* Backup and Restore in Depth: `netboxlabs.com/docs/enterprise/nbe-backups/`
* PostgreSQL Disaster Recovery: `netboxlabs.com/docs/enterprise/nbe-postgresql-dr/` (and its troubleshooting page)
* Advanced Tools and Troubleshooting: `netboxlabs.com/docs/enterprise/nbe-troubleshooting/`

No Enterprise code was used or inspected. The behaviours below were taken from the documentation. Where
Enterprise relies on Kubernetes machinery (Velero, the Crunchy PostgreSQL operator, the KOTS admin console), the
plugin provides the equivalent outcome with tools available on every NetBox install.

## What Enterprise documents, and what this plugin does

| Enterprise (as documented) | netbox-backup |
|---|---|
| **Disaster-recovery backup**: a Velero snapshot of the whole install (volumes, databases, configuration) | **Full backup**: consistent DB dump + media + scripts + reports + configuration files |
| **PostgreSQL backup**: pgBackRest, on by default, weekly full + daily incremental, continuous WAL archiving | **Database backup**, on by default (daily), logical `pg_dump`. Optional pgBackRest **monitoring** (`PointInTimeRecovery` condition) and a [setup guide](postgresql-dr.md) for WAL/PITR on the DB server |
| DR backup schedules are off by default | `full_backup_interval` is off by default |
| Retention policy card: number + unit, default **1 month** | `retention: '1 month'` (same syntax: `'30 days'`, `'2 weeks'`, …), plus `keep_last` |
| S3 storage settings entered in the admin console | `storage` backend `s3` (any S3-compatible target), or `local`/NFS |
| "Backup now" from the admin console's Backups tab | **Run backup** in the UI, `POST /api/plugins/backup/backups/run/`, `netbox_backup create` |
| Restore via the installer's `restore` command, confirming at a "Restore from backup instance?" prompt | `netbox_backup restore`, which requires typing the backup name (or `--yes`) |
| **Restores always rebuild the database from the PostgreSQL backups**; the DR snapshot deliberately excludes the PG data volume | The database is restored only from the logical dump, never from a file-level copy of a running data directory |
| **A healthy cluster after restore does not prove the data is back; check row counts** | Row counts for every table are recorded at backup time (same snapshot as the dump) and **compared automatically** after every restore and test restore |
| `PostgresRecoveryRequired`, `PostgresRestoreFailed`, `PostgresRestorePending` conditions; a restored-but-unrecovered DB must not report Ready | `RestoreEvent` status `recovery-required` / `failed`, and a **RecoveryRequired** health condition in *error* |
| *"Absence of RestoreFailed proves nothing"* | Health conditions report positive evidence (**RestoreTested**, **BackupVerified**), not only the absence of failures |
| **A DR backup can report Completed and hold no volume data**; verify before relying on it | Pre-upload check that the dump has data for every non-empty table; `deep` and `restore` verification levels |
| **Shared buckets: restoring another cluster's backup succeeds and brings up the other cluster's data**; give each cluster its own bucket or prefix | Backups are namespaced by `instance_id`. Restore **refuses** foreign backups by default. **StorageIsolated** warns when other instances share the location |
| **Restore filters backups by installer version**; download the installer matching the backup | Preflight compares the backup's applied migrations with the running code: **refuses** backups from newer code, migrates backups from older code |
| Pre-2.3.0 backups cannot restore the database; take a backup right after upgrading | Manifest format is versioned (`netbox-backup/1`). Documentation recommends `create --full --verify restore` before and after upgrades |
| Reset before restoring on the same host; verify the backup first | Automatic **safety backup** before every restore (skipped on an empty DR host), and artifacts are downloaded and verified **before** anything is wiped |
| Manual backups: `pg_dumpall` of the built-in PostgreSQL, plus media/scripts/reports and secrets | `netbox_backup download` gives standard `pg_restore`-able dumps and tarballs; manual restore documented |
| Support bundles for diagnostics | `netbox_backup status --json` / `show --json` (no secrets), job logs, restore logs |
| Encryption of backups at rest | Client-side AES-256-GCM with key fingerprint checks, and optional S3 SSE |

## Differences by design

* **No restore from the web UI.** Enterprise restores run from the installer on the host. Here they run from the
  CLI, because a restore replaces the database the web UI is served from, and NetBox services should be
  stopped first.
* **Logical rather than physical database backups by default.** A plugin runs as the NetBox database user,
  often on a different host from PostgreSQL. It cannot configure `archive_command` or read the data directory.
  Logical dumps are portable across PostgreSQL major versions and verifiable from NetBox. PITR is a documented,
  optional, monitored add-on.
* **No Kubernetes dependencies.** It works the same on bare metal, Docker Compose and Helm deployments. In
  Kubernetes, point `storage` at S3 and make sure the worker pod has the PostgreSQL client tools.

## Not implemented

* Block-level volume snapshots (Velero/CSI). Use your platform's snapshot tooling if you need them.
* Backups of Redis (not needed: cache and queue only) and of Diode/Hydra or other Enterprise-only components.
