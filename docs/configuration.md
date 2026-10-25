# Configuration

All settings live under `PLUGINS_CONFIG['netbox_backup']` in `configuration.py`. Every key is optional.

| Setting | Default | Description |
|---|---|---|
| `storage` | `{'backend': 'local', 'path': '<NetBox root>/backups'}` | Where backups go. See below. |
| `instance_id` | derived from `SECRET_KEY` | Identifies this installation inside the storage location. Letters, digits, `.`, `_`, `-`. |
| `database_backup_interval` | `1440` | Minutes between scheduled **database** backups. `None` disables them. |
| `full_backup_interval` | `None` | Minutes between scheduled **full** backups (database + files + configuration). |
| `retention` | `'1 month'` | Age after which backups are pruned. Accepts `'30 days'`, `'2w'`, `'12h'`, `'1 year'`, or an int (days). `None` keeps everything. |
| `keep_last` | `3` | Never prune the newest N successful backups of each kind. |
| `encryption_key` | `None` | Passphrase for AES-256-GCM encryption of every artifact. Generate with `netbox_backup genkey`. |
| `include_files` | `['media', 'scripts', 'reports']` | File trees in full backups. |
| `include_configuration` | `'auto'` | Bundle `configuration.py` (and `ldap_config.py`) into full backups. `'auto'` means only when encrypted. |
| `extra_configuration_files` | `[]` | Further files to bundle with the configuration (e.g. TLS certificates, SAML metadata). |
| `verify_after_backup` | `True` | Pre-upload verification (dump TOC and archive readability). |
| `stale_after` | 2× shortest interval | Minutes after which **BackupAvailable** turns to *warning*. |
| `housekeeping_interval` | `60` | Minutes between catalog sync, retention pruning and scratch-DB cleanup. |
| `work_dir` | system temp dir | Scratch space while building or restoring. Needs room for the dump plus archives. |
| `pg_dump` / `pg_restore` | `'pg_dump'` / `'pg_restore'` | Client binaries, e.g. `'/usr/lib/postgresql/16/bin/pg_dump'`. |
| `psql` | `'psql'` | Used only when `pg_restore` is newer than the server (see troubleshooting). |
| `pg_dump_extra_args` | `[]` | Extra `pg_dump` arguments, e.g. `['--compress=zstd:3']` (PG 16+). |
| `compression_level` | `6` | gzip level for file archives. |
| `pgbackrest` | `None` | Monitor an external pgBackRest stanza: `{'stanza': 'netbox', 'max_age': '8 days', 'binary': 'pgbackrest', 'config': None}`. |

Changing an interval takes effect when `netbox-rq` restarts.

## Storage backends

### Local directory / mounted share

```python
'storage': {'backend': 'local', 'path': '/mnt/backup/netbox'},
```

Writes are atomic (temporary file, `fsync`, rename). Files are created with mode 0600 and directories with
0700. The NetBox service user needs write access. A local disk only protects against data mistakes, not against
losing the host: prefer NFS/SMB or S3.

### S3-compatible

```python
'storage': {
    'backend': 's3',
    'bucket': 'netbox-backups',
    'prefix': 'prod',                     # optional
    'region_name': 'eu-central-1',
    'endpoint_url': None,                 # e.g. 'https://minio.example.com:9000'
    'access_key_id': '...',               # omit to use the standard AWS credential chain / instance role
    'secret_access_key': '...',
    'session_token': None,
    'verify': True,                       # or a CA bundle path
    'addressing_style': None,             # 'path' for most MinIO setups
    'storage_class': None,                # e.g. 'STANDARD_IA'
    'server_side_encryption': None,       # 'AES256' or 'aws:kms'
    'sse_kms_key_id': None,
},
```

Requires `boto3` (`pip install netbox-backup[s3]`). Minimum IAM permissions on the bucket and prefix:
`s3:ListBucket`, `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`.

Hardening:

* Turn on **bucket versioning** with a lifecycle rule that expires noncurrent versions after N days. A
  compromised NetBox host can then delete backups, but not permanently.
* Better still, use **S3 Object Lock** (governance mode) with a retention at least as long as the plugin's
  `retention`. If the plugin tries to prune a locked backup, the delete fails and is logged on the job, and the
  backup reappears in the catalog at the next sync. That is the intended behaviour.

### Custom backends

`'backend': 'mypackage.storage.MyStorage'`: subclass `netbox_backup.storage.Storage` and implement `put_file`,
`get_file`, `put_bytes`, `get_bytes`, `exists`, `size`, `list_children`, `delete_prefix`, `open_read` and
`describe`. `netbox_backup/tests/test_storage.py::StorageContract` is a ready-made test mixin for a new backend.

## Permissions

NetBox object permissions on the **Backup** object type:

| Action | Grants |
|---|---|
| `view` | Backups list, detail, Status page, `GET` API |
| `delete` | Delete backups (from storage too) |
| `run` *(custom action)* | Run backups, verify, sync catalog, from the UI and the API |
| `download` *(custom action)* | Download artifacts as stored (encrypted artifacts stay encrypted) |

Add `run` and `download` under **Additional actions** when you create the permission. **Restore** has no web
permission: it is a CLI operation on the NetBox host.

## Example: production

```python
PLUGINS_CONFIG = {
    'netbox_backup': {
        'instance_id': 'netbox-prod',
        'storage': {'backend': 's3', 'bucket': 'acme-netbox-backups', 'prefix': 'prod',
                    'region_name': 'eu-central-1'},          # credentials from the instance role
        'encryption_key': os.environ['NETBOX_BACKUP_KEY'],
        'database_backup_interval': 60,                     # hourly database backups
        'full_backup_interval': 1440,                       # daily full backups
        'retention': '45 days',
        'keep_last': 7,
        'work_dir': '/var/tmp/netbox-backup',
    },
}
```
