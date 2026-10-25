# Backup and restore in depth

## Architecture

The plugin is the control plane and the RQ worker is the data plane. Web requests never move backup bytes, and the
database restore never runs inside a web process.

| Component | Runs in | Does |
|---|---|---|
| `netbox_backup` Django app | NetBox web and worker | Models, read-only UI and API, metrics collector |
| Scheduler system job | RQ worker, every 5 minutes | Mirrors static config, evaluates cron schedules, enqueues due backups and the daily prune |
| `BackupJob`, `ReplicateJob`, `VerifyJob`, `PruneJob` | queue `netbox_backup.netbox_backup` | Stream components to storage, copy sets to replicas, level-1 checks, retention |
| Maintenance runner | separate CronJob or container with the prune credential | `manage.py nbbackup prune`, scheduled `verify --drill` |
| QA instance (optional) | a separate NetBox install with its own database, media, Redis and `instance_id` | `nbbackup qa run`: scheduled QA restores of production sets, signed reports |
| `manage.py nbbackup restore ...` | ops shell or one-shot container | Plan, load, finish after the switch |
| `netbox-backup` | any host with Python 3.12 | List, fetch, verify, decrypt and `restore-db` without Django; `check --max-age` for monitoring |

Design rules:

* **Storage is the source of truth.** `BackupSet` rows are a cache; `nbbackup catalog sync` rebuilds them from the
  signed manifest headers, and it runs after every restore.
* **Static config wins.** Targets, policies, recipients, the signing key and secret references live only in
  `PLUGINS_CONFIG` and work before the database exists.
* **One lock, one writer.** A PostgreSQL advisory lock per policy, on its own direct connection with TCP keepalives,
  prevents concurrent jobs. It is released the instant a worker dies. A session through a transaction-mode pooler
  (PgBouncer) is detected by a backend PID that changes between transactions and refused; set `direct_connection`.
  The job re-checks that it still holds the lock, and that it is within `max_runtime`, right before it writes the
  manifest and again before `_COMPLETE`. Each install writes only below its own `instance_id`, and `instance.json`
  is created with a create-only write.
* **Every job carries its own RQ timeout.** RQ's default of 300 s would kill any backup longer than five minutes:
  a backup gets its policy's `max_runtime`, prune the longest `max_runtime` (at least 1 h), verify 1 h.
* **No schema surgery.** The plugin dumps NetBox's database and loads dumps into an empty one. It never renames,
  alters or edits NetBox's tables, schemas or extensions.

## What a set contains

| Component | Source | Object |
|---|---|---|
| database | `pg_dump -Fc` (zstd with pg_dump 16+) of the NetBox database inside an exported snapshot, grants included | `db.pgdump.enc` |
| media | `STORAGES['default']`: image attachments and every other `FileField` | `media.tar.zst.enc` |
| scripts | `STORAGES['scripts']`: the files `ScriptModule` rows point to | `scripts.tar.zst.enc` |
| config | `configuration.py`, `ldap_config.py`, extra files, plugin list, pip freeze | `config.json.enc` |

The config component holds the configuration files **verbatim, including `SECRET_KEY`, `API_TOKEN_PEPPERS` and the
database password**. A replacement host can be rebuilt from a set and the recovery key alone. The object is
encrypted to the **recovery key only**: hosts with the ops key (QA restores, drills, `verify --deep`) restore the
database, media and scripts and skip it. `netbox-backup decrypt <set> --components config --format env` (with the
recovery key) also writes the secrets as a 0600 `secrets.env` (`SECRET_KEY`, `API_TOKEN_PEPPER_<id>`,
`DB_PASSWORD`) for the rebuilt host, and `restore finish` reports, by name only, which secrets differ from the
running configuration where the recovery key is mounted. A set already contains every password hash and token, so
it must be protected like production either way.

Data-source content lives in the database and is covered by the dump. Server-wide objects such as roles are never
dumped, because a shared server's roles are not NetBox's to restore. `static` is not backed up, because
`collectstatic` regenerates it. Tarballs store uid/gid 0 and no owner names.

**Media is checked against the dump.** The snapshot records every file path the database references. Media is
read after the snapshot, so every referenced file already exists. A missing file is looked up once more, then
listed as `media_missing`, and the set carries a warning that health reports. If the database references files but
the storage is empty, the set fails: the volume is probably not mounted.

