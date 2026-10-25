from netbox.plugins import PluginConfig

__version__ = '0.1.0'


class NetBoxBackupConfig(PluginConfig):
    name = 'netbox_backup'
    verbose_name = 'Backup & Recovery'
    description = 'Scheduled, verified, encrypted backups and disaster recovery for NetBox'
    version = __version__
    author = 'netbox-backup contributors'
    base_url = 'backup'
    min_version = '4.4.0'
    default_settings = {
        # Unique identifier of this NetBox installation. Backups are stored beneath it, and restores refuse
        # backups taken by a different instance unless explicitly overridden. Derived from SECRET_KEY if unset.
        'instance_id': None,
        # Where backups are kept. See docs/configuration.md for all options.
        'storage': {
            'backend': 'local',
            'path': None,  # Defaults to <NetBox root>/backups
        },
        # Database-only backups (minutes between runs). On by default; set to None to disable.
        'database_backup_interval': 1440,
        # Full backups: database, media, scripts, reports and (optionally) configuration. Off by default.
        'full_backup_interval': None,
        # Retention: how long a backup is kept, e.g. "30 days", "2 weeks", "1 month". Default 1 month.
        'retention': '1 month',
        # Never prune the N most recent successful backups of each kind, regardless of age.
        'keep_last': 3,
        # Passphrase used to encrypt every artifact (AES-256-GCM). Requires the "cryptography" package.
        'encryption_key': None,
        # File trees included in full backups.
        'include_files': ['media', 'scripts', 'reports'],
        # Include configuration.py (contains SECRET_KEY, API_TOKEN_PEPPERS, DB credentials) in full backups.
        # "auto" includes it only when backups are encrypted.
        'include_configuration': 'auto',
        # Additional files to bundle with the configuration (e.g. LDAP config, TLS material).
        'extra_configuration_files': [],
        # Run a quick verification (checksums + dump table of contents) after each backup.
        'verify_after_backup': True,
        # Minutes after which the newest successful backup is reported as stale. Default: 2x the shortest interval.
        'stale_after': None,
        # Housekeeping (retention pruning and catalog sync) interval in minutes.
        'housekeeping_interval': 60,
        # Location for temporary files while building or restoring a backup. Defaults to the system temp dir.
        'work_dir': None,
        # PostgreSQL client binaries. Must be the same major version as (or newer than) the server.
        'pg_dump': 'pg_dump',
        'pg_restore': 'pg_restore',
        # Used only when pg_restore is newer than the server (see docs/troubleshooting.md).
        'psql': 'psql',
        'pg_dump_extra_args': [],
        # gzip level for file archives (0-9).
        'compression_level': 6,
        # Optional: monitor an externally managed pgBackRest stanza (WAL archiving / point-in-time recovery).
        # e.g. {'stanza': 'netbox', 'binary': 'pgbackrest', 'max_age': '2 days'}
        'pgbackrest': None,
    }

    def ready(self):
        super().ready()
        from . import jobs  # noqa: F401
        jobs.register_system_jobs()


config = NetBoxBackupConfig
