"""
netbox-backup: the standalone break-glass CLI. No Django, no NetBox database.

It reads the same configuration as the plugin (the dict under PLUGINS_CONFIG['netbox_backup']) from a JSON file,
or from a configuration.py, and works directly against storage:

    netbox-backup --config nbb.json list
    netbox-backup --config nbb.json verify latest
    netbox-backup --config nbb.json fetch latest --out ./set
    netbox-backup --config nbb.json decrypt latest --out ./plain
    netbox-backup --config nbb.json restore-db latest --dsn postgresql://netbox@db/netbox_b
    netbox-backup --config nbb.json check --max-age 26h

restore-db is the last resort when no NetBox image can boot: it empties the target database (marked slot, or
--confirm <database name>), loads the set and checks the baseline. Afterwards start NetBox against that database
and run "manage.py nbbackup restore finish" as usual.
"""
import argparse
import json
import os
import runpy
import sys
import tempfile
from datetime import UTC, datetime

from .core import layout, sets
from .core import manifest as manifest_mod
from .core import pg as pgcore
from .core.config import Config, ConfigError, parse_duration
from .core.gpg import GPGError, Keyring
from .storage import StorageError, get_backend


def load_config(path):
    if path.endswith('.py'):
        namespace = runpy.run_path(path)
        data = namespace.get('PLUGINS_CONFIG', {}).get('netbox_backup')
        if data is None:
            raise ConfigError(f"{path} has no PLUGINS_CONFIG['netbox_backup']")
        seed = namespace.get('SECRET_KEY')
    else:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        seed = None
    return Config.from_dict(data, fallback_instance_seed=seed)


def _spool(config):
    if config.spool_dir:
        os.makedirs(config.spool_dir, mode=0o700, exist_ok=True)
    return config.spool_dir


def _targets(config, name):
    if name:
        return {name: config.target(name)}
    return config.targets


def _find(config, set_ref, target, instance):
    errors = []
    for name, cfg in _targets(config, target).items():
        try:
            backend = get_backend(cfg)
            listing = sets.resolve(backend, instance, set_ref)
            return name, backend, sets.load_manifest(backend, instance, listing.set_id)
        except (sets.SetError, StorageError, manifest_mod.ManifestError) as e:
            errors.append(f'{name}: {e}')
    raise SystemExit('Set not found: ' + '; '.join(errors))


def cmd_list(args, config):
    rows = []
    for name, target in _targets(config, args.target).items():
        backend = get_backend(target)
        instances = sets.list_instances(backend) if args.all_instances else [args.instance]
        for instance in instances:
            for listing in sets.list_sets(backend, instance):
                rows.append({'target': name, 'instance': instance, 'set_id': listing.set_id,
                             'created': listing.created.isoformat(), 'complete': listing.complete,
                             'size_bytes': listing.size})
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        print(f"{row['set_id']}  {row['target']:<14} {row['instance'][:8]}  {row['created'][:19]}  "
              f"{row['size_bytes']:>14}  {'complete' if row['complete'] else 'INCOMPLETE'}")
    return 0


def cmd_verify(args, config):
    """
    Level 1 against storage, and with --deep a full download and SHA-384 check of every object (decrypting when
    an identity is mounted).
    """
    name, backend, manifest = _find(config, args.set, args.target, args.instance)
    with Keyring(config.crypto, identities=args.deep and bool(config.crypto.identities)) as keyring:
        results = sets.level1(backend, manifest, keyring, target=config.target(name))
        if args.deep:
            with tempfile.TemporaryDirectory(dir=_spool(config)) as spool:
                for obj_name, obj in manifest.objects.items():
                    try:
                        path = sets.fetch(backend, obj, os.path.join(spool, obj_name))
                        results.append({'check': f'{obj_name}: sha384', 'ok': True, 'detail': 'downloaded'})
                        if config.crypto.identities:
                            sets.decrypt_file(keyring, path, path + '.plain')
                            results.append({'check': f'{obj_name}: decrypt', 'ok': True, 'detail': ''})
                    except (sets.SetError, GPGError) as e:
                        results.append({'check': f'{obj_name}: download', 'ok': False, 'detail': str(e)})
    for r in results:
        print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['check']}" + (f": {r['detail']}" if r['detail'] else ''))
    return 0 if all(r['ok'] for r in results) else 1


def cmd_fetch(args, config):
    _, backend, manifest = _find(config, args.set, args.target, args.instance)
    os.makedirs(args.out, mode=0o700, exist_ok=True)
    with open(os.path.join(args.out, layout.MANIFEST), 'wb') as f:
        f.write(manifest.raw)
    for obj_name, obj in manifest.objects.items():
        sets.fetch(backend, obj, os.path.join(args.out, obj_name))
        print(f"fetched {obj_name} ({obj['size']} bytes, SHA-384 verified)")
    return 0


def cmd_decrypt(args, config):
    _, backend, manifest = _find(config, args.set, args.target, args.instance)
    with Keyring(config.crypto, identities=True) as keyring:
        signer = manifest_mod.verify(manifest, keyring)
        print(f"manifest signed by {signer}")
        os.makedirs(args.out, mode=0o700, exist_ok=True)
        body = manifest_mod.decrypt_body(manifest, keyring)
        with open(os.path.join(args.out, 'manifest-body.json'), 'w') as f:
            json.dump(body, f, indent=2)
        components = args.components or [layout.COMPONENT_BY_OBJECT[n] for n in manifest.objects
                                          if n in layout.COMPONENT_BY_OBJECT]
        paths = sets.fetch_and_decrypt(backend, keyring, manifest, components, args.out)
    for component, path in paths.items():
        print(f"{component}: {path}")
    return 0