### Layout

```
<prefix>/<instance_id>/instance.json
<prefix>/<instance_id>/sets/<YYYY>/<MM>/<set_id>/manifest.json
<prefix>/<instance_id>/sets/<YYYY>/<MM>/<set_id>/db.pgdump.enc
<prefix>/<instance_id>/sets/<YYYY>/<MM>/<set_id>/media.tar.zst.enc
<prefix>/<instance_id>/sets/<YYYY>/<MM>/<set_id>/scripts.tar.zst.enc
<prefix>/<instance_id>/sets/<YYYY>/<MM>/<set_id>/config.json.enc
<prefix>/<instance_id>/sets/<YYYY>/<MM>/<set_id>/_COMPLETE
<prefix>/<source instance_id>/journal/<restore_id>/<NNN>-<step>.json
<prefix>/<instance_id>/reports/<report_id>-<set_id>.json      # signed QA restore reports
<prefix>/<instance_id>/audit/<event_id>-download.json          # one per download
<prefix>/<instance_id>/.probe/<timestamp>-<id>
```

`set_id` is a ULID, so keys sort by time on every backend. A set becomes visible only when `_COMPLETE` is written,
after everything else, so no backend needs atomic multi-object writes. A signed manifest with a matching
`_COMPLETE` is authoritative: catalog sync adopts it even if the database recorded that set as failed. Journals live
below the instance the restored set came from; each step is a new create-only object with the next free `NNN`.

### Manifest

`manifest.json` has a **signed plaintext header** and an **encrypted body**.

* Header: set ID, instance ID, policy, trigger, times, NetBox and plugin version, recipient and signer
  fingerprints. Per object it records the key, the size, the ciphertext SHA-384 and the backend checksum computed
  locally while streaming. It also holds the SHA-384 of the encrypted body, so the signature covers the body too.
  Listing and integrity checks need only the header.
* Body: versions (NetBox, plugins, PostgreSQL server and pg_dump), the `django_migrations` state, creator,
  hostname, schemas and grantee roles, token pepper IDs, and the **baseline**. The baseline is the row count of
  every table plus per-schema counts of indexes, constraints and triggers, taken by raw SQL inside the dump's
  snapshot.
* `_COMPLETE` holds the SHA-384 of `manifest.json`, so a replaced manifest is detected.

## The backup pipeline

1. Take the policy's advisory lock. Refuse to run against a database carrying the slot marker: a half-switched
   worker must not produce sets of stale data.
2. Open a `REPEATABLE READ READ ONLY` transaction and export its snapshot. Record versions, migrations, the baseline
   and referenced files in it, and keep it open until `pg_dump --snapshot` exits.
3. Stream each component through `gpg --encrypt` to the primary target with create-only writes, computing SHA-384
   and the backend checksum on the way. pg_dump runs through a pipe and tarballs come from one writer thread, so
   nothing is staged on local disk.
4. **Dump check:** the plaintext dump is also fed to `pg_restore --data-only`, and its rows are counted per table.
   They must equal the baseline. This proves the dump contains the data, not only its table of contents.
5. An object is committed only if every stage exited 0 and gpg reported `END_ENCRYPTION` with AES-256. Otherwise the
   upload is aborted, so a truncated dump never becomes a valid object.
6. Write the manifest, then `_COMPLETE`. On immutable targets the default retention locks each object as it is
   written; the worker never sets or extends locks.
7. **Level 1:** compare each object's size and checksum with what the backend reports, and check the signature
   and the lock status. The set is `verified` only if all of it passes.
8. Enqueue `ReplicateJob` when the policy has replicas, then a prune.

## Replicas

A policy writes to one primary target; `ReplicateJob` then copies each set to every target in its `replicas`:
the objects first, then `manifest.json`, then `_COMPLETE`, so a half-copied set is invisible on the replica just as
on the primary. Within one provider the copy is **server-side** (S3 to S3 on the same endpoint with `UploadPartCopy`,
GCS to GCS with a rewrite); across providers, or when the replica's credential cannot read the source, the objects
**stream** through the worker. Writes on the replica are create-only like every other write.

