"""
PostgreSQL helpers that need no Django: connections, the baseline, dump and restore commands, slot markers.

Passwords reach subprocesses only through a 0600 .pgpass in a private temporary directory, never argv or the
environment. The plugin never needs superuser rights: everything here works as the database owner.
"""
import contextlib
import os
import re
import shutil
import subprocess
import tempfile
import threading

import psycopg
from psycopg import sql

SLOT_MARKER = 'nbb:slot'

# libpq parameters passed through from DATABASES[...]['OPTIONS']
LIBPQ_OPTIONS = (
    'sslmode', 'sslrootcert', 'sslcert', 'sslkey', 'sslcrl', 'target_session_attrs', 'connect_timeout', 'service',
    'gssencmode', 'channel_binding', 'application_name',
)

# Settings pg_restore emits unconditionally but older servers reject, keyed by the first server major that knows
# them: pg_restore 17+ sets transaction_timeout.
UNSUPPORTED_SETTINGS = {'transaction_timeout': 17}


class PostgresError(Exception):
    pass


class Database:
    """
    Where and how to reach one PostgreSQL database, plus the client binaries to use.
    """

    def __init__(self, params, password=None, pg_dump='pg_dump', pg_restore='pg_restore', psql='psql'):
        self.params = {k: str(v) for k, v in params.items() if v not in (None, '')}
        self.password = password
        self.binaries = {'pg_dump': pg_dump, 'pg_restore': pg_restore, 'psql': psql}

    @classmethod
    def from_dsn(cls, dsn, **binaries):
        params = psycopg.conninfo.conninfo_to_dict(dsn)
        password = params.pop('password', None) or os.environ.get('PGPASSWORD')
        return cls(params, password=password, **binaries)

    @property
    def name(self):
        return self.params.get('dbname')

    def with_name(self, dbname):
        return Database({**self.params, 'dbname': dbname}, self.password, **self.binaries)

    def conninfo(self):
        return psycopg.conninfo.make_conninfo(**self.params)

    def connect(self, autocommit=True, **kwargs):
        return psycopg.connect(password=self.password, autocommit=autocommit, **self.params, **kwargs)

    def binary(self, name):
        path = shutil.which(self.binaries[name])
        if not path:
            raise PostgresError(
                f"PostgreSQL client {self.binaries[name]!r} not found. Install the newest postgresql-client from "
                f"the PostgreSQL apt repository."
            )
        return path

    def client_major(self, name):
        output = subprocess.run([self.binary(name), '--version'], capture_output=True, text=True, check=True).stdout
        match = re.search(r'(\d+)(?:\.\d+)?', output)
        return int(match.group(1)) if match else None

    @contextlib.contextmanager
    def environment(self):
        """
        Environment for client subprocesses, with the password in a private .pgpass.
        """
        env = {k: v for k, v in os.environ.items() if not k.startswith('PG')}
        directory = tempfile.mkdtemp(prefix='nbb-pgpass-', dir='/dev/shm' if os.path.isdir('/dev/shm') else None)
        try:
            if self.password:
                path = os.path.join(directory, 'pgpass')
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                escaped = self.password.replace('\\', '\\\\').replace(':', '\\:')
                with os.fdopen(fd, 'w') as f:
                    f.write(f'*:*:*:*:{escaped}\n')
                env['PGPASSFILE'] = path
            yield env
        finally:
            shutil.rmtree(directory, ignore_errors=True)


def server_major(conn):
    return conn.info.server_version // 10000


def check_dump_client(db, conn):
    client = db.client_major('pg_dump')
    server = server_major(conn)
    if client is not None and client < server:
        raise PostgresError(
            f"pg_dump {client} is older than the PostgreSQL server ({server}); install the newest postgresql-client"
        )
    return client


def dump_args(db, snapshot=None, compression=None):
    """
    pg_dump in custom format to stdout. Grants are included; owners are dropped at restore time instead.
    """
    args = [db.binary('pg_dump'), '--format=custom', '--no-password']
    if compression:
        args.append(f'--compress={compression}')
    if snapshot:
        args.append(f'--snapshot={snapshot}')
    args.append(f'--dbname={db.conninfo()}')
    return args


def default_compression(client_major):
    # zstd needs pg_dump 16+
    return 'zstd:3' if client_major and client_major >= 16 else None


