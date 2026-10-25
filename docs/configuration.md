# Configuration reference

Everything lives in `PLUGINS_CONFIG['netbox_backup']` in `configuration.py`. That includes targets, policies, keys,
secret references and restore slots. Nothing is configured in the UI, so a NetBox admin can't redirect, weaken or
decrypt backups, and a disaster-recovery host can read the same settings before any database exists. The
standalone `netbox-backup` CLI reads the same dict from a JSON file (or from `configuration.py`).

## Secrets

Secrets are always references, never literals:

| Reference | Reads |
|---|---|
| `env:NAME` | the environment variable `NAME` |
| `file:/path` | the file's contents, without the trailing newline (Docker/Kubernetes secret mounts) |

A literal value in a `credentials` block, or a secret-like option such as `secret_key` outside one, fails startup
checks. Only the reference string is stored in the database.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `instance_id` | generated on the first backup | UUID that names this install in every key and manifest. **Pin it** (`uuidgen`): QA restores, `restore-db` and disaster recovery on an empty database need it, and health warns until it is pinned. Unpinned, the first backup generates a random UUID, records it in the database and in a create-only `instance.json` with the hostname and `ALLOWED_HOSTS`; a later backup whose `ALLOWED_HOSTS` (or, with a wildcard, hostname) differ is refused as a clone |
| `direct_connection` | `{}` | `host`, `port`, `hostaddr`, `sslmode`, `sslrootcert` overriding `DATABASES['default']` for the plugin's own sessions (advisory locks, the exported snapshot, pg_dump, pg_restore). Set it when NetBox connects through PgBouncer or another transaction-mode pooler, e.g. `{'port': 5432}` on Azure Flexible Server where NetBox uses 6432. A pooled session is refused |
| `targets` | `{}` | Storage targets by name (below) |
| `policies` | one `default` policy when exactly one target exists | Backup policies by name (below) |
| `crypto` | | Keys (below). Without it no backup runs: there is no unencrypted mode |
| `databases` | `()` | The two databases of the restore slot pair, e.g. `['netbox_a', 'netbox_b']`. One is live, the other is the slot |
| `spool_dir` | `$TMPDIR/netbox-backup` | Local spool for restores. Use an ephemeral volume with room for about 3× the set, plus the size of the safety set for in-place restores. Everything kept there past a step (safety copies of media and scripts, the local copy of the safety dump) is ciphertext |
| `restore_jobs` | `4` | `pg_restore --jobs` |
| `rollback_days` | `2` | How long `restore finalize` waits before emptying the old database |
| `allow_download` | `False` | Enables `POST /api/plugins/backup/sets/{id}/download/` (superusers only, ciphertext only) |
| `verify_dump` | `True` | Feed the dump to `pg_restore` while it streams and compare its rows with the snapshot's counts |
| `pg_dump`, `pg_restore`, `psql` | on `PATH` | Client binaries; at least the server's major version |
| `pgbackrest` | `None` | Monitor an external pgBackRest stanza: `{'stanza': 'netbox', 'max_age': '2d'}` ([PostgreSQL DR](postgresql-dr.md)) |
| `extra_configuration_files` | `[]` | Further files for the config component (e.g. LDAP CA bundles) |

## Targets

Common options:

| Key | Default | Meaning |
|---|---|---|
| `backend` | required | `local`, `s3`, `azure_blob`, or a name registered through the `netbox_backup.backends` entry point |
| `prefix` | `''` | Key prefix; sets live at `<prefix>/<instance_id>/sets/<YYYY>/<MM>/<set_id>/` |
| `credentials` | `{}` | Secret references the worker uses |
| `prune_credentials` | `{}` | Secret references for the maintenance runner, the only credential allowed to delete |
| `retention_days` | `0` | The bucket's or container's default retention. `probe` checks that it is in place, in compliance mode, and locked where the provider supports locking |
| `quota_bytes` | `None` | Health warns at 70 % of it |
| `offsite` | `True` except for `local` | Counts toward "at least one locked off-site copy" |
| `retention` | the policy's | Override the policy's GFS rules for sets on this target |

### `local`: disk, NFS, SMB mount

