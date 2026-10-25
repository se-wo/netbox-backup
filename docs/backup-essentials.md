# Backup essentials

The short version: set backups up once, check them, rehearse a restore, and know how to recover onto a new host.
[Backup and restore in depth](backup-and-restore.md) has the details.

## What is protected

| Component | Contents |
|---|---|
| database | The NetBox database, every schema the NetBox role owns (core, plugins, netbox-branching branches) |
| media | Image attachments and every other uploaded file, read through `STORAGES['default']` (local disk, S3 or blob) |
| scripts | Uploaded custom scripts, read through `STORAGES['scripts']` |
| config | `configuration.py` and `ldap_config.py`, **secrets included**, plus the plugin list and pip freeze |

Not protected: Redis (cache and queue only; queued jobs are lost on restore), static files (`collectstatic`
regenerates them), other services' databases (Diode and the like), whole hosts or clusters (use Velero, restic or VM
snapshots for that layer).

## 1. Set up once

1. Install the plugin, GnuPG 2.4+ and the newest `postgresql-client` on the NetBox host and the worker.
2. Pick an **off-host target with immutability**: an S3 bucket with object lock (COMPLIANCE default retention, e.g.
   35 days) and a lifecycle rule aborting incomplete uploads after one day, or an Azure container with locked
   version-level immutability. A set on the database's own disk is not a DR backup.
3. Generate three keys with `nbbackup keys generate`. The **ops** key decrypts restores; keep it in a secret store
   and mount it only for restores. The **recovery** key goes offline (a printout, a smartcard, a safe), because it is
   the last way in. The **signing** key stays on the worker.
4. Write the static configuration ([reference](configuration.md)) and **pin `instance_id`**.
5. Run `manage.py migrate`, start a worker for `netbox_backup.netbox_backup`, then:

   ```bash
   python manage.py nbbackup target probe     # every required check must pass
   python manage.py nbbackup run              # the first set; verified at level 1
   ```

6. Keep a copy of the configuration with the ops and recovery keys: it is what a replacement host needs.

## 2. Check it

```bash
python manage.py nbbackup status          # exit code: 0 ok, 1 warning, 2 error
netbox-backup --config /etc/netbox-backup.json check --max-age 26h    # from outside NetBox
```

Or open **Backup → Status**, or add the *Backup health* dashboard widget. Health is green only if every enabled
policy produced a verified set within its interval + max_runtime + 1 hour **and** at least one verified, locked
off-site copy exists. With `METRICS_ENABLED`, alert on the gauges in [the reference](reference.md#metrics).

## 3. Rehearse a restore

A healthy backup doesn't prove restored data. With a restore slot configured, run a drill now and again (monthly is
a good default):

```bash
python manage.py nbbackup verify latest --drill --headroom-confirmed
```

It loads the set into the slot, compares the baseline, checks the migration plan and empties the slot again. A pass
marks the set as level 2 (QA restore).

## 4. Undo a mistake (bulk edit gone wrong)

```bash
python manage.py nbbackup restore plan latest
# with a slot: load, switch, finish (see the README); without one:
python manage.py nbbackup restore load latest --in-place --confirm netbox
```

In place takes a verified safety set first. `restore rollback --confirm netbox` returns to it, and
`restore finalize` cleans up after the rollback window.

## 5. Recover onto a new host

1. Install NetBox (same or newer version), the same plugins, this plugin, GnuPG and the PostgreSQL client tools.
2. Restore the configuration. It is in the set's config component:

   ```bash
   netbox-backup --config nbb.json decrypt latest --components config --out /tmp/recovered
   ```

   `nbb.json` holds the plugin's dict: targets with their credential references, `crypto` with the recovery key as
   identity, and the pinned `instance_id`. `config.json` in the output contains `configuration.py` verbatim, so
   `SECRET_KEY` and `API_TOKEN_PEPPERS` match and API tokens keep working.
3. Create an empty database owned by the NetBox role, point `configuration.py` at it, and restore:

   ```bash
   python manage.py nbbackup restore plan latest
   python manage.py nbbackup restore load latest --in-place     # an empty database needs no --confirm
   ```

   The load reads storage directly and finishes in the same run: migrations, media, sessions, job state.
4. Start NetBox and the workers. A full backup is already queued.

If no NetBox image boots at all, `netbox-backup restore-db latest --dsn postgresql://netbox@db/netbox --confirm
netbox` loads the database with only the configuration file and gpg. Run `nbbackup restore finish` once NetBox
starts.
