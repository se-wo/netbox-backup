# Changelog

## 0.1.0

Initial release.

* Database and full (database + media + scripts + reports + configuration) backups
* Local/NFS and S3-compatible storage, per-instance namespacing
* Scheduled backups and housekeeping as NetBox system jobs; retention by age with `keep_last`
* Client-side AES-256-GCM encryption with key fingerprints
* Snapshot-consistent row counts; pre-upload, quick, deep and test-restore verification
* CLI restore with preflight checks, safety backups and row-count verification
* Health conditions in the UI, REST API and CLI; optional pgBackRest monitoring
