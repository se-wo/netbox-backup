import shutil
import tempfile

from django.conf import settings
from django.test import override_settings

from .test_core import KeyMaterial

INSTANCE_ID = '0b1d3c5e-7f90-4a1b-8c2d-3e4f5a6b7c8d'


class PluginConfigMixin:
    """
    Gives each test a local target in a private directory, generated keys and a default policy. Override
    plugin_config or call configure(**overrides) to change PLUGINS_CONFIG['netbox_backup'].
    """
    plugin_config = {}

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='netbox-backup-test-')
        self.storage_path = f'{self.tmp}/storage'
        self._override = None
        self.configure()

    def crypto_config(self, identities=('ops',)):
        keys = KeyMaterial.get()
        return {
            'public_keys': f"file:{keys['bundle']}",
            'recipients': [keys['ops']['fpr']],
            'recovery_key': keys['recovery']['fpr'],
            'signing_key': keys['signing']['fpr'],
            'signing_secret_key': f"file:{keys['signing']['sec']}",
            'identities': [f"file:{keys[i]['sec']}" for i in identities],
        }

    def configure(self, **overrides):
        config = {
            'instance_id': INSTANCE_ID,
            'targets': {'primary': {'backend': 'local', 'path': self.storage_path, 'allow_prune': True}},
            'crypto': self.crypto_config(),
            'spool_dir': f'{self.tmp}/spool',
            **self.plugin_config,
            **overrides,
        }
        if self._override is not None:
            self._override.disable()
        self._override = override_settings(PLUGINS_CONFIG={**settings.PLUGINS_CONFIG, 'netbox_backup': config})
        self._override.enable()

    def tearDown(self):
        if self._override is not None:
            self._override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()
