# Changelog

## 0.2.0 (unreleased)

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