| Key | Meaning |
|---|---|
| `path` | Root directory |
| `allow_prune` | `True` lets `nbbackup prune` delete here. Filesystems have no separate credential, so this flag stands in for one |

Each write goes to an exclusively created `.part` file. It is fsynced, hard-linked to its final name (which fails if
that name exists) and the directory is fsynced. Filesystems give no immutability; use a WORM filer for that.

### `s3`

| Key | Meaning |
|---|---|
| `bucket`, `region`, `endpoint_url` | Where; `endpoint_url` for MinIO, Ceph RGW, Wasabi, Hetzner, IONOS |
| `credentials` | `access_key` + `secret_key` (+ `session_token`), or `profile`; empty uses the default chain (IAM role, IRSA/web identity) |
| `checksum_mode` | `when_supported` (default) or `when_required` for stores that reject trailing checksums (`InvalidArgument`) |
| `sse`, `kms_key_id` | `AES256` or `aws:kms`. With SSE-KMS or SSE-C the ETag is not an MD5, so level-1 checks fall back to size only (detected from `ServerSideEncryption` on every object, and by `target probe`'s two-part upload) |
| `storage_class`, `addressing_style`, `verify`, `concurrency` | As in boto3; `concurrency` (default 2) bounds memory to part size × (concurrency + 1) |

Every request carries Content-MD5. The ETag is computed locally from the part digests and stored in the manifest,
so level 1 compares our value with the server's. Writes use `If-None-Match: *`. The bucket needs:

* versioning plus object lock with a **default retention in COMPLIANCE mode** of at least `retention_days`;
* a lifecycle rule `AbortIncompleteMultipartUpload` after **1 day**.

### `azure_blob`

| Key | Meaning |
|---|---|
| `account_url` or `account_name`, `container` | Where |
| `credentials` | `connection_string`, `account_key`, `sas_token`, or nothing for managed/workload identity (`client_id` selects a user-assigned identity) |
| `tier` | `Hot` (default). Cool and Cold charge early-deletion fees, which a rolling retention always incurs |

Blocks carry a transactional MD5 and are committed create-only. Block size follows the size hint, because the
SDK's default caps a blob near 195 GiB. Enable **version-level immutability** with a default policy on the
container and lock it. Hierarchical-namespace accounts don't support it. Azure reports no whole-blob checksum for
block uploads, so level 1 is size only and says so.

## Policies

| Key | Default | Meaning |
|---|---|---|
| `target` | the only target | Primary target |
| `replicas` | `()` | Further targets (copying arrives in M2) |
| `components` | all four | `database`, `media`, `scripts`, `config` |
| `schedule` | `daily` | Five-field cron, or `hourly` (`0 * * * *`), `daily` (`0 1 * * *`), `weekly` (`0 1 * * 0`) |
| `time_zone` | `TIME_ZONE` | Time zone of the schedule and of the GFS buckets |
| `retention` | below | GFS rules |
| `zstd_level` | `3` | pg_dump (`--compress=zstd:N`, pg_dump 16+) and tarball compression |
| `max_runtime` | `4h` | The RQ job timeout of the backup. After it, the job refuses to write the manifest and `_COMPLETE`; the scheduler marks the set failed once the policy's lock is free and terminates the lock sessions of a hung job. Incomplete sets are collected after max(24 h, the longest `max_runtime` + 1 h) |
| `enabled` | `True` | |

The scheduler runs every 5 minutes, so schedules resolve to 5-minute slots. A run missed while the worker was down
runs once on recovery, not once per missed slot. A slot whose previous run is still queued or running is recorded as
a failed set ("Skipped: ..."), so health and alerts see it.

### Retention

| Key | Default | Meaning |
|---|---|---|
| `keep_last` | 7 | Most recent N sets |
| `keep_daily` | 14 | Newest set per day for N days |
| `keep_weekly` | 8 | Newest set per ISO week |
| `keep_monthly` | 12 | Newest set per month |
| `keep_yearly` | 0 | Newest set per year |
| `min_age` | `24h` | Never prune younger than this |
| `keep_min_verified` | 1 | Once QA restores exist, always keep N sets with a passed QA restore |

GFS counts only verified sets (level 1 passed), and the newest verified set is never pruned. While a policy's newest
`keep_last` sets are all unverified, prune deletes none of that policy's sets and reports it as held. GFS decides which sets
*may* go; the target's default retention decides the *earliest* date they can. That is why an hourly policy should
write database-only sets to a short-retention target: otherwise each of 24 daily sets carries a 35-day lock and a
full media copy.

## Crypto

| Key | Meaning |
|---|---|
| `public_keys` | Reference to an armored file holding every pinned public key. Any other key in it is discarded |
| `recipients` | Full fingerprints of the encryption recipients of the database, media, scripts and the manifest body: usually the ops key |
| `recovery_key` | Fingerprint of the offline recovery key. Required. Every object is also encrypted to it, and the config component (it holds `SECRET_KEY`, `API_TOKEN_PEPPERS` and the database password) **only** to it, so a host with the ops key (QA, drills) never reads the secrets |
| `signing_key` | Fingerprint of the key that signs manifests. Must be neither a recipient nor the recovery key |
| `signing_secret_key`, `signing_passphrase` | The worker's signing key and its passphrase (references) |
| `identities`, `identity_passphrase` | Decryption keys. Mount them **only** for restores and QA restores |
| `trusted_signers` | Extra fingerprints accepted on verification (a rotated signing key, a QA instance's report key) |
| `compliance` | `None` (default) or `'de-vs'` |
| `gpg_binary` | `gpg` |

The algorithm profile always admits only BSI TR-02102-1 key types: ECDSA/ECDH on brainpoolP256r1, P384r1 or
P512r1, and RSA of 3072 bits or more. Every subkey is checked, including expired and revoked ones, and a key with any
subkey outside the profile is rejected. `nbbackup keys generate` creates brainpoolP384r1 keys. Encryption is AES-256
(checked in `BEGIN_ENCRYPTION` and `DECRYPTION_INFO`), signatures use SHA-384. A manifest signature counts only with
exactly one signature whose `GOODSIG` and `VALIDSIG` agree, by a pinned signer's primary key, ECDSA or RSA, hash
SHA-384; an expired or revoked signing key fails.

### Keys

```
nbbackup keys generate --role ops      --name "NetBox ops" --out /secure/ops
nbbackup keys generate --role signing  --name "NetBox signing" --passphrase-env NBB_SIGNING_PASSPHRASE --out /secure/signing
nbbackup keys generate --role recovery --name "NetBox recovery" --passphrase-env NBB_RECOVERY_PASSPHRASE --out /mnt/usb/recovery
```

The signing and recovery roles refuse to generate a key without a passphrase (or use a smartcard and import only
the public key). Generate the **recovery key offline**: on an air-gapped machine with a live system, write the
secret key and its passphrase to two separate offline media, and bring only the public key (`.asc`) to the
NetBox hosts. The recovery key is needed for the config component, that is, for rebuilding a host from nothing.

`compliance: 'de-vs'` additionally runs gpg with `--compliance=de-vs --require-compliance`. That needs a GnuPG and
libgcrypt build whose random number generator is de-vs compliant. Stock distribution packages (Ubuntu 24.04: GnuPG
2.4.4, libgcrypt 1.10.3) are not: they refuse every encrypt and sign in that mode with "RNG is not compliant". Use
GnuPG VS-Desktop or a libgcrypt built with a compliant RNG before turning it on.

Key rotation: new recipients apply to new sets. Keep old decryption keys in escrow until the last set encrypted to
them has expired.

## Environment-specific notes

* **netbox-docker:** add the plugin and gnupg to the image along with the newest `postgresql-client` from the
  PostgreSQL apt repository, and run a second worker container with `rqworker netbox_backup.netbox_backup`. Run
  restores with `docker compose run --rm --entrypoint "" netbox /opt/netbox/venv/bin/python manage.py nbbackup ...`
  so the entrypoint's automatic `migrate` never runs against the slot.
* **Helm:** a plugin worker deployment, secret mounts for the `file:` references, and the maintenance runner as a
  CronJob in its own namespace with the prune credential.
* **Shared PaaS PostgreSQL:** the NetBox role must own its database, create its extensions (e.g. `ltree`) itself,
  and own the slot database. No superuser or server-level rights are needed.
