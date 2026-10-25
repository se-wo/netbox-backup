import io
import json

from dcim.models import Site
from django.core.management import CommandError, call_command
from django.test import TransactionTestCase

from netbox_backup.models import Backup, RestoreEvent

from .utils import PluginConfigMixin


class CommandTest(PluginConfigMixin, TransactionTestCase):

    def run_command(self, *args):
        out = io.StringIO()
        try:
            call_command('netbox_backup', *args, stdout=out, stderr=out)
        except SystemExit as e:
            return out.getvalue(), e.code
        return out.getvalue(), 0

    def test_lifecycle(self):
        Site.objects.create(name='One', slug='one')
        output, _ = self.run_command('create', '--verify', 'deep')
        self.assertIn('Created', output)
        name = Backup.objects.get().name

        output, _ = self.run_command('list', '--json')
        self.assertEqual(json.loads(output)[0]['name'], name)

        output, _ = self.run_command('show', 'latest')
        self.assertIn(name, output)

        output, _ = self.run_command('restore', 'latest', '--dry-run')
        self.assertIn('Preflight checks passed', output)
        self.assertFalse(RestoreEvent.objects.exists())

        Site.objects.create(name='Two', slug='two')
        output, _ = self.run_command('restore', name, '--yes', '--no-safety-backup')
        self.assertIn('Restore of', output)
        self.assertEqual(list(Site.objects.values_list('slug', flat=True)), ['one'])

        output, code = self.run_command('status', '--json')
        self.assertIn(json.loads(output)['status'], ('ok', 'info', 'warning', 'error'))
        self.assertIn(code, (0, 1, 2))

    def test_restore_unknown_backup(self):
        with self.assertRaises(CommandError):
            call_command('netbox_backup', 'restore', 'nope', '--yes', stdout=io.StringIO())

    def test_restore_requires_confirmation_without_tty(self):
        self.run_command('create')
        with self.assertRaisesRegex(CommandError, '--yes'):
            call_command('netbox_backup', 'restore', 'latest', stdout=io.StringIO())

    def test_genkey(self):
        output, _ = self.run_command('genkey')
        self.assertGreaterEqual(len(output.strip()), 40)
