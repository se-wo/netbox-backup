# Changelog

## 0.2.0 (unreleased)

Rebuilt to the implementation spec (M1). Not backward compatible with 0.1.0: configuration, storage layout, models
and commands changed. Migration 0002 replaces the 0.1.0 tables.

* Static configuration: named targets and policies, `env:`/`file:` secret references, pinned `instance_id`
* Storage drivers with one streaming, create-only interface and `probe`: local/NFS, S3 and compatibles, Azure Blob;
  third-party drivers through the `netbox_backup.backends` entry point
* GnuPG encryption to two recipients and signed manifests (BSI algorithm profile; de-vs compliance mode opt-in);
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
* Supports NetBox 4.6 and 4.7, PostgreSQL 15+, Python 3.12+

## 0.1.0

Initial release.
