"""
Backup verification. A backup that "completed" is not a backup that restores: these checks prove it.

    quick    every artifact is present in storage with the recorded size
    deep     quick + download, checksum, decrypt, dump table of contents covers every non-empty table,
             file archives are readable
    restore  deep + restore the dump into a scratch database and compare per-table row counts
"""
import contextlib
import logging
import secrets
import tempfile

from django.conf import settings
from django.utils import timezone

from .. import conf
from ..choices import VerificationLevelChoices, VerificationStatusChoices
from ..storage import get_storage
from . import catalog, files, pg
from .backup import storage_key

logger = logging.getLogger('netbox_backup')


class Verifier:
    def __init__(self, manifest, log=None, storage=None):
        self.manifest = manifest
        self.log = log or logger
        self.storage = storage or get_storage()
        self.results = []

    def record(self, check, ok, detail=''):
        self.results.append({'check': check, 'ok': bool(ok), 'detail': detail})
        suffix = f': {detail}' if detail else ''
        (self.log.info if ok else self.log.error)(f"[{'PASS' if ok else 'FAIL'}] {check}{suffix}")
        return ok

    @property
    def passed(self):
        return bool(self.results) and all(r['ok'] for r in self.results)

    def run(self, level=VerificationLevelChoices.LEVEL_QUICK):
        try:
            self.quick()
            if level in (VerificationLevelChoices.LEVEL_DEEP, VerificationLevelChoices.LEVEL_RESTORE) and self.passed:
                with tempfile.TemporaryDirectory(prefix='netbox-backup-verify-', dir=conf.work_dir()) as workdir:
                    paths = self.deep(workdir)
                    if level == VerificationLevelChoices.LEVEL_RESTORE and self.passed:
                        self.test_restore(paths['database'])
        except Exception as e:
            self.record('verification', False, f'{type(e).__name__}: {e}')
        return self.passed

    def quick(self):
        m = self.manifest
        try:
            catalog.check_format(m)
            self.record('manifest format', True, m['format'])
        except Exception as e:
            self.record('manifest format', False, str(e))
            return
        self.record('database artifact present', any(a['type'] == 'database' for a in m.get('artifacts', [])))
        tables = m.get('database', {}).get('tables', 0)
        rows = m.get('database', {}).get('rows', 0)
        self.record('database is not empty', tables > 0, f"{tables} tables, {rows} rows")
        for artifact in m.get('artifacts', []):
            key = storage_key(m['instance_id'], m['name'], artifact['name'])
            if not self.storage.exists(key):
                self.record(f"{artifact['name']} in storage", False, 'object not found')
                continue
            size = self.storage.size(key)
            self.record(f"{artifact['name']} in storage", size == artifact['size'], f"{size} bytes")

    def deep(self, workdir):
        paths = catalog.fetch_artifacts(self.manifest, workdir, storage=self.storage, log=self.log)
        self.record('download, checksums and decryption', True, f"{len(paths)} artifacts")
        toc = pg.dump_toc(paths['database'])
        with_data = {name for entry_type, name in toc if entry_type == 'TABLE DATA'}
        counts = self.manifest.get('database', {}).get('row_counts', {})
        missing = sorted(t for t, n in counts.items() if n and t not in with_data)
        self.record(
            'dump contains data for every non-empty table', not missing,
            f"missing: {', '.join(missing[:10])}" if missing else f"{len(with_data)} tables with data",
        )
        for artifact in self.manifest.get('artifacts', []):
            if artifact['type'] in ('database',):
                continue
            members = files.list_archive(paths[artifact['type']])
            # A tree that had files must not come back as an empty archive
            ok = not artifact.get('files') or len(members) > 0
            self.record(f"{artifact['type']} archive readable", ok, f"{len(members)} entries")
        return paths

    def test_restore(self, dump_path):
        scratch = f"{settings.DATABASES['default']['NAME']}_nbbk_verify_{secrets.token_hex(3)}"
        with pg.connect() as admin:
            try:
                pg.create_database(admin, scratch)
            except Exception as e:
                self.record('test restore', False, f"cannot create scratch database (needs CREATEDB): {e}")
                return
            try:
                pg.restore(dump_path, dbname=scratch, logger=self.log)
                with pg.connect(dbname=scratch) as conn, conn.cursor() as cursor:
                    restored = pg.row_counts(cursor)
                expected = self.manifest.get('database', {}).get('row_counts', {})
                mismatches = compare_row_counts(expected, restored)
                self.record(
                    'test restore row counts match', not mismatches,
                    format_mismatches(mismatches) if mismatches else f"{len(expected)} tables, "
                                                                     f"{sum(expected.values())} rows",
                )
            finally:
                with contextlib.suppress(Exception):
                    pg.drop_database(admin, scratch)


def compare_row_counts(expected, actual):
    """
    Return {table: {'expected': n, 'actual': m}} for every table whose count differs (None = table absent).
    """
    mismatches = {}
    for table in sorted(set(expected) | set(actual)):
        if expected.get(table) != actual.get(table):
            mismatches[table] = {'expected': expected.get(table), 'actual': actual.get(table)}
    return mismatches


def format_mismatches(mismatches, limit=10):
    parts = [f"{t}: expected {v['expected']}, found {v['actual']}" for t, v in list(mismatches.items())[:limit]]
    if len(mismatches) > limit:
        parts.append(f'... and {len(mismatches) - limit} more')
    return '; '.join(parts)


def verify_backup(backup, level=VerificationLevelChoices.LEVEL_QUICK, log=None):
    """
    Verify a Backup row against storage and record the outcome on it.
    """
    log = log or logger
    storage = get_storage()
    manifest = catalog.read_manifest(storage, backup.instance_id, backup.name) or backup.manifest
    verifier = Verifier(manifest, log=log, storage=storage)
    passed = verifier.run(level)
    backup.verification_status = (
        VerificationStatusChoices.STATUS_PASSED if passed else VerificationStatusChoices.STATUS_FAILED
    )
    backup.verification_level = level
    backup.verified_at = timezone.now()
    backup.verification_log = verifier.results
    backup.save()
    return passed, verifier.results


def cleanup_scratch_databases():
    """
    Drop scratch databases left behind by an interrupted test restore.
    """
    prefix = f"{settings.DATABASES['default']['NAME']}_nbbk_verify_"
    with pg.connect() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", [prefix.replace('_', r'\_') + '%'])
        for (name,) in cursor.fetchall():
            pg.drop_database(conn, name)

