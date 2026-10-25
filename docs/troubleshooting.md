# Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| S3 upload fails with `InvalidArgument` | The store rejects trailing checksums | Set `checksum_mode: when_required` on the target |
| `pg_dump ... is older than the PostgreSQL server` | Client older than server | Install the newest `postgresql-client` from the PostgreSQL apt repository |
| `RNG is not compliant with --compliance=de-vs mode` | `compliance: 'de-vs'` with a stock GnuPG/libgcrypt | Remove `compliance` (the BSI algorithm profile still applies) or install a de-vs compliant GnuPG build |
| `Key ... does not meet the 'bsi' algorithm profile` | A pinned key uses Ed25519/Curve25519 or RSA < 3072 | Generate keys with `nbbackup keys generate` (brainpoolP384r1) |
| `Pinned key(s) not found in crypto.public_keys` | The key file lacks a recipient or the signing key | Rebuild the bundle: `cat */*.pub.asc > pubkeys.asc` |
| `No mounted identity matches a recipient` | Restore without the ops or recovery key mounted; or the config component, which only the recovery key opens | Mount the key and list it in `crypto.identities`; the config component needs the recovery key |
| `backend PID changed between transactions: a connection pooler` | NetBox's database connection goes through PgBouncer (e.g. Azure Flexible Server port 6432) | Set `direct_connection: {'port': 5432}` (or the direct host) in the plugin config |
| `max_runtime ... exceeded; not writing the manifest` / `no longer holds the lock` | The backup ran longer than its policy's `max_runtime`, or the scheduler reclaimed its lock | Raise `max_runtime`, or find what made the job slow; the incomplete set is collected later |
| `instance_id ... was recorded by another install` | An unpinned clone or QA copy restored from another install | Pin a new `instance_id` on this install |
| Level 1 says "size only" on S3 | The bucket uses SSE-KMS or SSE-C, whose ETag is not an MD5 | Expected; sizes, SHA-384 on read and signatures still apply. `target probe` reports it |
| Prune reports a policy as held | The policy's newest sets are all unverified | Fix the verification failure (`nbbackup verify`); prune resumes once a set verifies |
| `keys generate` refuses without `--passphrase-env` | Recovery and signing keys need a passphrase | Pass `--passphrase-env VAR` (or keep the key on a smartcard) |
| `crypto is not configured` | No `crypto` block | There is no unencrypted mode; configure keys |
| Media warning on every set | Files referenced in the database are missing from storage | Check `media_missing` in the set; restore those files from an older set (`restore media`) |
| `the database references N file(s) but the storage is empty` | The media volume is not mounted in the worker | Mount it, or point `STORAGES['default']` at the right place |
| `Dump check: the dump's rows differ` | The dump did not capture what the snapshot counted | Report it with the job log; set `verify_dump: false` only to get a set while investigating |
| Set listed as incompatible | Set from a newer NetBox, or a plugin missing | Upgrade NetBox or install the plugin version `restore plan` shows |
| `restore load` stops at the baseline check | Dump and counts disagree, or the load was incomplete | Re-run load; inspect the slot before it is emptied again |
| `restore plan` rejects the slot | Slot missing, unmarked or not owned by the NetBox role | `nbbackup slot init <db>`, or the platform team runs `ALTER DATABASE ... OWNER TO netbox` |
| `server headroom not confirmed` | Slot loads double NetBox's footprint on the server | Check free space (Azure Flexible Server turns read-only server-wide at 95 %), then pass `--headroom-confirmed` |
| `holds the rollback copy of restore ...` | The slot still holds the previous database | `nbbackup restore finalize` once the rollback window has passed |
| API tokens fail after restore | Pepper IDs missing from `API_TOKEN_PEPPERS` | Restore the original peppers (they are in the set's config component), or reissue tokens |
| Scheduling not active | No worker on the plugin queue | Run `manage.py rqworker netbox_backup.netbox_backup` |
| Probe: `lifecycle aborts incomplete uploads` fails | No `AbortIncompleteMultipartUpload` rule | Add one with 1 day to the bucket |
| Probe: `default retention (compliance mode)` fails | Object lock missing, governance mode, or shorter than `retention_days` | Enable object lock with a COMPLIANCE default retention of at least `retention_days` |
| Prune deletes nothing | The target has no prune credential, or the sets are still locked | Run `nbbackup prune` on the maintenance runner with `prune_credentials`; locked sets wait for their retention |
| Backup refuses: `carries the restore slot marker` | NetBox points at a slot that was never finished | Finish the restore, or switch back and run `restore rollback` |