@contextlib.contextmanager
def exported_snapshot(db):
    """
    A REPEATABLE READ READ ONLY transaction with an exported snapshot, kept open until the context exits so that
    pg_dump --snapshot and every query on the yielded cursor see exactly the same state.
    """
    with db.connect(autocommit=False) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        with conn.cursor() as cursor:
            cursor.execute('SELECT pg_export_snapshot()')
            snapshot = cursor.fetchone()[0]
            yield snapshot, cursor, conn
        conn.rollback()


# Schemas and the baseline

SYSTEM_SCHEMAS = ('pg_catalog', 'information_schema', 'pg_toast')


def user_schemas(cursor):
    """
    Non-system schemas of the current database, with their owners: {schema: owner}.
    """
    cursor.execute("""
        SELECT n.nspname, pg_get_userbyid(n.nspowner)
        FROM pg_namespace n
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_'
        ORDER BY n.nspname
    """)
    return dict(cursor.fetchall())


def owned_schemas(cursor):
    """
    Schemas the current role may empty: those it owns, plus public when owned by pg_database_owner and the role
    owns the database (PostgreSQL 15+ default).
    """
    cursor.execute("""
        SELECT n.nspname FROM pg_namespace n
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_'
          AND (pg_has_role(current_user, n.nspowner, 'USAGE')
               AND (pg_get_userbyid(n.nspowner) = current_user
                    OR (pg_get_userbyid(n.nspowner) = 'pg_database_owner'
                        AND (SELECT pg_get_userbyid(datdba) FROM pg_database
                             WHERE datname = current_database()) = current_user)))
        ORDER BY n.nspname
    """)
    return [row[0] for row in cursor.fetchall()]


def baseline(cursor):
    """
    Row count per table, plus per-schema counts of indexes, constraints and triggers, and the newest last_updated
    of every table that has one. Taken with raw SQL inside the dump's snapshot.
    """
    cursor.execute("""
        SELECT n.nspname, c.relname,
               EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'last_updated'
                       AND NOT a.attisdropped)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
          AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_'
        ORDER BY 1, 2
    """)
    tables = cursor.fetchall()
    counts = {}
    newest = None
    for schema, table, has_last_updated in tables:
        ident = sql.Identifier(schema, table)
        if has_last_updated:
            cursor.execute(sql.SQL('SELECT count(*), max(last_updated) FROM {}').format(ident))
            count, last = cursor.fetchone()
            if last is not None and (newest is None or last > newest):
                newest = last
        else:
            cursor.execute(sql.SQL('SELECT count(*) FROM {}').format(ident))
            count = cursor.fetchone()[0]
        counts[f'{schema}.{table}'] = count
    cursor.execute("""
        SELECT n.nspname,
               (SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                WHERE c.relnamespace = n.oid),
               (SELECT count(*) FROM pg_constraint k WHERE k.connamespace = n.oid),
               (SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
                WHERE c.relnamespace = n.oid AND NOT t.tgisinternal)
        FROM pg_namespace n
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_'
        ORDER BY 1
    """)
    schemas = {name: {'indexes': i, 'constraints': k, 'triggers': t} for name, i, k, t in cursor.fetchall()}
    return {
        'tables': counts,
        'schemas': schemas,
        'rows': sum(counts.values()),
        'newest_last_updated': newest.isoformat() if newest else None,
    }


def compare_baseline(expected, actual):
    """
    Differences between two baselines: {'tables': {name: (expected, actual)}, 'schemas': {...}}. Missing tables
    count as None.
    """
    result = {'tables': {}, 'schemas': {}}
    exp_tables, act_tables = expected.get('tables', {}), actual.get('tables', {})
    for name in sorted(set(exp_tables) | set(act_tables)):
        if exp_tables.get(name) != act_tables.get(name):
            result['tables'][name] = (exp_tables.get(name), act_tables.get(name))
    exp_schemas, act_schemas = expected.get('schemas', {}), actual.get('schemas', {})
    for name in sorted(set(exp_schemas) | set(act_schemas)):
        if exp_schemas.get(name) != act_schemas.get(name):
            result['schemas'][name] = (exp_schemas.get(name), act_schemas.get(name))
    return result if result['tables'] or result['schemas'] else {}


def format_differences(diff, limit=10):
    items = [f'{name}: {a} -> {b}' for name, (a, b) in diff.get('tables', {}).items()]
    items += [f'schema {name}: {a} -> {b}' for name, (a, b) in diff.get('schemas', {}).items()]
    more = f' (+{len(items) - limit} more)' if len(items) > limit else ''
    return ', '.join(items[:limit]) + more