def cmd_restore_db(args, config):
    _, backend, manifest = _find(config, args.set, args.target, args.instance)
    db = pgcore.Database.from_dsn(args.dsn, pg_dump=config.pg_dump, pg_restore=config.pg_restore, psql=config.psql)
    with db.connect() as conn:
        marked = pgcore.has_slot_marker(conn, db.name)
        if not marked and args.confirm != db.name:
            raise SystemExit(f"{db.name} carries no slot marker: pass --confirm {db.name} to replace it")
        sessions = pgcore.other_sessions(conn)
        if sessions:
            raise SystemExit(f"{len(sessions)} other session(s) are connected to {db.name}")
    with Keyring(config.crypto, identities=True) as keyring:
        manifest_mod.verify(manifest, keyring)
        body = manifest_mod.decrypt_body(manifest, keyring)
        with tempfile.TemporaryDirectory(dir=_spool(config)) as spool:
            paths = sets.fetch_and_decrypt(backend, keyring, manifest, ['database'], spool)
            with db.connect() as conn:
                pgcore.empty_database(conn)
                with conn.cursor() as cursor:
                    missing = pgcore.missing_roles(cursor, body.get('database', {}).get('grantees', []))
            print(f"emptied {db.name}; loading set {manifest.set_id}")
            pgcore.load_dump(db, paths['database'], jobs=config.restore_jobs, no_privileges=bool(missing))
    with db.connect() as conn, conn.cursor() as cursor:
        diff = pgcore.compare_baseline(body['baseline'], pgcore.baseline(cursor))
        if marked:
            # The database now holds NetBox data; it is no longer an empty slot
            pgcore.set_slot_marker(conn, db.name, False)
    if diff:
        print(f"BASELINE MISMATCH: {pgcore.format_differences(diff)}", file=sys.stderr)
        return 2
    if missing:
        print(f"Loaded without privileges; re-apply grants for: {', '.join(missing)}", file=sys.stderr)
    print(f"Set {manifest.set_id} loaded into {db.name}; baseline matches. Start NetBox against {db.name} and run "
          f"'manage.py nbbackup restore finish'.")
    return 0


def cmd_check(args, config):
    """
    External dead-man's check: the newest complete set of the instance must be younger than --max-age on every
    target (or the given one). Exit 0 ok, 2 too old or unreachable.
    """
    max_age = parse_duration(args.max_age)
    now = datetime.now(UTC)
    status = 0
    for name, target in _targets(config, args.target).items():
        try:
            complete = sets.list_sets(get_backend(target), args.instance, include_incomplete=False)
        except StorageError as e:
            print(f"CRITICAL {name}: {e}")
            status = 2
            continue
        if not complete:
            print(f"CRITICAL {name}: no complete set")
            status = 2
            continue
        age = now - complete[-1].created
        if age > max_age:
            print(f"CRITICAL {name}: newest set {complete[-1].set_id} is {age} old (max {max_age})")
            status = 2
        else:
            print(f"OK {name}: newest set {complete[-1].set_id} is {age} old")
    return status


def build_parser():
    parser = argparse.ArgumentParser(prog='netbox-backup', description=__doc__.split('\n\n')[0].strip())
    parser.add_argument('--config', default=os.environ.get('NETBOX_BACKUP_CONFIG', '/etc/netbox-backup.json'),
                        help='JSON file with the plugin configuration, or a configuration.py '
                             '(default: $NETBOX_BACKUP_CONFIG or /etc/netbox-backup.json)')
    parser.add_argument('--target', help='Only this target')
    parser.add_argument('--instance', help='Instance ID (default: the configured one)')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('list', help='List sets')
    p.add_argument('--all-instances', action='store_true')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('verify', help='Integrity check (level 1; --deep downloads everything)')
    p.add_argument('set', nargs='?', default='latest')
    p.add_argument('--deep', action='store_true')
    p = sub.add_parser('fetch', help='Download a set (ciphertext) and check every hash')
    p.add_argument('set', nargs='?', default='latest')
    p.add_argument('--out', required=True)
    p = sub.add_parser('decrypt', help='Download, verify and decrypt a set')
    p.add_argument('set', nargs='?', default='latest')
    p.add_argument('--out', required=True)
    p.add_argument('--components', nargs='+', choices=sorted(layout.OBJECT_NAMES))
    p = sub.add_parser('restore-db', help='Load a set into a database without NetBox')
    p.add_argument('set', nargs='?', default='latest')
    p.add_argument('--dsn', required=True, help='postgresql://user@host/dbname (password via PGPASSWORD/.pgpass)')
    p.add_argument('--confirm', metavar='DATABASE')
    p = sub.add_parser('check', help="Dead-man's check for external monitoring")
    p.add_argument('--max-age', required=True, help='e.g. 26h')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ConfigError) as e:
        print(f"netbox-backup: {e}", file=sys.stderr)
        return 2
    args.instance = args.instance or config.instance_id
    handlers = {'list': cmd_list, 'verify': cmd_verify, 'fetch': cmd_fetch, 'decrypt': cmd_decrypt,
                'restore-db': cmd_restore_db, 'check': cmd_check}
    try:
        return handlers[args.command](args, config)
    except (StorageError, GPGError, sets.SetError, manifest_mod.ManifestError, pgcore.PostgresError, ConfigError) as e:
        print(f"netbox-backup: {e}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
