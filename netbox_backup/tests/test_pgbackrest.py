from datetime import UTC, datetime, timedelta
from unittest import mock

from django.test import SimpleTestCase, override_settings

from netbox_backup.engine import pgbackrest

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def stanza(stop=NOW - timedelta(hours=3), archive=True, code=0):
    return {
        'name': 'netbox',
        'status': {'code': code, 'message': 'ok' if not code else 'missing stanza path'},
        'backup': [{'label': '20261025-090000F', 'type': 'full', 'timestamp': {'start': 0,
                                                                              'stop': int(stop.timestamp())}}],
        'archive': [{'min': '000000010000000000000001', 'max': '000000010000000000000042'}] if archive else [],
    }


class PgBackRestTest(SimpleTestCase):

    def test_healthy(self):
        status, message = pgbackrest.summarize(stanza(), timedelta(days=1), now=NOW)
        self.assertEqual(status, 'ok')
        self.assertIn('000000010000000000000042', message)

    def test_old_backup(self):
        status, _ = pgbackrest.summarize(stanza(stop=NOW - timedelta(days=3)), timedelta(days=1), now=NOW)
        self.assertEqual(status, 'warning')

    def test_no_wal(self):
        status, message = pgbackrest.summarize(stanza(archive=False), timedelta(days=1), now=NOW)
        self.assertEqual(status, 'warning')
        self.assertIn('point-in-time recovery unavailable', message)

    def test_stanza_error(self):
        status, _ = pgbackrest.summarize(stanza(code=1), timedelta(days=1), now=NOW)
        self.assertEqual(status, 'error')

    @override_settings(PLUGINS_CONFIG={'netbox_backup': {}})
    def test_disabled_by_default(self):
        self.assertIsNone(pgbackrest.condition())

    @override_settings(PLUGINS_CONFIG={'netbox_backup': {'pgbackrest': {'stanza': 'netbox',
                                                                        'binary': 'definitely-missing'}}})
    def test_missing_binary_reported(self):
        condition = pgbackrest.condition()
        self.assertEqual(condition.status, 'error')
        self.assertIn('not found', condition.message)

    @override_settings(PLUGINS_CONFIG={'netbox_backup': {'pgbackrest': {'stanza': 'netbox'}}})
    def test_condition_uses_info(self):
        with mock.patch.object(pgbackrest, 'info', return_value=stanza(stop=datetime.now(UTC))):
            self.assertEqual(pgbackrest.condition().status, 'ok')