def grantees(cursor):
    """
    Roles other than the current one that hold privileges on objects in the database's user schemas.
    """
    cursor.execute("""
        SELECT DISTINCT grantee FROM information_schema.role_table_grants
        WHERE grantee NOT IN (current_user, 'PUBLIC') AND table_schema NOT IN ('pg_catalog', 'information_schema')
    """)
    return sorted(row[0] for row in cursor.fetchall())


def missing_roles(cursor, roles):
    if not roles:
        return []
    cursor.execute('SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)', [list(roles)])
    present = {row[0] for row in cursor.fetchall()}
    return sorted(set(roles) - present)


# The dump check: pg_restore reads the plaintext stream and we count every row it emits

class RowCountTap:
    """
    Receives the plaintext custom-format dump while it streams, renders it with pg_restore --data-only and counts
    COPY rows per table. finish() fails unless pg_restore read the whole archive and the counts equal the expected
    baseline, which proves the dump contains the data, not just its table of contents.
    """
    COPY_RE = re.compile(rb'^COPY (\S+) ')

    def __init__(self, db, expected_tables):
        self.expected = expected_tables
        self.counts = {}
        self.stderr = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            [db.binary('pg_restore'), '--data-only', '--file=-'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
        )
        self.broken = None
        self.reader = threading.Thread(target=self._count, daemon=True, name='nbb-dumpcheck')
        self.reader.start()

    @staticmethod
    def _unquote(name):
        parts = re.findall(r'"((?:[^"]|"")*)"|([^.]+)', name)
        return '.'.join(q.replace('""', '"') if q else p for q, p in parts)

    def _count(self):
        table = None
        rows = 0
        for line in self.proc.stdout:
            if table is None:
                match = self.COPY_RE.match(line)
                if match:
                    table, rows = self._unquote(match.group(1).decode()), 0
            elif line == b'\\.\n':
                self.counts[table] = self.counts.get(table, 0) + rows
                table = None
            else:
                rows += 1

    def write(self, chunk):
        if self.broken:
            return
        try:
            self.proc.stdin.write(chunk)
        except (BrokenPipeError, OSError) as e:
            self.broken = e

    def finish(self):
        with contextlib.suppress(OSError):
            self.proc.stdin.close()
        code = self.proc.wait()
        self.reader.join()
        self.stderr.seek(0)
        message = self.stderr.read().decode(errors='replace').strip()
        self.stderr.close()
        if code != 0 or self.broken:
            raise PostgresError(f"Dump check: pg_restore could not read the dump: {message or self.broken}")
        counted = {name: self.counts.get(name, 0) for name in self.expected}
        diff = {n: (self.expected[n], counted[n]) for n in self.expected if self.expected[n] != counted[n]}
        if diff:
            raise PostgresError(
                "Dump check: the dump's rows differ from the snapshot's counts: "
                + format_differences({'tables': diff})
            )

    def abort(self):
        with contextlib.suppress(Exception):
            self.proc.kill()
            self.proc.wait()
            self.reader.join(timeout=10)
            self.stderr.close()


# Sessions, slot markers, emptying and loading

def other_sessions(conn, dbname=None):
    """
    Client sessions on the database other than this connection: [(pid, user, application, address, state)].
    """
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT pid, usename, application_name, client_addr::text, state
            FROM pg_stat_activity
            WHERE datname = coalesce(%s, current_database()) AND pid <> pg_backend_pid()
              AND backend_type = 'client backend'
        """, [dbname])
        return cursor.fetchall()


def database_comment(conn, dbname):
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT shobj_description(oid, 'pg_database'), pg_get_userbyid(datdba)
            FROM pg_database WHERE datname = %s
        """, [dbname])
        row = cursor.fetchone()
    if row is None:
        raise PostgresError(f"Database {dbname!r} does not exist")
    return row[0], row[1]


def has_slot_marker(conn, dbname):
    return (database_comment(conn, dbname)[0] or '').strip() == SLOT_MARKER


def set_slot_marker(conn, dbname, marked):
    with conn.cursor() as cursor:
        cursor.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(
            sql.Identifier(dbname), sql.Literal(SLOT_MARKER) if marked else sql.SQL('NULL'),
        ))


