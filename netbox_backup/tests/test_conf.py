from datetime import timedelta

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from netbox_backup import conf


class ConfTest(SimpleTestCase):

    def test_parse_duration(self):
        self.assertEqual(conf.parse_duration('30 days'), timedelta(days=30))
        self.assertEqual(conf.parse_duration('1 month'), timedelta(days=30))
        self.assertEqual(conf.parse_duration('2w'), timedelta(weeks=2))
        self.assertEqual(conf.parse_duration('12h'), timedelta(hours=12))
        self.assertEqual(conf.parse_duration('1 year'), timedelta(days=365))
        self.assertEqual(conf.parse_duration(7), timedelta(days=7))
        self.assertIsNone(conf.parse_duration(None))
        with self.assertRaises(ImproperlyConfigured):
            conf.parse_duration('forever')
        with self.assertRaises(ImproperlyConfigured):
            conf.parse_duration('3 fortnights')

    def test_instance_id_derived_from_secret_key(self):
        with override_settings(SECRET_KEY='a' * 50, PLUGINS_CONFIG={'netbox_backup': {}}):
            first = conf.instance_id()
        with override_settings(SECRET_KEY='b' * 50, PLUGINS_CONFIG={'netbox_backup': {}}):
            second = conf.instance_id()
        self.assertTrue(first.startswith('netbox-'))
        self.assertNotEqual(first, second)

    def test_instance_id_explicit_and_validated(self):
        with override_settings(PLUGINS_CONFIG={'netbox_backup': {'instance_id': 'prod-dc1'}}):
            self.assertEqual(conf.instance_id(), 'prod-dc1')
        with override_settings(PLUGINS_CONFIG={'netbox_backup': {'instance_id': '../escape'}}):
            with self.assertRaises(ImproperlyConfigured):
                conf.instance_id()

    def test_include_configuration_auto(self):
        with override_settings(PLUGINS_CONFIG={'netbox_backup': {'include_configuration': 'auto'}}):
            self.assertFalse(conf.include_configuration())
        with override_settings(PLUGINS_CONFIG={'netbox_backup': {'include_configuration': 'auto',
                                                                 'encryption_key': 'k'}}):
            self.assertTrue(conf.include_configuration())

    def test_stale_after_defaults_to_twice_shortest_interval(self):
        with override_settings(PLUGINS_CONFIG={'netbox_backup': {'database_backup_interval': 60,
                                                                 'full_backup_interval': 1440}}):
            self.assertEqual(conf.stale_after(), timedelta(minutes=120))
        with override_settings(PLUGINS_CONFIG={'netbox_backup': {'database_backup_interval': None,
                                                                 'full_backup_interval': None}}):
            self.assertIsNone(conf.stale_after())
