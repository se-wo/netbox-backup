"""
Static configuration: targets, policies, crypto and restore slots.

Everything that decides where backups go and who can read them lives in configuration.py (PLUGINS_CONFIG) or, for
the standalone CLI, in an equivalent JSON file. Nothing here touches Django, so a disaster-recovery host can read
the same file before any database exists.
"""
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

__all__ = (
    'Config', 'ConfigError', 'CryptoConfig', 'PolicyConfig', 'RetentionConfig', 'TargetConfig', 'parse_duration',
    'resolve_secret',
)

COMPONENTS = ('database', 'media', 'scripts', 'config')

SCHEDULE_PRESETS = {
    'hourly': '0 * * * *',
    'daily': '0 1 * * *',
    'weekly': '0 1 * * 0',
}

# Option names that hold secrets. They may only appear inside a target's "credentials" or "prune_credentials",
# and only as references.
SECRET_OPTION_NAMES = {
    'access_key', 'secret_key', 'session_token', 'account_key', 'sas_token', 'connection_string', 'password',
    'client_secret', 'key', 'service_account_json',
}

_UNITS = {
    's': 'seconds', 'sec': 'seconds', 'second': 'seconds', 'seconds': 'seconds',
    'm': 'minutes', 'min': 'minutes', 'mins': 'minutes', 'minute': 'minutes', 'minutes': 'minutes',
    'h': 'hours', 'hr': 'hours', 'hour': 'hours', 'hours': 'hours',
    'd': 'days', 'day': 'days', 'days': 'days',
    'w': 'weeks', 'week': 'weeks', 'weeks': 'weeks',
}


class ConfigError(ValueError):
    pass


def parse_duration(value, default_unit='hours'):
    """
    Parse a duration: an int or float (in default_unit), a timedelta, or a string such as "24h", "30 days", "90m".
    """
    if value is None or value == '':
        return None
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool):
        raise ConfigError(f"Cannot parse duration {value!r}")
    if isinstance(value, (int, float)):
        return timedelta(**{default_unit: value})
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)\s*', str(value))
    if not match or match.group(2).lower() not in _UNITS:
        raise ConfigError(f"Cannot parse duration {value!r} (try e.g. '24h' or '30 days')")
    return timedelta(**{_UNITS[match.group(2).lower()]: float(match.group(1))})


def is_reference(value):
    return isinstance(value, str) and value.startswith(('env:', 'file:'))


def resolve_secret(ref):
    """
    Resolve a secret reference: "env:NAME" reads an environment variable, "file:/path" reads a file (trailing
    newline stripped). Literal values are refused, so secrets never end up in configuration.py or the database.
    """
    if ref is None:
        return None
    if not is_reference(ref):
        raise ConfigError("Secrets must be references ('env:NAME' or 'file:/path'), not literal values")
    kind, _, name = ref.partition(':')
    if kind == 'env':
        if name not in os.environ:
            raise ConfigError(f"Environment variable {name} (from {ref!r}) is not set")
        return os.environ[name]
    try:
        with open(name, encoding='utf-8') as f:
            return f.read().rstrip('\n')
    except OSError as e:
        raise ConfigError(f"Cannot read secret file {name}: {e.strerror}") from e


def read_reference_bytes(ref):
    """
    Like resolve_secret, for binary material such as key files. A plain path is accepted for public material.
    """
    if is_reference(ref):
        kind, _, name = ref.partition(':')
        if kind == 'env':
            return resolve_secret(ref).encode()
        ref = name
    with open(ref, 'rb') as f:
        return f.read()


@dataclass(frozen=True)
class RetentionConfig:
    keep_last: int = 7
    keep_daily: int = 14
    keep_weekly: int = 8
    keep_monthly: int = 12
    keep_yearly: int = 0
    min_age: timedelta = timedelta(hours=24)
    keep_min_verified: int = 1

    @classmethod
    def from_dict(cls, data, base=None):
        base = base or cls()
        data = dict(data or {})
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"Unknown retention option(s): {', '.join(sorted(unknown))}")
        values = {}
        for name in cls.__dataclass_fields__:
            if name not in data:
                values[name] = getattr(base, name)
            elif name == 'min_age':
                values[name] = parse_duration(data[name]) or timedelta(0)
            else:
                value = data[name]
                if type(value) is not int or value < 0:
                    raise ConfigError(f"Retention option {name} must be a non-negative integer")
                values[name] = value
        return cls(**values)

    def as_dict(self):
        return {
            'keep_last': self.keep_last, 'keep_daily': self.keep_daily, 'keep_weekly': self.keep_weekly,
            'keep_monthly': self.keep_monthly, 'keep_yearly': self.keep_yearly,
            'min_age_seconds': int(self.min_age.total_seconds()), 'keep_min_verified': self.keep_min_verified,
        }


