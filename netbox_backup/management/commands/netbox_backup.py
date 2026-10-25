import getpass
import json
import logging
import os
import secrets
import sys
import tempfile

from django.core.management.base import BaseCommand, CommandError

from netbox_backup import conf
from netbox_backup.choices import BackupKindChoices, BackupTriggerChoices, VerificationLevelChoices
from netbox_backup.engine import catalog, health, retention
from netbox_backup.engine.backup import BackupError, create_backup
from netbox_backup.engine.restore import FILE_COMPONENTS, preflight, restore_backup
from netbox_backup.engine.verify import Verifier
from netbox_backup.storage import get_storage


def human_size(n):
    n = float(n or 0)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return None


class _StdoutHandler(logging.Handler):
    def __init__(self, command):
        super().__init__()
        self.command = command

    def emit(self, record):
        message = record.getMessage()
        if record.levelno >= logging.ERROR:
            self.command.stderr.write(self.command.style.ERROR(message))
        elif record.levelno >= logging.WARNING:
            self.command.stdout.write(self.command.style.WARNING(message))
        else:
            self.command.stdout.write(message)


class Command(BaseCommand):
    help = "Create, verify, list, prune and restore NetBox backups."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest='action', required=True, metavar='ACTION')

        p = sub.add_parser('create', help='Create a backup now')
        p.add_argument('--full', action='store_true', help='Include media, scripts, reports and configuration')
        p.add_argument('--verify', choices=VerificationLevelChoices.values(), help='Verify after creating')
        p.add_argument('--no-prune', action='store_true', help='Skip retention pruning afterwards')

        p = sub.add_parser('list', help='List backups in storage')
        p.add_argument('--instance', help='List another instance\'s backups')
        p.add_argument('--instances', action='store_true', help='List instance IDs present in storage')
        p.add_argument('--json', action='store_true')

        p = sub.add_parser('show', help='Show a backup\'s manifest')
        p.add_argument('backup', help='Backup name, unique prefix, "latest" or "latest-full"')
        p.add_argument('--instance')
        p.add_argument('--json', action='store_true')

        p = sub.add_parser('verify', help='Verify a backup')
        p.add_argument('backup')
        p.add_argument('--instance')
        p.add_argument('--level', choices=VerificationLevelChoices.values(), default='deep')

        p = sub.add_parser('restore', help='Restore a backup (stop NetBox services first)')
        p.add_argument('backup', help='Backup name, unique prefix, "latest" or "latest-full"')
        p.add_argument('--instance', help='Restore from another instance ID (implies --allow-foreign-instance)')
        p.add_argument('--components', help=f'Comma-separated subset of: database,{",".join(FILE_COMPONENTS)}')
        p.add_argument('--yes', action='store_true', help='Do not prompt for confirmation')
        p.add_argument('--dry-run', action='store_true', help='Run preflight checks only')
        p.add_argument('--no-safety-backup', action='store_true', help='Skip the pre-restore safety backup')
        p.add_argument('--allow-foreign-instance', action='store_true',
                       help='Allow restoring a backup taken by a different NetBox instance')
        p.add_argument('--allow-newer', action='store_true',
                       help='Allow restoring a backup taken by newer NetBox/plugin code (not recommended)')
        p.add_argument('--terminate-connections', action='store_true',
                       help='Terminate other database sessions instead of refusing')
        p.add_argument('--no-migrate', action='store_true', help='Do not run migrations after the restore')
        p.add_argument('--extract-config', metavar='DIR', help='Also extract backed-up configuration files to DIR')
        p.add_argument('--jobs', type=int, default=1, help='Parallel pg_restore jobs')

        p = sub.add_parser('download', help='Download (and decrypt) a backup\'s artifacts for manual inspection')
        p.add_argument('backup')
        p.add_argument('destination')
        p.add_argument('--instance')

        p = sub.add_parser('delete', help='Delete a backup from storage and the catalog')
        p.add_argument('backup')
        p.add_argument('--yes', action='store_true')

        p = sub.add_parser('prune', help='Apply the retention policy')
        p.add_argument('--dry-run', action='store_true')

        sub.add_parser('sync', help='Rebuild the catalog from storage')

        p = sub.add_parser('status', help='Show health conditions (exit 0 ok, 1 warning, 2 error)')
        p.add_argument('--json', action='store_true')

        sub.add_parser('genkey', help='Generate a random encryption key')

    def handle(self, *args, **options):
        self.log = logging.getLogger('netbox_backup.cli')
        self.log.propagate = False
        self.log.setLevel(logging.DEBUG if options['verbosity'] > 1 else logging.INFO)
        if not self.log.handlers:
            self.log.addHandler(_StdoutHandler(self))
        try:
            return getattr(self, f"do_{options['action']}")(options)
        except BackupError as e:
            raise CommandError(str(e))

    # Actions

    def do_create(self, options):
        kind = BackupKindChoices.KIND_FULL if options['full'] else BackupKindChoices.KIND_DATABASE
        try:
            backup = create_backup(kind=kind, trigger=BackupTriggerChoices.TRIGGER_CLI,
                                   requested_by=getpass.getuser(), log=self.log)
        except Exception as e:
            raise CommandError(f"Backup failed: {e}")
        self.stdout.write(self.style.SUCCESS(f"Created {backup.name} ({human_size(backup.size)}) at {backup.location}"))
        if options['verify']:
            from netbox_backup.engine.verify import verify_backup
            passed, _ = verify_backup(backup, level=options['verify'], log=self.log)
            if not passed:
                raise CommandError("Verification failed")
        if not options['no_prune']:
            retention.prune(log=self.log)

    def do_list(self, options):
        storage = get_storage()
        if options['instances']:
            local = conf.instance_id()
            for instance in catalog.list_instances(storage):
                self.stdout.write(f"{instance}{'  (this instance)' if instance == local else ''}")
            return
        manifests = catalog.list_manifests(options['instance'], storage)
        if options['json']:
            self.stdout.write(json.dumps([{k: m.get(k) for k in (
                'name', 'kind', 'status', 'started_at', 'size', 'instance_id')} for m in manifests], indent=2))
            return
        if not manifests:
            self.stdout.write(f"No backups for instance {options['instance'] or conf.instance_id()}")
            return
        self.stdout.write(f"{'NAME':<36} {'KIND':<9} {'STATUS':<10} {'SIZE':>10}  {'NETBOX':<10} {'ROWS':>9}  ENC")
        for m in manifests:
            self.stdout.write(
                f"{m['name']:<36} {m['kind']:<9} {m.get('status', '?'):<10} {human_size(m.get('size')):>10}  "
                f"{m.get('netbox', {}).get('version', '?'):<10} {m.get('database', {}).get('rows', 0):>9}  "
                f"{'yes' if m.get('encryption', {}).get('enabled') else 'no'}"
            )
        incomplete = catalog.incomplete_backups(options['instance'], storage)
        if incomplete:
            self.stdout.write(self.style.WARNING(f"Incomplete (no manifest): {', '.join(incomplete)}"))

    def do_show(self, options):
        m = catalog.resolve(options['backup'], options['instance'])
        if options['json']:
            self.stdout.write(json.dumps(m, indent=2, sort_keys=True))
            return
        db = m.get('database', {})
        self.stdout.write(f"Name:         {m['name']}")
        self.stdout.write(f"Instance:     {m['instance_id']}  (host {m.get('hostname')})")
        self.stdout.write(f"Kind:         {m['kind']}  trigger={m.get('trigger')}  status={m.get('status')}")
        self.stdout.write(f"Taken:        {m.get('started_at')}  ({m.get('duration_seconds')}s)")
        self.stdout.write(f"NetBox:       {m.get('netbox', {}).get('version')}  plugin {m.get('plugin_version')}")
        self.stdout.write(f"PostgreSQL:   {m.get('postgresql', {}).get('server_version')}")
        self.stdout.write(f"Database:     {db.get('tables')} tables, {db.get('rows')} rows, "
                          f"{len(m.get('migrations', []))} migrations")
        self.stdout.write(f"Encrypted:    {'yes (key ' + m['encryption']['key_fingerprint'] + ')' if m.get('encryption', {}).get('enabled') else 'no'}")  # noqa: E501
        self.stdout.write("Artifacts:")
        for a in m.get('artifacts', []):
            self.stdout.write(f"  {a['name']:<28} {human_size(a['size']):>10}  sha256 {a['sha256'][:16]}…")
        for w in m.get('warnings', []):
            self.stdout.write(self.style.WARNING(f"Warning: {w}"))

    def do_verify(self, options):
        manifest = catalog.resolve(options['backup'], options['instance'])
        from netbox_backup.models import Backup
        backup = Backup.objects.filter(name=manifest['name']).first()
        if backup is not None:
            from netbox_backup.engine.verify import verify_backup
            passed, _ = verify_backup(backup, level=options['level'], log=self.log)
        else:
            passed = Verifier(manifest, log=self.log).run(options['level'])
        if not passed:
            raise CommandError(f"Verification of {manifest['name']} FAILED")
        self.stdout.write(self.style.SUCCESS(f"Verification of {manifest['name']} passed ({options['level']})"))

    def do_restore(self, options):
        instance = options['instance']
        manifest = catalog.resolve(options['backup'], instance)
        components = options['components'].split(',') if options['components'] else None
        allow_foreign = options['allow_foreign_instance'] or bool(instance)

        self.stdout.write(f"Backup:   {manifest['name']} ({manifest['kind']}, taken {manifest.get('started_at')} on "
                          f"{manifest.get('hostname')}, NetBox {manifest.get('netbox', {}).get('version')})")
        self.stdout.write(f"Instance: {manifest['instance_id']}  (this installation: {conf.instance_id()})")
        self.stdout.write(f"Data:     {manifest['database']['tables']} tables, {manifest['database']['rows']} rows")

        check = preflight(manifest, components=components, allow_foreign_instance=allow_foreign,
                          allow_newer=options['allow_newer'], terminate_connections=options['terminate_connections'])
        self.stdout.write(f"Restores: {', '.join(check.components)}")
        for note in check.notes:
            self.stdout.write(f"  - {note}")
        for warning in check.warnings:
            self.stdout.write(self.style.WARNING(f"  ! {warning}"))
        for error in check.errors:
            self.stderr.write(self.style.ERROR(f"  ✗ {error}"))
        if not check.ok:
            raise CommandError("Preflight checks failed; nothing was changed.")
        self.stdout.write(self.style.SUCCESS("Preflight checks passed."))
        if options['dry_run']:
            return

        if not options['yes']:
            if not sys.stdin.isatty():
                raise CommandError("Refusing to restore without --yes when not attached to a terminal")
            self.stdout.write(self.style.WARNING(
                "\nThis REPLACES the current NetBox data with the backup's. A safety backup is taken first"
                f"{' (disabled!)' if options['no_safety_backup'] else ''}."
            ))
            answer = input(f"Type the backup name to confirm ({manifest['name']}): ").strip()
            if answer != manifest['name']:
                raise CommandError("Confirmation did not match; aborted.")

        try:
            event = restore_backup(
                manifest, components=components, restored_by=getpass.getuser(),
                safety_backup=not options['no_safety_backup'], allow_foreign_instance=allow_foreign,
                allow_newer=options['allow_newer'], terminate_connections=options['terminate_connections'],
                migrate=not options['no_migrate'], extract_config_to=options['extract_config'], jobs=options['jobs'],
                log=self.log,
            )
        except Exception as e:
            raise CommandError(f"Restore failed: {e}")

        if event.status == 'recovery-required':
            raise CommandError("RECOVERY REQUIRED: restored data does not match the backup's row counts. "
                               "See the restore log; do not put this instance back into service yet.")
        status = event.get_status_display()
        self.stdout.write(self.style.SUCCESS(f"\nRestore of {manifest['name']} complete ({status})."))
        if event.safety_backup_name:
            self.stdout.write(f"Undo with: manage.py netbox_backup restore {event.safety_backup_name}")
        self.stdout.write("Next: restart NetBox services (e.g. systemctl restart netbox netbox-rq).")

    def do_download(self, options):
        manifest = catalog.resolve(options['backup'], options['instance'])
        dest = options['destination']
        os.makedirs(dest, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dest, prefix='.download-') as workdir:
            paths = catalog.fetch_artifacts(manifest, workdir, log=self.log)
            for artifact_type, path in paths.items():
                os.replace(path, os.path.join(dest, os.path.basename(path)))
        with open(os.path.join(dest, 'manifest.json'), 'w') as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
        self.stdout.write(self.style.SUCCESS(f"Downloaded {manifest['name']} to {dest}"))
        self.stdout.write(f"Manual database restore: pg_restore --no-owner --dbname=<db> {dest}/database.dump")

    def do_delete(self, options):
        from netbox_backup.models import Backup
        manifest = catalog.resolve(options['backup'])
        if not options['yes']:
            answer = input(f"Delete {manifest['name']} permanently? [y/N] ").strip().lower()
            if answer != 'y':
                raise CommandError("Aborted")
        backup = Backup.objects.filter(name=manifest['name']).first()
        if backup:
            retention.delete_backup(backup, log=self.log)
        else:
            get_storage().delete_prefix(f"{manifest['instance_id']}/{manifest['name']}")
        self.stdout.write(self.style.SUCCESS(f"Deleted {manifest['name']}"))

    def do_prune(self, options):
        removed = retention.prune(dry_run=options['dry_run'], log=self.log)
        verb = 'Would delete' if options['dry_run'] else 'Deleted'
        self.stdout.write(f"{verb} {len(removed)} backup(s)")

    def do_sync(self, options):
        catalog.sync(log=self.log)

    def do_status(self, options):
        conds = health.conditions()
        worst = health.overall(conds)
        if options['json']:
            self.stdout.write(json.dumps({
                'instance_id': conf.instance_id(),
                'status': worst,
                'conditions': [c.as_dict() for c in conds],
            }, indent=2))
        else:
            self.stdout.write(f"Instance {conf.instance_id()}: {worst.upper()}")
            styles = {'ok': self.style.SUCCESS, 'info': self.style.HTTP_INFO, 'warning': self.style.WARNING,
                      'error': self.style.ERROR}
            for c in conds:
                self.stdout.write(f"  {styles[c.status](f'{c.status.upper():<7}')} {c.name:<24} {c.message}")
        sys.exit({'ok': 0, 'info': 0, 'warning': 1, 'error': 2}[worst])

    def do_genkey(self, options):
        self.stdout.write(secrets.token_urlsafe(32))
