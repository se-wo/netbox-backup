# CLI and REST API Reference

## CLI: `manage.py netbox_backup <action>`

| Action | Purpose |
|---|---|
| `create [--full] [--verify quick\|deep\|restore] [--no-prune]` | Take a backup now. |
| `list [--instance ID] [--instances] [--json]` | List backups **in storage** (authoritative), or the instance IDs present. |
| `show <backup> [--instance ID] [--json]` | Show a manifest. |
| `verify <backup> [--level quick\|deep\|restore]` | Verify a backup (default `deep`). Exits non-zero on failure. |
| `restore <backup> [options]` | Restore. See below. |
| `download <backup> <dir>` | Download, verify and decrypt the artifacts into `<dir>`. |
| `delete <backup> [--yes]` | Delete from storage and the catalog. |
| `prune [--dry-run]` | Apply the retention policy. |
| `sync` | Rebuild the catalog from storage. |
| `status [--json]` | Health conditions. Exit code **0** ok/info, **1** warning, **2** error. |
| `genkey` | Print a random encryption key. |

`<backup>` is a full name, a unique prefix, `latest`, `latest-full` or `latest-database`.

### `restore` options

| Option | Effect |
|---|---|
| `--dry-run` | Run preflight checks only. |
| `--yes` | Skip the confirmation prompt (required when not on a TTY). |
| `--components database,media,scripts,reports` | Restore a subset. |
| `--instance ID` | Restore another instance's backup (implies `--allow-foreign-instance`). |
| `--allow-foreign-instance` | Allow a backup whose instance ID differs from this installation's. |
| `--allow-newer` | Allow a backup made by newer code. **Not recommended.** |
| `--terminate-connections` | Kill other database sessions instead of refusing. |
| `--no-safety-backup` | Skip the pre-restore safety backup. |
| `--no-migrate` | Do not run migrations afterwards. |
| `--extract-config DIR` | Also write the backed-up configuration files to `DIR`. |
| `--jobs N` | Parallel `pg_restore` jobs. |

## REST API: `/api/plugins/backup/`

| Method & path | Permission | Description |
|---|---|---|
| `GET  backups/` | `view_backup` | List (filters: `kind`, `status`, `trigger`, `verification_status`, `encrypted`, `instance_id`, `started_at__after`, `started_at__before`, `q`) |
| `GET  backups/<id>/` | `view_backup` | Detail, including `manifest` and `verification_log` |
| `DELETE backups/<id>/` | `delete_backup` | Delete from catalog and storage |
| `POST backups/run/` | `run_backup` | `{"kind": "database"\|"full", "verify_level": null\|"quick"\|"deep"\|"restore"}` → `202` + Job |
| `POST backups/<id>/verify/` | `run_backup` + `view_backup` | `{"level": "deep"}` → `202` + Job |
| `POST backups/sync/` | `run_backup` | Reconcile with storage → counts |
| `GET  status/` | `view_backup` | `{"instance_id", "status", "conditions": [{"name", "status", "message"}]}` |
| `GET  restores/`, `GET restores/<id>/` | `view_restoreevent` | Restore history |

Write endpoints need a write-enabled token. Poll the returned job at `/api/core/jobs/<id>/`. On completion its
`data` holds `{"backup": name, "backup_id": id, ...}`.

Example: trigger a full backup with a test restore from CI before an upgrade.

```bash
curl -sX POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     https://netbox.example.com/api/plugins/backup/backups/run/ \
     -d '{"kind": "full", "verify_level": "restore"}'
```

## Health conditions

| Condition | ok | warning | error |
|---|---|---|---|
| StorageReachable | probe write/read/delete succeeded | | storage unusable |
| ScheduleConfigured | a schedule is enabled and its job is queued | no schedule, or no queued job (restart netbox-rq) | |
| BackupAvailable | newest successful backup is fresh | older than `stale_after` | none exists |
| FullBackupAvailable | a full backup exists | files are unprotected | |
| LastBackupSucceeded | last run succeeded | completed with warnings | last run failed |
| BackupVerified | newest backup verified (*info* if never) | | verification failed |
| RestoreTested | some backup passed a test restore | never test-restored | |
| Encryption | encrypted (*info* if not) | | |
| ConfigurationProtected | configuration included | `SECRET_KEY`/peppers not backed up | |
| StorageIsolated | no other instance in the location | other instance IDs present | |
| PointInTimeRecovery *(optional)* | pgBackRest healthy | no WAL / stale | stanza broken |
| RecoveryRequired *(after a restore)* | | | last restore failed or row counts mismatched |

## Prometheus / monitoring

`netbox_backup status --json` (exit code 0/1/2) works as a Nagios/Icinga check or a node-exporter textfile
collector. `GET /api/plugins/backup/status/` works for HTTP-based monitoring.
