import shutil
import tempfile

from django.conf import settings
from django.test import override_settings


class PluginConfigMixin:
    """
    Give each test its own storage and work directories, with optional plugin config overrides.
    """
    plugin_config = {}

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='netbox-backup-test-')
        self.storage_path = f'{self.tmp}/storage'
        self.configure()

    def configure(self, **overrides):
        config = {
            'storage': {'backend': 'local', 'path': self.storage_path},
            'work_dir': f'{self.tmp}/work',
            **self.plugin_config,
            **overrides,
        }
        if getattr(self, '_override', None) is None:
            self._base_config = dict(settings.PLUGINS_CONFIG.get('netbox_backup', {}))
            self._override = override_settings(PLUGINS_CONFIG={**settings.PLUGINS_CONFIG, 'netbox_backup': {}})
            self._override.enable()
        # Mutate in place: re-creating the override would unwind any override_settings enabled after it
        plugin_config = settings.PLUGINS_CONFIG['netbox_backup']
        plugin_config.clear()
        plugin_config.update({**self._base_config, **config})

    def tearDown(self):
        self._override.disable()
        self._override = None
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()