Every copied object is read once and its SHA-384 checked against the signed manifest before the copy counts: a
streamed object before it is committed, a server-side copy right after it was made. That read also yields what
level 1 compares on the replica. A server-side copy has its own part layout, so its S3 ETag is not the primary's:
the copy job computes the whole-object MD5 and the part MD5s for the part size it used, and records them (with the
part size) in the copy's catalog row. Replica copies are then verified at level 1 like the primary, after every copy
and on each `verify_schedule`.

Each copy has its own status in the copy matrix. While a replica copy failed, the set is **partial** and health
warns; the scheduler retries a failed copy an hour after the last attempt, up to three attempts, and
`nbbackup replicate <set>` retries by hand. The primary does not prune a set until every replica holds it. A
replica target applies its own retention (a `retention` block on the target) and default lock.

## Crypto

The plugin contains no cryptographic code of its own. gpg runs as a subprocess with an isolated `GNUPGHOME` per job
holding only the pinned keys. It always gets `--batch` and `--status-fd`, never retrieves keys automatically, and
every result is checked from the status lines:

* encryption must report `BEGIN_ENCRYPTION` and `END_ENCRYPTION` with AES-256 (cipher 9), to the configured
  recipients plus the recovery key (the config component: the recovery key only);
* decryption must report `DECRYPTION_OKAY` with AES-256 and MDC or AEAD integrity. gpg emits plaintext before its
  check finishes, so output goes to a staging file that is used only after gpg exits successfully;
* a signature counts only if there is exactly one (`NEWSIG`), its `GOODSIG` and `VALIDSIG` name the same key, and
  `VALIDSIG` names a pinned signer's primary key, ECDSA or RSA, and SHA-384. Expired or revoked signing keys fail.
  This is stricter than `--assert-signer`, which GnuPG releases before 2.4.5 lack;
* every pinned key, including each subkey, must meet the algorithm profile, or the key is rejected.

Restores verify the manifest signature and each object's ciphertext SHA-384 *before* decrypting anything.

The always-on worker holds only the public encryption keys plus the signing key. A compromised worker could sign
forged future sets but cannot read or alter locked past ones. After a worker compromise, restore only sets created
before the incident.

Post-quantum protection is not provided, and that is an accepted risk: sets are short-lived and nobody expects to
restore one that is years old. TR-02102-1 recommends classical key agreement until the end of 2031. GnuPG 2.5
already ships Kyber, so a later move means new keys, not new code.

## Verification levels

| Level | What runs | Where | When |
|---|---|---|---|
| 1 Integrity | sizes and checksums against the backend, signature, `_COMPLETE` hash, lock status ("size only" where the backend reports no usable checksum: Azure, SSE-KMS, SSE-C) | the worker, no private key | after every backup; `nbbackup verify <set>`; the UI's Verify button |
| 2 QA restore | decrypt, hash check, full restore, baseline check, `migrate --check`, API smoke test | a separate QA instance (`nbbackup qa run`), signed report | monthly, after every production NetBox or plugin upgrade, and quarterly with the offline recovery key only |
| 2 Drill | decrypt, hash check, full load into the slot, baseline check, migration plan, archive listings | `nbbackup verify <set> --drill` in the restore slot | on demand, or scheduled on the maintenance runner |

A set is trusted only at the level it was last verified, and the UI shows that level. `verify_schedule` on a policy
re-runs level 1 on all of its sets, so an object deleted, replaced or unlocked behind the plugin's back shows up.

### QA restore instance

A QA instance is a separate NetBox install, e.g. `netbox-qa`, with its own database, media storage and Redis, on the
same or a newer NetBox version (which makes it an upgrade rehearsal), and **its own pinned `instance_id`**, so it
never writes below production's path and can't make dead production backups look healthy. A CronJob on QA runs

```
manage.py nbbackup qa run --from-instance <production instance_id> --confirm <QA database>
```

That is `restore load --in-place` of production's newest set (without a safety set: QA's data is disposable),
followed by `restore finish --qa`: the same finish as everywhere, then `migrate --check` and an API smoke test, and
no post-restore backup. The result, passed or failed, becomes a report signed with **QA's own key** (QA's
`crypto.signing_key`; production's signing key goes in QA's `trusted_signers`, so QA can verify production's
manifests). It is written create-only to `<prefix>/<production instance_id>/reports/` with a credential that can
write only there (`qa.report_target`).