def is_empty(conn):
    """
    True if the database holds no user tables, views or sequences.
    """
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_'
        """)
        return cursor.fetchone()[0] == 0


def foreign_owned(cursor):
    """
    Schemas and extensions in the database that the current role does not own and so cannot drop:
    {'schemas': [...], 'extensions': [...]}.
    """
    owned = set(owned_schemas(cursor))
    schemas = sorted(s for s in user_schemas(cursor) if s not in owned)
    cursor.execute("""
        SELECT e.extname FROM pg_extension e
        WHERE e.extname <> 'plpgsql' AND pg_get_userbyid(e.extowner) <> current_user
    """)
    return {'schemas': schemas, 'extensions': sorted(row[0] for row in cursor.fetchall())}


def lock_capacity(cursor):
    """
    (relations owned by the current role, the server's lock table capacity).
    """
    cursor.execute("""
        SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE pg_get_userbyid(c.relowner) = current_user
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')),
               current_setting('max_locks_per_transaction')::int * current_setting('max_connections')::int
    """)
    return cursor.fetchone()


def empty_database(conn, lock_timeout='10s'):
    """
    Return the database to the state of a fresh one: drop public and every other schema the role owns, recreate
    public. One transaction with a lock timeout. Only call on a marked slot or the live database under --in-place.
    """
    with conn.cursor() as cursor:
        cursor.execute('SELECT current_user')
        user = cursor.fetchone()[0]
        with conn.transaction():
            cursor.execute(sql.SQL('SET LOCAL lock_timeout = {}').format(sql.Literal(lock_timeout)))
            for schema in owned_schemas(cursor):
                cursor.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            cursor.execute("SELECT to_regnamespace('public') IS NULL")
            if cursor.fetchone()[0]:
                cursor.execute(sql.SQL('CREATE SCHEMA public AUTHORIZATION {}').format(sql.Identifier(user)))
                cursor.execute('GRANT USAGE ON SCHEMA public TO PUBLIC')


def restore_args(db, path, jobs=1, no_privileges=False):
    args = [db.binary('pg_restore'), '--no-password', '--exit-on-error', '--no-owner', f'--jobs={jobs}',
            f'--dbname={db.conninfo()}']
    if no_privileges:
        args.append('--no-privileges')
    args.append(path)
    return args


def load_dump(db, path, jobs=1, no_privileges=False, log=None):
    """
    pg_restore a custom-format file into db; any non-zero exit fails. Falls back to pg_restore | psql when the client
    is newer than the server and would emit settings the server rejects.
    """
    with db.connect() as conn:
        server = server_major(conn)
    client = db.client_major('pg_restore') or 0
    strip = {name for name, since in UNSUPPORTED_SETTINGS.items() if client >= since > server}
    with db.environment() as env:
        if strip:
            _load_via_psql(db, path, strip, no_privileges, env, log)
            return
        result = subprocess.run(restore_args(db, path, jobs, no_privileges), capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise PostgresError(f"pg_restore exited with status {result.returncode}: {result.stderr.strip()[-2000:]}")
    if log and result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            log.warning(line)


def _load_via_psql(db, path, strip, no_privileges, env, log):
    if log:
        log.info(f"pg_restore is newer than the server; loading through psql without {', '.join(sorted(strip))}")
    pattern = re.compile(rb'^SET (%s) = ' % b'|'.join(re.escape(s.encode()) for s in sorted(strip)))
    producer_args = [db.binary('pg_restore'), '--no-owner', '--file=-', path]
    if no_privileges:
        producer_args.insert(2, '--no-privileges')
    with tempfile.TemporaryFile() as producer_err, tempfile.TemporaryFile() as consumer_err:
        producer = subprocess.Popen(producer_args, stdout=subprocess.PIPE, stderr=producer_err, env=env)
        consumer = subprocess.Popen(
            [db.binary('psql'), '--no-password', '--no-psqlrc', '--quiet', '--set=ON_ERROR_STOP=1',
             '--single-transaction', '--output=/dev/null', f'--dbname={db.conninfo()}'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=consumer_err, env=env,
        )
        try:
            for line in producer.stdout:
                if not pattern.match(line):
                    consumer.stdin.write(line)
            consumer.stdin.close()
        except BrokenPipeError:
            pass
        finally:
            with contextlib.suppress(BrokenPipeError):
                consumer.stdin.close()
            producer.stdout.close()
            if consumer.wait() != 0:
                producer.kill()
            producer.wait()
        for proc, err, name in ((consumer, consumer_err, 'psql'), (producer, producer_err, 'pg_restore')):
            if proc.returncode != 0:
                err.seek(0)
                raise PostgresError(f"{name} exited with status {proc.returncode}: "
                                    f"{err.read().decode(errors='replace').strip()[-2000:]}")


def analyze(conn):
    with conn.cursor() as cursor:
        cursor.execute('ANALYZE')
