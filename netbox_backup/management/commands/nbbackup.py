"""
manage.py nbbackup ...

    run <policy> · pre-upgrade · list · show <set> · verify <set> [--drill] · prune [--dry-run] · slot init <db>
    restore plan|load|finish|status|rollback|finalize|media · catalog sync · target probe <name>
    keys generate · status [--json]

Restore commands run from an ops shell or a one-shot container, never the web process. They skip Django system
checks and work against an empty or unmigrated database.
"""
import json
import logging
import os
import sys

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ... import conf
from ...choices import RestoreStrategyChoices, TriggerChoices

logger = logging.getLogger('netbox_backup')


def human_size(n):
    if n is None:
        return '-'
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if abs(n) < 1024:
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TiB'


class _Echo(logging.Handler):
    def __init__(self, command):
        super().__init__()
        self.command = command

    def emit(self, record):
        style = {logging.ERROR: self.command.style.ERROR, logging.WARNING: self.command.style.WARNING}.get(
            record.levelno, str)
        self.command.stdout.write(style(record.getMessage()))


class Command(BaseCommand):
    help = 'netbox-backup: backups, verification and restores'
    requires_system_checks = []
    requires_migrations_checks = False

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest='action', required=True)

        p = sub.add_parser('run', help='Run a backup now')
        p.add_argument('policy', nargs='?')
        p.add_argument('--components', nargs='+')
        p.add_argument('--background', action='store_true', help='Enqueue on the plugin queue instead')

        p = sub.add_parser('pre-upgrade', help='A manual set tagged for an upgrade')
        p.add_argument('--policy')

        p = sub.add_parser('list', help='List sets in storage (no database needed)')
        p.add_argument('--target')
        p.add_argument('--instance')
        p.add_argument('--json', action='store_true')

        p = sub.add_parser('show', help='Show a set')
        p.add_argument('set', nargs='?', default='latest')
        p.add_argument('--target')
        p.add_argument('--json', action='store_true')

        p = sub.add_parser('verify', help='Level-1 check, or a drill (QA restore in the slot)')
        p.add_argument('set', nargs='?', default='latest')
        p.add_argument('--drill', action='store_true')
        p.add_argument('--target')
        p.add_argument('--headroom-confirmed', action='store_true')

        p = sub.add_parser('prune', help='Apply retention (deletes only with a prune credential)')
        p.add_argument('--dry-run', action='store_true')
        p.add_argument('--target', action='append')

        p = sub.add_parser('slot', help='Restore slot setup')
        slot = p.add_subparsers(dest='slot_action', required=True)
        q = slot.add_parser('init')
        q.add_argument('database')

        p = sub.add_parser('restore', help='Restore workflow')
        restore = p.add_subparsers(dest='restore_action', required=True)
        for name in ('plan', 'load'):
            q = restore.add_parser(name)
            q.add_argument('set', nargs='?', default='latest')
            q.add_argument('--target')
            q.add_argument('--from-instance')
            q.add_argument('--components', nargs='+', choices=('database', 'media', 'scripts'))
            q.add_argument('--headroom-confirmed', action='store_true')
            q.add_argument('--json', action='store_true')
            if name == 'load':
                q.add_argument('--slot', action='store_true')
                q.add_argument('--in-place', action='store_true')
                q.add_argument('--confirm', metavar='DATABASE')
                q.add_argument('--skip-safety', action='store_true')
                q.add_argument('--revoke-resurrected', action='store_true')
        q = restore.add_parser('finish')
        q.add_argument('restore_id', nargs='?')
        q.add_argument('--qa', action='store_true')
        q.add_argument('--revoke-resurrected', action='store_true', default=None)
        q = restore.add_parser('status')
        q.add_argument('restore_id', nargs='?')
        q.add_argument('--json', action='store_true')
        q = restore.add_parser('rollback')
        q.add_argument('restore_id', nargs='?')
        q.add_argument('--confirm', metavar='DATABASE')
        q = restore.add_parser('finalize')
        q.add_argument('restore_id', nargs='?')
        q.add_argument('--force', action='store_true')
        q = restore.add_parser('media')
        q.add_argument('set', nargs='?', default='latest')
        q.add_argument('--target')
        q.add_argument('--components', nargs='+', choices=('media', 'scripts'), default=['media'])

        p = sub.add_parser('catalog', help='Catalog maintenance')
        catalog = p.add_subparsers(dest='catalog_action', required=True)
        catalog.add_parser('sync')

        p = sub.add_parser('target', help='Storage targets')
        target = p.add_subparsers(dest='target_action', required=True)
        q = target.add_parser('probe')
        q.add_argument('name', nargs='?')

        p = sub.add_parser('keys', help='Key management')
        keys = p.add_subparsers(dest='keys_action', required=True)
        q = keys.add_parser('generate', help='Generate a brainpoolP384r1 key pair')
        q.add_argument('--name', required=True, help='User ID, e.g. "netbox-backup ops <ops@example.com>"')
        q.add_argument('--out', required=True, help='Directory for <fingerprint>.pub.asc and .sec.asc')
        q.add_argument('--passphrase-env', help='Environment variable holding the passphrase')

        p = sub.add_parser('status', help='Health summary (exit 0 ok, 1 warning, 2 error)')
        p.add_argument('--json', action='store_true')
        p.add_argument('--no-workers', action='store_true', help='Skip the RQ worker and scheduler checks')

    def handle(self, *args, **options):
        self.options = options
        handler = _Echo(self)
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.INFO)
        try:
            action = options['action'].replace('-', '_')
            return getattr(self, f'do_{action}')(options)
        except CommandError:
            raise
        except Exception as e:
            raise CommandError(f'{type(e).__name__}: {e}') from e
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)

    def _user(self):
        return os.environ.get('SUDO_USER') or os.environ.get('USER') or 'cli'

    def _policy(self, name):
        config = conf.get_config()
        if name:
            return config.policy(name).name
        if len(config.policies) != 1:
            raise CommandError(f"Name a policy: {', '.join(config.policies) or 'none configured'}")
        return next(iter(config.policies))

    # Backups

    def do_run(self, options, trigger=TriggerChoices.CLI):
        policy = self._policy(options.get('policy'))
        if options.get('background'):
            from ...engine import catalog
            from ...jobs import QUEUE, BackupJob
            from ...models import BackupPolicy
            catalog.sync_static()
            row = BackupPolicy.objects.get(name=policy)
            job = BackupJob.enqueue(instance=row, policy=policy, trigger=trigger, created_by=self._user(),
                                    components=options.get('components'), queue_name=QUEUE,
                                    job_timeout=int(row.max_runtime.total_seconds()))
            self.stdout.write(f"Enqueued job {job.pk}")
            return
        from ...engine.backup import run_backup
        backup_set = run_backup(policy, trigger=trigger, created_by=self._user(), log=logger,
                                components=options.get('components'))
        self.stdout.write(self.style.SUCCESS(
            f"{backup_set.set_id} {backup_set.get_status_display()} {human_size(backup_set.size_bytes)}"
        ))

    def do_pre_upgrade(self, options):
        return self.do_run({'policy': options.get('policy')}, trigger=TriggerChoices.PRE_UPGRADE)

    def do_list(self, options):
        from ...core import sets
        from ...storage import get_backend
        config = conf.get_config()
        instance = options.get('instance') or config.instance_id
        rows = []
        for name, target in config.targets.items():
            if options.get('target') and name != options['target']:
                continue
            backend = get_backend(target)
            for listing in sets.list_sets(backend, instance):
                rows.append({'target': name, 'set_id': listing.set_id, 'created': listing.created.isoformat(),
                             'complete': listing.complete, 'size_bytes': listing.size,
                             'objects': sorted(listing.objects)})
        if options.get('json'):
            self.stdout.write(json.dumps(rows, indent=2))
            return
        self.stdout.write(f"{'SET':<27} {'TARGET':<16} {'CREATED':<20} {'SIZE':>10}  STATE")
        for row in rows:
            state = 'complete' if row['complete'] else 'incomplete'
            self.stdout.write(f"{row['set_id']:<27} {row['target']:<16} {row['created'][:19]:<20} "
                              f"{human_size(row['size_bytes']):>10}  {state}")

    def do_show(self, options):
        from ...engine.restore import locate
        config = conf.get_config()
        target, backend, manifest = locate(config, options['set'], target=options.get('target'))
        document = {'target': target, 'location': backend.describe(), 'header': manifest.header}
        try:
            from ...core import manifest as manifest_mod
            from ...core.gpg import Keyring
            with Keyring(config.crypto, identities=bool(config.crypto.identities)) as keyring:
                document['signer'] = manifest_mod.verify(manifest, keyring)
                if config.crypto.identities:
                    body = manifest_mod.decrypt_body(manifest, keyring)
                    document['body'] = {k: v for k, v in body.items() if k != 'baseline'}
                    document['baseline'] = {'tables': len(body['baseline']['tables']),
                                            'rows': body['baseline']['rows'],
                                            'newest_last_updated': body['baseline']['newest_last_updated']}
        except Exception as e:
            document['verification_error'] = str(e)
        self.stdout.write(json.dumps(document, indent=2, default=str))

    def do_verify(self, options):
        if options['drill']:
            from ...engine.restore import Restorer
            journal = Restorer(log=logger, user=self._user()).drill(
                options['set'], target=options.get('target'), headroom_confirmed=options['headroom_confirmed'])
            self.stdout.write(self.style.SUCCESS(f"Drill {journal.restore_id} passed for set {journal['set_id']}"))
            return
        from ...engine import catalog
        from ...engine.restore import locate
        from ...engine.verify import verify_set
        from ...models import BackupSet
        _, _, manifest = locate(conf.get_config(), options['set'], target=options.get('target'))
        if not BackupSet.objects.filter(set_id=manifest.set_id).exists():
            catalog.sync()
        if not verify_set(manifest.set_id, log=logger):
            raise CommandError(f"Set {manifest.set_id} failed verification")
        self.stdout.write(self.style.SUCCESS(f"Set {manifest.set_id} passed level 1"))

    def do_prune(self, options):
        from ...engine import catalog
        from ...engine.prune import prune
        catalog.sync(log=logger)
        failed = False
        for report in prune(dry_run=options['dry_run'], log=logger, targets=options.get('target')):
            mode = 'deleting' if report.can_delete else 'dry run (no prune credential)' if not options['dry_run'] \
                else 'dry run'
            self.stdout.write(f"{report.target}: {mode}")
            for set_id in report.delete:
                done = ' (deleted)' if set_id in report.deleted else ''
                self.stdout.write(f"  - {set_id}{done}")
            for set_id, until in report.locked.items():
                self.stdout.write(f"  ~ {set_id} locked until {until}")
            for set_id in report.waiting_for_replicas:
                self.stdout.write(f"  ~ {set_id} waits for its replicas")
            self.stdout.write(f"  kept {len(report.keep)} set(s)")
            if report.probes_due:
                done = f", {report.probes_deleted} deleted" if report.can_delete else ''
                self.stdout.write(f"  {len(report.probes_due)} expired probe object(s){done}")
            for error in report.errors:
                failed = True
                self.stderr.write(self.style.ERROR(f"  ! {error}"))
        if failed:
            raise CommandError('Prune finished with errors')

    # Slots and restores

    def do_slot(self, options):
        from ...engine.restore import init_slot
        database = init_slot(options['database'])
        self.stdout.write(self.style.SUCCESS(f"{database} is now a restore slot (COMMENT 'nbb:slot')"))

    def do_restore(self, options):
        from ...engine import journal as journal_mod
        from ...engine.restore import Restorer, build_plan
        action = options['restore_action']
        restorer = Restorer(log=logger, user=self._user())
        if action == 'plan':
            plan = build_plan(options['set'], target=options.get('target'), from_instance=options.get('from_instance'),
                              components=options.get('components'), headroom_confirmed=options['headroom_confirmed'])
            if options['json']:
                self.stdout.write(json.dumps(plan.as_dict(), indent=2, default=str))
            else:
                self._print_plan(plan)
            if not plan.ok:
                raise CommandError('Preflight failed')
            return
        if action == 'load':
            if options['slot'] == options['in_place']:
                raise CommandError('Pass exactly one of --slot or --in-place')
            journal = restorer.load(
                options['set'], strategy=RestoreStrategyChoices.SLOT if options['slot'] else
                RestoreStrategyChoices.IN_PLACE, target=options.get('target'),
                from_instance=options.get('from_instance'), components=options.get('components'),
                confirm=options.get('confirm'), skip_safety=options['skip_safety'],
                headroom_confirmed=options['headroom_confirmed'], revoke_resurrected=options['revoke_resurrected'],
            )
        elif action == 'finish':
            journal = restorer.finish(options.get('restore_id'), qa=options['qa'],
                                      revoke_resurrected=options['revoke_resurrected'])
        elif action == 'rollback':
            journal = restorer.rollback(options.get('restore_id'), confirm=options.get('confirm'))
        elif action == 'finalize':
            journal = restorer.finalize(options.get('restore_id'), force=options['force'])
        elif action == 'media':
            journal = restorer.restore_media(options['set'], target=options.get('target'),
                                             components=options['components'])
        else:  # status
            journals = journal_mod.list_journals()
            if options.get('restore_id'):
                journals = [j for j in journals if j.restore_id == options['restore_id'].upper()]
            if options.get('json'):
                self.stdout.write(json.dumps([j.data for j in journals], indent=2, default=str))
                return
            for j in journals[:10]:
                last = j['steps'][-1]['step'] if j['steps'] else '-'
                self.stdout.write(f"{j.restore_id}  {j['strategy']:<9} set {j['set_id']}  {j['status']:<12} "
                                  f"last step: {last}")
                for warning in j.get('warnings', []):
                    self.stdout.write(self.style.WARNING(f"    ! {warning}"))
            return
        self.stdout.write(self.style.SUCCESS(f"Restore {journal.restore_id}: {journal['status']}"))
        for warning in journal.get('warnings', []):
            self.stdout.write(self.style.WARNING(f"  ! {warning}"))

    def _print_plan(self, plan):
        self.stdout.write(f"Set {plan.set_id} from {plan.header.get('created_at', '')[:19]} "
                          f"(NetBox {plan.header.get('netbox_version')}), {human_size(plan.size_bytes)} on "
                          f"{plan.target}")
        self.stdout.write(f"Recommended strategy: {plan.strategy} - {plan.reason}")
        if plan.data_loss:
            self.stdout.write(f"Data loss: {plan.data_loss}")
        for c in plan.checks:
            style = {'abort': self.style.ERROR, 'warn': self.style.WARNING}.get(c['result'], self.style.SUCCESS)
            self.stdout.write(style(f"  [{c['result'].upper():<5}] {c['check']}: {c['detail']}"))
        flag = '--slot' if plan.strategy == RestoreStrategyChoices.SLOT else \
            f'--in-place --confirm {plan.live_database}'
        if plan.ok:
            self.stdout.write(f"Next: nbbackup restore load {plan.set_id} {flag}")

    # Maintenance

    def do_catalog(self, options):
        from ...engine import catalog
        stats = catalog.sync(log=logger)
        self.stdout.write(json.dumps(stats))

    def do_target(self, options):
        from ...engine import catalog
        from ...models import StorageTarget
        from ...storage import get_backend
        config = conf.get_config()
        catalog.sync_static(config)
        names = [options['name']] if options.get('name') else list(config.targets)
        failed = False
        for name in names:
            report = get_backend(config.target(name)).probe(under=config.instance_id)
            StorageTarget.objects.filter(name=name).update(last_probe=report.as_dict(), last_probe_at=timezone.now())
            self.stdout.write(f"{name}: {'OK' if report.ok else 'FAILED'}")
            for c in report.checks:
                mark = 'ok' if c['ok'] else ('FAIL' if c['required'] else 'warn')
                self.stdout.write(f"  [{mark:<4}] {c['check']}" + (f": {c['detail']}" if c['detail'] else ''))
            failed = failed or not report.ok
        if failed:
            raise CommandError('Probe failed')

    def do_keys(self, options):
        from ...core.gpg import generate_key
        passphrase = os.environ.get(options['passphrase_env'], '') if options.get('passphrase_env') else ''
        config = conf.get_config()
        fpr, public, secret = generate_key(options['name'], gpg_binary=config.crypto.gpg_binary,
                                           passphrase=passphrase)
        os.makedirs(options['out'], mode=0o700, exist_ok=True)
        for suffix, data, mode in (('pub.asc', public, 0o644), ('sec.asc', secret, 0o600)):
            path = os.path.join(options['out'], f'{fpr}.{suffix}')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            self.stdout.write(f"wrote {path}")
        self.stdout.write(self.style.SUCCESS(f"fingerprint {fpr}"))

    def do_status(self, options):
        from ...engine import health
        summary = health.summary(check_workers=not options['no_workers'])
        if options['json']:
            self.stdout.write(json.dumps(summary, indent=2, default=str))
        else:
            for c in summary['conditions']:
                style = {'error': self.style.ERROR, 'warning': self.style.WARNING}.get(c['status'], str)
                subject = f" [{c['subject']}]" if c['subject'] else ''
                self.stdout.write(style(f"{c['status'].upper():<8} {c['name']}{subject}: {c['message']}"))
        sys.exit({'ok': 0, 'info': 0, 'warning': 1, 'error': 2}[summary['status']])
