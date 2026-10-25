# Enterprise parity

NetBox Enterprise has three backup layers: continuous pgBackRest backups in a cluster volume, Velero DR backups to
S3, and a manual kubectl procedure. This table maps every documented behaviour to this plugin.

| Enterprise behaviour (documented) | Enterprise mechanism | netbox-backup | Status |
|---|---|---|---|
| PostgreSQL backups on by default, full Sunday and incremental Monday–Saturday at 01:00, `retentionFull: 4` | Crunchy PGO + pgBackRest | A policy enabled at install: daily at 01:00, GFS retention | Done |
| Continuous WAL archiving; PITR by time, LSN or immediate | pgBackRest `archive_command` | Monitors an external pgBackRest stanza; configuration rendering and runbooks later | Monitoring done |
| DR backup of the whole install to S3, daily/weekly/cron, retention 1 month | Velero | Sets to any backend, cron schedule, GFS and minimum age | Done |
| A unique prefix per cluster, else clusters can't be told apart | Operator discipline | Mandatory `instance_id` in every key and manifest; restore shows the source instance | Done |
| Restore silently hides backups from other versions | Installer version filter | Every set is listed with a compatibility verdict and reason; older sets restore with migrations | Done |
| "Application restored!" doesn't prove data; compare a recorded Sites count | Manual | The manifest stores per-table counts, per-schema index/constraint/trigger counts and the newest `last_updated`; restore compares them automatically | Done |
| Live data dir excluded to avoid torn copies | Velero exclusion + pgBackRest rebuild | Never copies files from a data dir: `pg_dump` inside an exported snapshot | Done |
| hostPath volumes skipped while the backup reports Completed | Velero limitation | Components report bytes and file counts; referenced-but-missing media warns; empty-but-referenced storage fails the set | Done |
| Manual backup: media, scripts, reports, static; `pg_dumpall`; secrets; config with `--decrypt` | kubectl exec scripts | The same components, encrypted; the config component includes the secrets | Done |
| Manual restore: maintenance mode, secrets first, drop/recreate the DB with template0 UTF8, fix grants | Runbook | Restore into a marked second database and switch by config, or in place behind a verified safety set | Done |
| Repo pressure alert at 70 % of `repoStorageSize` | `PostgresBackupRepoPressure` | Capacity per target, alert at 70 % of `quota_bytes` | Done |
| WAL piles up without limit when archiving fails, while Ready stays True | PostgreSQL behaviour | Alerts on `pg_stat_archiver.failed_count` and archive lag | Later |
| Restore removes schedules, which return after recovery; the restore flag must be cleared | Operator behaviour | Backups of the live database continue during a slot load; finish resets job state and takes a full backup | Done |
| PITR leaves a timeline fork until the next full backup | pgBackRest error [058] | A full backup is enqueued right after any restore | Done |
| S3 stores rejecting `STREAMING-UNSIGNED-PAYLOAD-TRAILER` fail with InvalidArgument | Velero AWS plugin v1.14.2 | `checksum_mode: when_required` | Done |
| HA restore waits for 3 controller nodes | Embedded Cluster | Not applicable: a restore needs one database and one process | n/a |
| Air-gapped installs unsupported for DR | Embedded Cluster | Supported via local/NFS and SFTP targets | Done |

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M1 Core | Full sets; s3, azure_blob, local; GnuPG encryption and signing; signed manifest; GFS retention on bucket default retention; prune with a separate credential; restore in place or via marked slot and switch; level-1 integrity; metrics and alert rules; `status --json`; external check; standalone CLI | Implemented |
| M2 Backends and drills | gcs, sftp, rclone; replicas (ReplicateJob, server-side within a provider, recorded copy checksums); QA restore instance (`qa run`, `finish --qa` with `migrate --check` and an API smoke test, reports signed with the QA key and imported by production); scheduled drills measure server headroom | Implemented; exit criterion (monthly QA restore green on a reference install for 3 months) pending |
| M3 Browse | Load a set into the slot read-only, diff objects against live, export selected objects | Planned |
| Later | Incremental media; provider-clone load (`--from-dsn`); pgBackRest configuration for self-hosted servers | On demand |
