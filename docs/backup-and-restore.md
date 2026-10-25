# Backup and restore in depth

## Architecture

The plugin is the control plane and the RQ worker is the data plane. Web requests never move backup bytes, and the
database restore never runs inside a web process.

| Component | Runs in | Does |
|---|---|---|
| `netbox_backup` Django app | NetBox web and worker | Models, read-only UI and API, metrics collector |
| Scheduler system job | RQ worker, every 5 minutes | Mirrors static config, evaluates cron schedules, enqueues due backups and the daily prune |
| `BackupJob`, `VerifyJob`, `PruneJob` | queue `netbox_backup.netbox_backup` | Stream components to storage, level-1 checks, retention |
| Maintenance runner | separate CronJob or container with the prune credential | `manage.py nbbackup prune` |
| `manage.py nbbackup restore ...` | ops shell or one-shot container | Plan, load, finish after the switch |
| `netbox-backup` | any host with Python 3.12 | List, fetch, verify, decrypt and `restore-db` without Django; `check --max-age` for monitoring |

Design rules:

* **Storage is the source of truth.** `BackupSet` rows are a cache; `nbbackup catalog sync` rebuilds them from the
  signed manifest headers, and it runs after every restore.
* **Static config wins.** Targets, policies, recipients, the signing key and secret references live only in
  `PLUGINS_CONFIG` and work before the database exists.
* **One lock, one writer.** A PostgreSQL advisory lock per policy, on its own connection, prevents concurrent jobs.
  It is released the instant a worker dies. Each install writes only below its own `instance_id`, and
  `instance.json` is created with a create-only write.
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
encrypted to the ops and recovery keys like everything else. A set already contains every password hash and token,
so it must be protected like production either way.

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
<prefix>/<instance_id>/journal/<restore_id>/<NNN>-<step>.json
<prefix>/<instance_id>/.probe/<timestamp>-<id>
```

`set_id` is a ULID, so keys sort by time on every backend. A set becomes visible only when `_COMPLETE` is written,
after everything else, so no backend needs atomic multi-object writes.

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

## Crypto

The plugin contains no cryptographic code of its own. gpg runs as a subprocess with an isolated `GNUPGHOME` per job
holding only the pinned keys. It always gets `--batch` and `--status-fd`, never retrieves keys automatically, and
every result is checked from the status lines:

* encryption must report `END_ENCRYPTION` with AES-256, to both recipients;
* decryption must report `DECRYPTION_OKAY` with AES-256 and MDC or AEAD integrity. gpg emits plaintext before its
  check finishes, so output goes to a staging file that is used only after gpg exits successfully;
* a signature counts only if `VALIDSIG` names a pinned signer's primary key and SHA-384. This is equivalent to
  `--assert-signer`, which GnuPG releases before 2.4.5 lack.

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
| 1 Integrity | sizes and checksums against the backend, signature, `_COMPLETE` hash, lock status ("size only" where the backend reports no checksum) | the worker, no private key | after every backup; `nbbackup verify <set>`; the UI's Verify button |
| 2 QA restore | decrypt, hash check, full load into the slot, baseline check, migration plan | `nbbackup verify <set> --drill` in the restore slot | on demand (a separate QA instance follows in M2) |

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
   sessions. It moves the slot marker to the old database and takes a safety copy of the current media and scripts.
6. `finish` continues:
   * runs `migrate --no-input`;
   * marks restored pending, scheduled and running jobs as errored;
   * empties NetBox's RQ keys, and the cache when it is NetBox's alone (otherwise only the `config` keys);
   * deletes all sessions and runs `reindex --lazy` and `ANALYZE`;
   * writes the set's media and scripts, moving files the set doesn't have to `.nbb-quarantine/<restore_id>/`;
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
| Instance | the set belongs to another `instance_id` | requires `--from-instance <id>` |
| Peppers | restored v2 tokens reference pepper IDs missing from `API_TOKEN_PEPPERS` | warn: those tokens stop validating |

### In-place restore

This is the everyday path for application-level mistakes, such as a failed bulk edit, when no second database
exists. The second database is then needed only in a disaster.

* It requires NetBox to be stopped, no other sessions, and `--confirm <database name>`. An empty live database (a
  fresh DR host) needs neither confirmation nor a safety set.
* It takes a **safety set** of the live database and media, uploads it, and verifies the signature, hashes and level
  1 *before anything is deleted*. A local copy stays on the spool as a fast path until finalize. If the live database
  can't be dumped at all, `--skip-safety` proceeds without one and records that in the journal.
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
* incomplete sets (no `_COMPLETE`) older than a day are garbage once their lock expires;
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
  are off by default, superuser-only, presigned for at most five minutes, and ciphertext only.
* **Restore:** CLI only, which requires infrastructure access. Four-eyes approval is left to the organisation's
  change process. There is no restore permission in NetBox.
* **Subprocesses:** database passwords reach pg_dump and pg_restore through a 0600 `.pgpass` in a private tmpfs
  directory, never argv or the environment.
* **Startup:** health refuses known sample secrets such as netbox-docker's default `SECRET_KEY` and pepper, and a
  backup refuses to run with them.