@dataclass(frozen=True)
class TargetConfig:
    name: str
    backend: str
    prefix: str = ''
    options: dict = field(default_factory=dict)
    credentials: dict = field(default_factory=dict)
    prune_credentials: dict = field(default_factory=dict)
    retention_days: int = 0
    quota_bytes: int | None = None
    offsite: bool = False
    retention: RetentionConfig | None = None

    @classmethod
    def from_dict(cls, name, data):
        data = dict(data or {})
        backend = data.pop('backend', None)
        if not backend:
            raise ConfigError(f"Target {name!r}: 'backend' is required")
        credentials = data.pop('credentials', None) or {}
        prune_credentials = data.pop('prune_credentials', None) or {}
        for label, creds in (('credentials', credentials), ('prune_credentials', prune_credentials)):
            for key, value in creds.items():
                if value is not None and not is_reference(value):
                    raise ConfigError(
                        f"Target {name!r}: {label}.{key} must be a reference ('env:NAME' or 'file:/path'), "
                        f"not a literal secret"
                    )
        misplaced = sorted(k for k in data if k in SECRET_OPTION_NAMES)
        if misplaced:
            raise ConfigError(
                f"Target {name!r}: {', '.join(misplaced)} must go inside 'credentials', as references"
            )
        retention_days = data.pop('retention_days', 0) or 0
        if type(retention_days) is not int or retention_days < 0:
            raise ConfigError(f"Target {name!r}: retention_days must be a non-negative integer")
        quota = data.pop('quota_bytes', None)
        if quota is not None and (type(quota) is not int or quota <= 0):
            raise ConfigError(f"Target {name!r}: quota_bytes must be a positive integer")
        retention = data.pop('retention', None)
        prefix = str(data.pop('prefix', '') or '').strip('/')
        offsite = bool(data.pop('offsite', backend not in ('local',)))
        return cls(
            name=name, backend=backend, prefix=prefix, options=data, credentials=credentials,
            prune_credentials=prune_credentials, retention_days=retention_days, quota_bytes=quota,
            offsite=offsite, retention=RetentionConfig.from_dict(retention) if retention is not None else None,
        )

    def public_dict(self):
        """
        Description without secrets: references are kept (they name a secret, they are not one).
        """
        return {
            'backend': self.backend, 'prefix': self.prefix, 'options': dict(self.options),
            'credentials': dict(self.credentials), 'retention_days': self.retention_days,
            'quota_bytes': self.quota_bytes, 'offsite': self.offsite,
            'can_prune': bool(self.prune_credentials) or self.options.get('allow_prune', False),
        }


@dataclass(frozen=True)
class PolicyConfig:
    name: str
    target: str
    components: tuple = COMPONENTS
    schedule: str = SCHEDULE_PRESETS['daily']
    time_zone: str | None = None
    replicas: tuple = ()
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    zstd_level: int = 3
    max_runtime: timedelta = timedelta(hours=4)
    verify_schedule: str | None = None
    enabled: bool = True

    @classmethod
    def from_dict(cls, name, data, targets):
        data = dict(data or {})
        allowed = set(cls.__dataclass_fields__) - {'name'}
        unknown = set(data) - allowed
        if unknown:
            raise ConfigError(f"Policy {name!r}: unknown option(s) {', '.join(sorted(unknown))}")
        target = data.get('target')
        if target is None and len(targets) == 1:
            target = next(iter(targets))
        if target not in targets:
            raise ConfigError(f"Policy {name!r}: target {target!r} is not a configured target")
        replicas = tuple(data.get('replicas') or ())
        for replica in replicas:
            if replica not in targets or replica == target:
                raise ConfigError(f"Policy {name!r}: replica {replica!r} is not a configured target")
        components = tuple(data.get('components') or COMPONENTS)
        bad = [c for c in components if c not in COMPONENTS]
        if bad:
            raise ConfigError(f"Policy {name!r}: unknown component(s) {', '.join(bad)}")
        schedule = SCHEDULE_PRESETS.get(data.get('schedule') or 'daily', data.get('schedule'))
        if schedule is not None and len(str(schedule).split()) != 5:
            raise ConfigError(f"Policy {name!r}: schedule must be a five-field cron expression or a preset")
        zstd_level = data.get('zstd_level', 3)
        if type(zstd_level) is not int or not 1 <= zstd_level <= 19:
            raise ConfigError(f"Policy {name!r}: zstd_level must be an integer between 1 and 19")
        retention = RetentionConfig.from_dict(data.get('retention'), base=targets[target].retention)
        return cls(
            name=name, target=target, components=components, schedule=schedule,
            time_zone=data.get('time_zone'), replicas=replicas, retention=retention, zstd_level=zstd_level,
            max_runtime=parse_duration(data.get('max_runtime', '4h')), verify_schedule=data.get('verify_schedule'),
            enabled=bool(data.get('enabled', True)),
        )


