# CLI, REST API and metrics reference

## `manage.py nbbackup`

| Command | Does |
|---|---|
| `run [<policy>] [--components ...] [--background]` | A backup now; `--background` enqueues it on the plugin queue |
| `pre-upgrade [--policy]` | A manual set tagged for an upgrade |
| `list [--target] [--instance] [--all-instances] [--json]` | Sets in storage (no catalog needed); `--all-instances` lists every instance on the target |
| `show [<set>] [--json]` | Signed header, plus the decrypted body summary when an identity is mounted |
| `verify [<set>]` | Level-1 check of every copy |
| `verify [<set>] --drill [--headroom-confirmed]` | Level 2 on demand: load into the restore slot, check the baseline and migration plan, empty the slot. With `headroom` configured the server is measured instead of `--headroom-confirmed` |
| `replicate [<set>] [--target ...]` | Copy a set to its policy's replicas (or the named targets) and verify the copies; retries a failed copy |
| `prune [--dry-run] [--target ...]` | Apply retention; deletes only with a prune credential. Reports held policies (newest sets unverified) and expired probe objects |
| `slot init <database>` | Mark an empty database owned by the NetBox role as the restore slot |
| `restore plan [<set>] [--from-instance] [--components] [--json]` | Preflight; recommends `--slot` or `--in-place` |
| `restore load [<set>] --slot \| --in-place --confirm <db> [--skip-safety] [--revoke-resurrected] [--qa]` | Load the set; `--qa` (QA instances, in place) finishes with `finish --qa` right away |
| `restore finish [<restore>] [--qa] [--revoke-resurrected]` | After the switch: migrate, reset runtime state, write media. `--qa` adds `migrate --check` and an API smoke test and takes no post-restore backup |
| `restore status [<restore>] [--json]` | Journals, local and from storage |
| `restore rollback [<restore>] [--confirm <db>]` | Switch back (slot) or reload the safety set (in place) |
| `restore finalize [<restore>] [--force]` | Empty the old database after `rollback_days` |
| `restore media [<set>] [--components media scripts]` | Media alone, against the live database |
| `qa run [<set>] --from-instance <prod id> --confirm <QA db> [--target]` | On a QA instance: load production's set in place, `finish --qa`, write a report signed with the QA key below the production instance, passed or failed |
| `qa import` | On production: import the QA reports signed by `crypto.report_signers` (also hourly and on every catalog sync) |
| `catalog sync` | Rebuild sets and copies from storage, then import QA reports |
| `target probe [<name>]` | Pooled-session check and ownership check on the database, then per target: round trip, create-only check, a two-part upload with the target's encryption settings (ETag vs local MD5), default retention, lifecycle rule; for `local`, that the path is not on the database volume |
| `keys generate --role ops\|recovery\|signing --name <uid> --out <dir> [--passphrase-env VAR]` | A brainpoolP384r1 key pair, written as `<fingerprint>.pub.asc` / `.sec.asc`; recovery and signing keys require `--passphrase-env` |
| `status [--json] [--no-workers]` | Health; exits 0 ok, 1 warning, 2 error. pgBackRest is reported separately and doesn't affect the exit code |

`<set>` is a ULID, a unique ULID prefix, or `latest` (the default).

## `netbox-backup` (standalone, no Django)

| Command | Does |
|---|---|
| `list [--all-instances] [--json]` | Sets per target |
| `verify [<set>] [--deep]` | Level 1; `--deep` downloads every object, checks SHA-384 and decrypts when an identity is configured (the config component is skipped without the recovery key) |
| `fetch [<set>] --out <dir>` | Ciphertext and manifest, hash-checked |
| `decrypt [<set>] --out <dir> [--components ...] [--format files\|env]` | Verified plaintext components plus the manifest body. Without `--components`, config is included only where the recovery key is mounted. `--format env` also writes `secrets.env` (0600) |
| `restore-db [<set>] --dsn <url> [--confirm <db>]` | Load the database component into a marked slot or a confirmed database, then check the baseline |
| `check --max-age 26h` | Dead-man's check against storage: exit 0 ok, 2 too old or unreachable |

Global options are `--config` (a JSON file with the plugin dict, or a `configuration.py`; default
`$NETBOX_BACKUP_CONFIG` or `/etc/netbox-backup.json`), `--target` and `--instance`. Every command but `list`
needs a pinned `instance_id` in that file or `--instance`. `restore-db` reads the password
from the DSN or `PGPASSWORD`.

## REST API (`/api/plugins/backup/`)

| Endpoint | Methods | Notes |
|---|---|---|
| `targets/`, `policies/` | GET | Read-only mirrors of static config |
| `policies/{id}/run/` | POST | Manual backup; needs `netbox_backup.run_backuppolicy`. Returns 202 with the job |
| `sets/` | GET | Filter by `policy_id`, `status`, `trigger`, `verify_level`, `target_id`, `started__after/before` |
| `sets/{id}/download/` | POST | Off unless `allow_download`; superusers only; presigned URLs valid five minutes or less; ciphertext only; recorded in `<instance_id>/audit/` first, refused if that fails |
| `restores/` | GET | Plans and journals |
| `status/` | GET | One JSON health summary for monitoring |

## Permissions

Standard view permissions per model, plus two custom actions registered through NetBox's model-action API:
`run_backuppolicy` (manual backups) and `verify_backupset` (level-1 checks). There is no restore permission, because
restores need infrastructure access rather than a NetBox login. Downloads are superuser-only: a set contains every
password hash and token.

## Events

Each `BackupJob` uses its `BackupPolicy` as the job's object and stores the set status in the job data
(`set_id`, `status`, `size_bytes`, `warnings`, `error`). NetBox raises "Job completed" for every terminal job
status, so an event rule on that event for *Backup policy*, with a condition on `data.status`, can send webhooks
or notifications.

## Metrics

With `METRICS_ENABLED`, these gauges are computed from the catalog at scrape time (worker processes can't publish
to the web process's `/metrics`):

| Metric | Labels |
|---|---|
| `netbox_backup_last_success_timestamp_seconds` | policy, target (until a policy has a verified set: its creation time, so a first backup that never succeeds still alerts) |
| `netbox_backup_last_verified_timestamp_seconds` | policy, level |
| `netbox_backup_scheduler_last_tick_timestamp_seconds` | |
| `netbox_backup_enabled_policies` | |
| `netbox_backup_duration_seconds` | policy |
| `netbox_backup_size_bytes` | policy, component |
| `netbox_backup_media_missing` | policy |
| `netbox_backup_locked_offsite_copies` | policy |
| `netbox_backup_target_up` | target |

[`contrib/prometheus-alerts.yml`](../contrib/prometheus-alerts.yml) has example rules:
* no success within interval + max_runtime + 1 h;
* the gauges absent altogether (`absent()`: metrics disabled, the plugin not loaded, the scrape failing);
* no scheduler run for 15 minutes;
* a drop in enabled policies;
* no locked off-site copy;
* missing media;
* a failed probe;
* no QA restore in 35 days.

Add `netbox-backup check --max-age 26h` to external monitoring; it catches a NetBox that is down entirely, and fails
on an empty prefix (a first backup that never arrived).
