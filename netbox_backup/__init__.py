__version__ = '0.2.0.dev0'

try:
    from netbox.plugins import PluginConfig
except ImportError:  # Standalone CLI: the core and storage drivers work without NetBox
    PluginConfig = None


if PluginConfig is not None:

    class NetBoxBackupConfig(PluginConfig):
        name = 'netbox_backup'
        verbose_name = 'Backup'
        description = 'Scheduled, encrypted, verified backups and disaster recovery for NetBox'
        version = __version__
        author = 'netbox-backup contributors'
        base_url = 'backup'
        min_version = '4.6.0'
        max_version = '4.7.99'
        queues = ['netbox_backup']
        # Everything is static configuration; see docs/configuration.md
        default_settings = {}

        def ready(self):
            super().ready()
            # Never touches the database: restore commands run against an empty or unmigrated one
            from . import jobs  # noqa: F401  registers the scheduler system job
            from .models import register_actions
            register_actions()

    config = NetBoxBackupConfig
