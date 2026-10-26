# Changelog

## 0.2.0 (unreleased)

### M2: backends and drills

* Storage drivers `gcs` (resumable uploads with `ifGenerationMatch=0`, MD5 level-1 checks, locked bucket retention
  required by probe), `sftp` (asyncssh, pinned host key, exclusive `.part` file and create-only rename) and `rclone`
  (binary pinned by SHA-256, referenced and optionally encrypted rclone.conf, spool then `copyto --immutable`);
  extras `[gcs]` and `[sftp]`
* Replicas: `ReplicateJob` copies each set to the policy's `replicas` after the backup, objects first and
  `_COMPLETE` last; server-side within a provider (S3 `UploadPartCopy`, GCS rewrite), streaming otherwise. Every
  copied object is read back against the manifest's SHA-384, and the copy job records the whole-object MD5 and the
  part MD5s for its part size, so level 1 compares real checksums on the replica too. Per-copy status, `partial`
  sets, retries by the scheduler (three attempts, an hour apart) and `nbbackup replicate`; migration 0004
* QA restore instances: `nbbackup qa run` (in-place load of production's set, `finish --qa` with `migrate --check`
  and an API smoke test, no post-restore backup), reports signed with the QA instance's own key below
  `<production instance>/reports/`, imported by production against `crypto.report_signers` (never its own key);
  a pass marks the set level 2, a failure turns health red. `restore load --in-place --qa` now finishes as a QA
  restore (before, an in-place load always ran a normal finish)
* Scheduled drills: `headroom` (`storage_bytes`, `max_used_percent`) lets `verify --drill` and slot plans measure
  the server instead of needing `--headroom-confirmed`

### Gaps from M1 closed

* The ownership check (schemas and extensions not owned by the NetBox role) also runs in `target probe` and daily
  in the scheduler, with a health condition
* Preflight `Locks` also aborts when other roles hold locks on the role's relations, and names them
* A `local` target on the PostgreSQL data directory's filesystem is refused (where the data directory is visible)
* `verify_schedule` re-runs level 1 on every usable set of the policy
* Downloads are recorded in storage (`<instance>/audit/`) before the URLs are handed out
* Crash tests (a kill after every backup upload and every load and finish step, then a rerun) and guard tests
  (drills and finalize refuse unmarked databases and other sessions; an in-place load stops when its safety set
  does not verify, and a kill mid-load is recoverable with rollback)

### M1

Rebuilt to the implementation spec (M1). Not backward compatible with 0.1.0: configuration, storage layout, models
and commands changed. Migration 0002 replaces the 0.1.0 tables.

* Static configuration: named targets and policies, `env:`/`file:` secret references, `instance_id`
* Storage drivers with one streaming, create-only interface and `probe`: local/NFS, S3 and compatibles, Azure Blob;
  third-party drivers through the `netbox_backup.backends` entry point
* GnuPG encryption to the recipients and the recovery key, and signed manifests (BSI algorithm profile; de-vs compliance mode opt-in);
  no unencrypted mode
* Set format: ULID set IDs, signed header plus encrypted body, `_COMPLETE` marker, per-object SHA-384 and locally
  computed backend checksums
* Streaming backups inside an exported snapshot, the dump checked row for row against the baseline, media checked
  against the database's file references, level-1 verification after every set
* Cron schedules per policy with time zones, GFS retention with a minimum age, prune only with a separate credential
* Restore workflow: `slot init`, `restore plan/load/finish/status/rollback/finalize/media`, in place behind a
  verified safety set, `verify --drill`, journals in storage, resurrected-identity report
* `manage.py nbbackup`, standalone `netbox-backup` CLI (list, fetch, verify, decrypt, restore-db, check)
* Read-only UI and REST API, dashboard widget, Prometheus metrics, example alert rules
* Operational rules from the M1 review: every job carries its own RQ timeout; advisory locks on a direct,
  keepalive connection with pooler detection (`direct_connection`) and a lock and `max_runtime` re-check before the
  manifest and `_COMPLETE`; overruns failed only once the lock is free (hung holders are terminated); skipped runs
  recorded as failed; SSE-KMS/SSE-C detected and checked size only, with prune holding a policy whose newest sets are
  unverified; a strict definition of an empty database for disaster recovery, and reruns reuse their safety set
* The config component is encrypted to the new `crypto.recovery_key` only; `decrypt --format env`; `finish` reports
  differing secrets; `keys generate --role` requires passphrases for recovery and signing keys; the signing key may
  not be a recipient; stricter signature and subkey profile checks
* Unpinned installs generate a random `instance_id` on the first backup (recorded in `instance.json`) and refuse to
  back up as a clone; `list --all-instances`; journals below the source instance
* Encrypted safety copies of media and scripts; pgBackRest reported apart from backup health; an `absent()` alert
  and creation-time `last_success` for policies without a set
* Supports NetBox 4.6 and 4.7, PostgreSQL 15+, Python 3.12+

## 0.1.0

Initial release.