@dataclass(frozen=True)
class CryptoConfig:
    gpg_binary: str = 'gpg'
    compliance: str | None = None
    public_keys: str | None = None
    recipients: tuple = ()
    signing_key: str | None = None
    signing_secret_key: str | None = None
    signing_passphrase: str | None = None
    identities: tuple = ()
    identity_passphrase: str | None = None
    # Extra fingerprints accepted when verifying (e.g. a QA instance's report key, or a rotated signing key)
    trusted_signers: tuple = ()

    @classmethod
    def from_dict(cls, data):
        data = dict(data or {})
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"crypto: unknown option(s) {', '.join(sorted(unknown))}")
        for key in ('signing_secret_key', 'signing_passphrase', 'identity_passphrase'):
            if data.get(key) is not None and not is_reference(data[key]):
                raise ConfigError(f"crypto.{key} must be a reference ('env:NAME' or 'file:/path')")
        identities = tuple(data.get('identities') or ())
        for ref in identities:
            if not is_reference(ref):
                raise ConfigError("crypto.identities must be references ('env:NAME' or 'file:/path')")
        compliance = data.get('compliance')
        if compliance not in (None, 'de-vs'):
            raise ConfigError("crypto.compliance must be None or 'de-vs'")
        recipients = tuple(_fingerprint(f, 'crypto.recipients') for f in data.get('recipients') or ())
        signing_key = _fingerprint(data['signing_key'], 'crypto.signing_key') if data.get('signing_key') else None
        return cls(
            gpg_binary=data.get('gpg_binary') or 'gpg', compliance=compliance, public_keys=data.get('public_keys'),
            recipients=recipients, signing_key=signing_key, signing_secret_key=data.get('signing_secret_key'),
            signing_passphrase=data.get('signing_passphrase'), identities=identities,
            identity_passphrase=data.get('identity_passphrase'),
            trusted_signers=tuple(_fingerprint(f, 'crypto.trusted_signers') for f in data.get('trusted_signers') or ()),
        )

    @property
    def configured(self):
        return bool(self.public_keys and self.recipients and self.signing_key)


def _fingerprint(value, label):
    fpr = re.sub(r'\s+', '', str(value)).upper()
    if not re.fullmatch(r'[0-9A-F]{40}|[0-9A-F]{64}', fpr):
        raise ConfigError(f"{label}: {value!r} is not a full OpenPGP fingerprint (40 or 64 hex digits)")
    return fpr


@dataclass(frozen=True)
class Config:
    instance_id: str
    instance_id_pinned: bool
    targets: dict
    policies: dict
    crypto: CryptoConfig
    databases: tuple = ()
    spool_dir: str | None = None
    pg_dump: str = 'pg_dump'
    pg_restore: str = 'pg_restore'
    psql: str = 'psql'
    restore_jobs: int = 4
    rollback_days: int = 2
    allow_download: bool = False
    verify_dump: bool = True
    pgbackrest: dict | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_dict(cls, data, fallback_instance_seed=None):
        data = dict(data or {})
        instance_id = data.get('instance_id')
        pinned = bool(instance_id)
        if instance_id:
            try:
                instance_id = str(uuid.UUID(str(instance_id)))
            except ValueError as e:
                raise ConfigError("instance_id must be a UUID (generate one with 'uuidgen')") from e
        elif fallback_instance_seed:
            # Stable without configuration, but tied to the seed (NetBox's SECRET_KEY); health warns until pinned.
            instance_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'netbox-backup:{fallback_instance_seed}'))
        else:
            raise ConfigError("instance_id is required")

        targets = {}
        for name, target in (data.get('targets') or {}).items():
            _check_name(name, 'target')
            targets[name] = TargetConfig.from_dict(name, target)

        policies = {}
        raw_policies = data.get('policies')
        if raw_policies is None and len(targets) == 1:
            # One target and no policies: the install default, a daily full set at 01:00
            raw_policies = {'default': {}}
        for name, policy in (raw_policies or {}).items():
            _check_name(name, 'policy')
            policies[name] = PolicyConfig.from_dict(name, policy, targets)

        databases = tuple(data.get('databases') or ())
        if databases and len(databases) != 2:
            raise ConfigError("databases must list exactly two database names (live and restore slot)")
        restore_jobs = data.get('restore_jobs', 4)
        if type(restore_jobs) is not int or restore_jobs < 1:
            raise ConfigError("restore_jobs must be a positive integer")
        return cls(
            instance_id=instance_id, instance_id_pinned=pinned, targets=targets, policies=policies,
            crypto=CryptoConfig.from_dict(data.get('crypto')), databases=databases,
            spool_dir=data.get('spool_dir'), pg_dump=data.get('pg_dump') or 'pg_dump',
            pg_restore=data.get('pg_restore') or 'pg_restore', psql=data.get('psql') or 'psql',
            restore_jobs=restore_jobs, rollback_days=int(data.get('rollback_days', 2)),
            allow_download=bool(data.get('allow_download', False)), verify_dump=bool(data.get('verify_dump', True)),
            pgbackrest=data.get('pgbackrest'), raw=data,
        )

    def target(self, name):
        try:
            return self.targets[name]
        except KeyError:
            raise ConfigError(f"Unknown target {name!r}") from None

    def policy(self, name):
        try:
            return self.policies[name]
        except KeyError:
            raise ConfigError(f"Unknown policy {name!r}") from None


def _check_name(name, kind):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', str(name)):
        raise ConfigError(f"Invalid {kind} name {name!r}: use letters, digits, '.', '_' and '-'")