Production lists QA's key in `crypto.report_signers` and imports the reports hourly and on every catalog sync
(`nbbackup qa import` by hand). Only reports by a report signer count, and never one signed by production's own key:
a compromised production worker could sign forged sets, but cannot vouch for them. A passed report marks the set
level 2 (`qa_passed_at`, and `recovery_drill_at` when the recovery key alone decrypted it), feeds `keep_min_verified`
and the level-2 metric; a failed report turns health red until a later QA restore passes.

The restored data is used as it is: event rules, webhooks and data sources come back exactly as in production, so
QA must have no network path to production systems, and it holds a full copy of production data plus the ops key.
Protect it like production. Mount the ops identity only into the QA restore job.

### Scheduled drills

Without a QA instance, `nbbackup verify --drill` runs the same check on demand in the restore slot of production's
own server. Scheduled from the maintenance runner, a drill must not pass `--headroom-confirmed`: configure
`headroom` instead ([configuration](configuration.md#top-level)), and every drill measures the server first. It
loads the slot only if the server's databases plus twice the live database stay below `max_used_percent` of
`storage_bytes`, because Azure Flexible Server turns read-only server-wide at 95 % used.


## Restores

A restore loads the set into a **second, empty NetBox database** (the restore slot), and NetBox is then switched to
it by configuration. The live database is never modified, so a failed restore costs nothing, and rollback means
switching back. This is NetBox's own documented procedure (an empty database, load the dump) with a safety net.

### One-time setup (platform team)

1. Create two databases on the shared server, both owned by the NetBox role, e.g. `netbox_a` and `netbox_b`.
2. Allow-list the extensions NetBox needs (e.g. `ltree`) and let the NetBox role create them itself so that it owns
   them. The ownership check also runs in `slot init` and in every plan.
3. Declare both: `'databases': ['netbox_a', 'netbox_b']`. Then run `nbbackup slot init netbox_b` once. It refuses a
   database that isn't empty and marks it with `COMMENT ON DATABASE netbox_b IS 'nbb:slot'`. The plugin only ever
   empties a database carrying that marker, or the live database under `--in-place --confirm`.

### Steps

Assumption: the operator enables maintenance mode before load, and nothing important runs during a restore. Writes
made between load and the switch are lost by design; plan and load warn when maintenance mode is off.

"NetBox stopped" means web servers, RQ workers and housekeeping are scaled to zero. The restore runs as a separate
one-shot process from the same image and config. The restore commands skip Django system checks, the plugin's
`ready()` never touches the database, and the session check ignores the command's own connections.

1. `nbbackup restore plan <set>` runs the preflight only. It recommends a strategy and says why, and prints the size,
   compatibility, and how much data written since the set will be lost.
2. `nbbackup restore load <set> --slot` recomputes the recommendation and refuses if the flag differs from it, or if
   the slot lacks the marker or has other sessions. It downloads every object to the spool while hashing, then
   decrypts. Then it empties the slot and runs `pg_restore --no-owner --exit-on-error -j 4`; any non-zero exit fails
   the load. NetBox keeps running on the live database meanwhile.
3. The baseline is compared: per-table row counts and per-schema counts of indexes, constraints and triggers. A
   mismatch stops here, with the live database untouched.
4. The operator switches NetBox: stop web and all workers, point `DATABASES['default']['NAME']` at the slot
   (`DB_NAME` in netbox-docker, or the Helm values), and start one ops container.
5. `nbbackup restore finish` checks that it is connected to the restored database and that the old one has no
   sessions. It moves the slot marker to the old database and takes a safety copy of the current media and scripts,
   encrypted like a set (`media-before.tar.zst.enc`); decrypted archives are deleted as soon as they are written.
6. `finish` continues:
   * runs `migrate --no-input`;
   * marks restored pending, scheduled and running jobs as errored;
   * empties NetBox's RQ keys, and the cache when it is NetBox's alone (otherwise only the `config` keys);
   * deletes all sessions and runs `reindex --lazy` and `ANALYZE`;
   * writes the set's media and scripts through `STORAGES['default']` and `STORAGES['scripts']`, on every restore
     path, moving files the set doesn't have to `.nbb-quarantine/<restore_id>/`;
   * reports resurrected users and tokens, syncs the catalog, writes the journal and enqueues a full backup.
7. Start web and workers.

Every step can be re-run, and load always empties the slot first. The journal is written to a local file and, after
each step, to storage, so `nbbackup restore status` shows where a run stopped, even from a fresh container. `load`
reads storage directly and never touches the catalog, so it also works on a fresh, unmigrated install.

### Preflight

| Check | Fails when | Behaviour |
|---|---|---|
| Signature and hashes | the signature or a size doesn't match (SHA-384 is checked while fetching) | abort |
| Key | no mounted identity matches a recipient | abort, naming the recipient fingerprints |
| Strategy | load names a different strategy than plan recommends | abort; rerun plan or pass the matching flag |
| NetBox version | the set is newer than the running NetBox, or from 4.7.0, whose dumps lose ltree triggers | abort; older sets migrate forward in finish |
| Plugins | a plugin with migrations in the set is not installed | abort, listing plugins and versions |
| PostgreSQL | the server major is older than the set's, or pg_restore is older than the pg_dump that wrote it | abort |
| Slot | missing marker, other sessions, not owned by the NetBox role, or an unfinalized rollback copy | abort, printing the command needed |
| Ownership | an archive schema exists in the target but isn't the role's, or a foreign-owned extension would survive emptying | abort before anything is deleted |
| Locks | the relations involved exceed half the server's lock capacity | abort |
| Redis | tasks and cache share one Redis database | warn; only RQ keys and the config cache keys are deleted |
| Space | the local spool is too small, or server headroom is not confirmed (`--headroom-confirmed`) | abort; Azure Flexible Server turns read-only server-wide at 95 % used |
| Instance | the set belongs to another `instance_id`, or this install's `instance_id` is not pinned | requires `--from-instance <id>` (`nbbackup list --all-instances`); QA restores and `restore-db` need a pinned ID |
| Grantees | roles the dump grants to don't exist on this server | warn; loaded without privileges, the dropped GRANT/REVOKE/ALTER DEFAULT PRIVILEGES lines are kept in the journal (`dropped_grants`) |
| Peppers | restored v2 tokens reference pepper IDs missing from `API_TOKEN_PEPPERS` | warn: those tokens stop validating |

### In-place restore

This is the everyday path for application-level mistakes, such as a failed bulk edit, when no second database
exists. The second database is then needed only in a disaster.

* It requires NetBox to be stopped, no other sessions, and `--confirm <database name>`. An **empty** live database
  (a fresh DR host) needs neither confirmation nor a safety set. Empty means all of: no relations in any non-system
  schema, no extension but plpgsql, no slot marker, no unfinished restore journal for it, and the other database of
  the configured slot pair empty too. Otherwise the error lists why and the newest change time in both databases.
* A rerun after a crashed load (an unfinished journal for this database) reuses that journal's safety set and marks
  the old journal `superseded`. It never takes a new safety set of half a load.
* It takes a **safety set** of the live database and media, uploads it, and verifies the signature, hashes and level
  1 *before anything is deleted*. A local copy of the (encrypted) safety dump stays on the spool as a fast path until
  finalize; the spool needs about 3× the set plus the safety set. If the live database can't be dumped at all,
  `--skip-safety` proceeds without one and records that in the journal.
* It empties the live database in one transaction with a lock timeout, loads the set, compares the baseline, then
  runs `finish`.
* If any step fails, or later on request, `restore rollback --confirm <database>` reloads the safety set: from the
  local copy if it is still there, otherwise from storage.

The trade-off is that NetBox is down for the whole load, and rollback is a second restore rather than a config switch.

### Rollback, finalize, media

* **Slot rollback:** point `DATABASES['default']['NAME']` back and run `nbbackup restore rollback`. It moves the slot
  marker back and restores media from the copy that finish took.
* `nbbackup restore finalize` empties the old database after `rollback_days` (`--force` to do it sooner). It too acts
  only on a marked database without sessions.
* `nbbackup restore media <set>` restores media alone against the live database, with the same safety copy first.
* Restoring revives users, sessions and tokens that existed at backup time. `finish` deletes all sessions and
  reports every user and token that is active again although it was deleted or disabled before the restore, using a
  snapshot that load writes (encrypted) to the journal. With `--revoke-resurrected` it disables them as well.

### The DR kit

Keep these together, off the infrastructure they protect, and check them at every recovery-key drill:

* read credentials for the storage targets (and the bucket, prefix and region or account URL);
* the armored public keys and their fingerprints (ops, recovery, signing, trusted signers);
* the recovery secret key and, separately, its passphrase;
* the pinned `instance_id`;
* the **direct** (not pooled) database DSN for `restore-db`, e.g. port 5432 rather than PgBouncer's 6432;
* a NetBox image of the same version as the sets, with this plugin, gnupg and `postgresql-client` installed.

Drills record which key decrypted each component; a drill in which the recovery key decrypted everything counts as
a recovery-key drill and is tracked separately (`recovery_drill_at`).

### When NetBox itself cannot start

`netbox-backup` (no Django) reads the same configuration from a JSON file:

```bash
netbox-backup --config /etc/netbox-backup.json list
netbox-backup --config /etc/netbox-backup.json verify --deep latest
netbox-backup --config /etc/netbox-backup.json restore-db latest --dsn postgresql://netbox@db/netbox_b
```

`restore-db` empties the target database (a marked slot, or `--confirm <database>`), decrypts with gpg, runs
`pg_restore --exit-on-error` and checks the baseline. It leaves a journal in storage: start NetBox against that
database and run `manage.py nbbackup restore finish` as usual. When the target is the marked slot of the configured
`databases` pair, the marker stays until `finish` moves it to the old database, so rollback and finalize work as
after a normal slot restore.

## Retention and pruning

Retention is GFS by count plus a minimum age ([configuration](configuration.md#retention)). Pruning runs after each
backup and daily. It deletes only where the target's `prune_credentials` are configured (or `allow_prune` on a local
target), which should be the maintenance runner alone; the worker, without that credential, only *reports* what is
due:

* `_COMPLETE` goes first, so a half-deleted set is invisible rather than corrupt;
* deletion is by version where the target is versioned;
* objects under an active lock are skipped and retried at the next prune;
* the primary target does not prune a set until all its required replicas hold it;
* incomplete sets (no `_COMPLETE`) older than max(24 h, the longest `max_runtime` + 1 h) are garbage once their
  lock expires;
* while a policy's newest `keep_last` sets are all unverified (e.g. after a bucket switched to SSE-KMS and level 1
  failed), none of its sets is deleted and prune reports the policy as held;
* probe objects (`target probe` writes them below `<instance_id>/.probe/`) older than a day go once their lock
  expires;
* `--dry-run` shows the diff first, which matters after a retention change, since it applies to all sets.

## Security

* **Threat model:** ransomware with NetBox host access, a leaked storage credential, a malicious insider with NetBox
  admin rights.
* **Separation:** the worker can write sets but can neither read, delete nor re-lock them. Prune runs on the
  maintenance runner with its own credential.
* **Immutability:** bucket or container default retention in compliance mode. Health is green only if a verified,
  locked off-site copy exists.
* **Insider:** all configuration is static, so a NetBox admin can't redirect, weaken or decrypt backups. Downloads
  are off by default, superuser-only, presigned for at most five minutes, and ciphertext only. Every download is
  recorded in storage (`<instance_id>/audit/`) before the URLs are handed out; without that record there is no
  download.
* **Restore:** CLI only, which requires infrastructure access. Four-eyes approval is left to the organisation's
  change process. There is no restore permission in NetBox.
* **Subprocesses:** database passwords reach pg_dump and pg_restore through a 0600 `.pgpass` in a private tmpfs
  directory, never argv or the environment. The rclone binary runs only with its pinned SHA-256; SFTP connects only
  to the pinned host key.
* **Ownership:** a schema or extension in the live database or the slot that the NetBox role doesn't own makes
  every restore abort at preflight. `slot init`, `target probe` and a daily scheduler run check for it, and health
  turns red long before a restore needs it.
* **Startup:** health refuses known sample secrets such as netbox-docker's default `SECRET_KEY` and pepper, and a
  backup refuses to run with them.
