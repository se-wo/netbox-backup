"""
PostgreSQL helpers: connection parameters, consistent snapshots, row counts and pg_dump/pg_restore wrappers.
"""
import contextlib
import os
import re
import shutil
import subprocess

import psycopg
from django.conf import settings
from psycopg import sql

from .. import conf

# libpq parameters that may be supplied via DATABASES[...]['OPTIONS'] and must be passed to pg_dump/pg_restore too
LIBPQ_OPTIONS = (
    'sslmode', 'sslrootcert', 'sslcert', 'sslkey', 'sslpassword', 'sslcrl', 'target_session_attrs',
    'connect_timeout', 'service', 'passfile', 'gssencmode', 'channel_binding',
)


class PostgresError(Exception):
    pass


def connection_params(database='default', dbname=None):
    db = settings.DATABASES[database]
    params = {
        'dbname': dbname or db.get('NAME'),
        'user': db.get('USER') or None,
        'host': db.get('HOST') or None,
        'port': str(db['PORT']) if db.get('PORT') else None,
    }
    for key, value in (db.get('OPTIONS') or {}).items():
        if key in LIBPQ_OPTIONS and value is not None:
            params[key] = str(value)
    return {k: v for k, v in params.items() if v}, db.get('PASSWORD') or None


def conninfo(dbname=None):
    params, _ = connection_params(dbname=dbname)
    return psycopg.conninfo.make_conninfo(**params)


def connect(dbname=None, autocommit=True):
    params, password = connection_params(dbname=dbname)
    return psycopg.connect(password=password, autocommit=autocommit, **params)


def subprocess_env():
    _, password = connection_params()
    env = dict(os.environ)
    if password:
        env['PGPASSWORD'] = password
    return env


def binary(name):
    path = conf.get(name)
    resolved = shutil.which(path)
    if not resolved:
        raise PostgresError(
            f"PostgreSQL client '{path}' not found. Install the PostgreSQL client package matching your "
            f"server's major version, or set PLUGINS_CONFIG['netbox_backup']['{name}']."
        )
    return resolved


def client_major_version(name='pg_dump'):
    output = subprocess.run([binary(name), '--version'], capture_output=True, text=True, check=True).stdout
    match = re.search(r'(\d+)(?:\.(\d+))?', output)
    return int(match.group(1)) if match else None


def server_version(conn):
    return conn.info.server_version  # e.g. 160004


def check_client_compatibility(conn, name='pg_dump'):
    """
    pg_dump refuses to dump a server newer than itself. Detect this up front with a clear message.
    """
    client = client_major_version(name)
    server = server_version(conn) // 10000
    if client is not None and client < server:
        raise PostgresError(
            f"{name} major version {client} is older than the PostgreSQL server ({server}). "
            f"Install the PostgreSQL {server} client tools or point '{name}' at a newer binary."
        )
    return client, server


def list_tables(cursor):
    cursor.execute("""
        SELECT c.relname
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') AND NOT c.relispartition
        ORDER BY c.relname
    """)
    return [row[0] for row in cursor.fetchall()]


def row_counts(cursor):
    counts = {}
    for table in list_tables(cursor):
        cursor.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(table)))
        counts[table] = cursor.fetchone()[0]
    return counts


def applied_migrations(cursor):
    cursor.execute("SELECT to_regclass('django_migrations')")
    if cursor.fetchone()[0] is None:
        return []
    cursor.execute('SELECT app, name FROM django_migrations ORDER BY app, name')
    return [f'{app}.{name}' for app, name in cursor.fetchall()]


@contextlib.contextmanager
def exported_snapshot():
    """
    Open a REPEATABLE READ transaction and export its snapshot. While the context is open, pg_dump can be pointed
    at the snapshot with --snapshot so that the dump and the row counts we record describe exactly the same state.
    Yields (snapshot_id, cursor).
    """
    with connect(autocommit=False) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        with conn.cursor() as cursor:
            cursor.execute('SELECT pg_export_snapshot()')
            snapshot_id = cursor.fetchone()[0]
            yield snapshot_id, cursor, conn
        conn.rollback()


def run(args, logger=None, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, env=subprocess_env(), **kwargs)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise PostgresError(f"{os.path.basename(args[0])} exited with status {result.returncode}: {stderr}")
    if logger and result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            logger.warning(line)
    return result


def dump(path, snapshot_id=None, logger=None):
    args = [binary('pg_dump'), '--format=custom', '--no-password', f'--file={path}']
    if snapshot_id:
        args.append(f'--snapshot={snapshot_id}')
    args += list(conf.get('pg_dump_extra_args') or [])
    args.append(f'--dbname={conninfo()}')
    run(args, logger=logger)


def dump_toc(path):
    """
    Return the table of contents of a custom-format dump (pg_restore --list), parsed into (type, name) tuples.
    """
    result = run([binary('pg_restore'), '--list', path])
    entries = []
    for line in result.stdout.splitlines():
        if not line or line.startswith(';'):
            continue
        # e.g. "3541; 0 16385 TABLE DATA public dcim_site netbox"
        match = re.match(r'^\d+;\s+\d+\s+\d+\s+(.+?)\s+(\S+)\s+(\S+)\s+(\S+)\s*$', line)
        if match:
            entries.append((match.group(1), match.group(3)))
    return entries


def restore(path, dbname=None, jobs=1, logger=None):
    args = [
        binary('pg_restore'), '--no-password', '--exit-on-error', '--no-owner', '--no-privileges',
        f'--jobs={jobs}', f'--dbname={conninfo(dbname=dbname)}', path,
    ]
    run(args, logger=logger)


def other_connections(conn):
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT pid, usename, application_name, client_addr::text, state
            FROM pg_stat_activity
            WHERE datname = current_database() AND pid <> pg_backend_pid() AND backend_type = 'client backend'
        """)
        return cursor.fetchall()


def terminate_other_connections(conn):
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT pg_terminate_backend(pid) FROM pg_stat_activity
            WHERE datname = current_database() AND pid <> pg_backend_pid() AND backend_type = 'client backend'
        """)


def wipe_schema(conn):
    """
    Remove every object from the current schema so the dump can be restored into a clean database. Tries
    DROP SCHEMA first and falls back to dropping objects owned by the connecting role when the role does not own
    the schema (PostgreSQL < 15 default).
    """
    with conn.cursor() as cursor:
        cursor.execute('SELECT current_schema(), current_user')
        schema, user = cursor.fetchone()
        try:
            with conn.transaction():
                cursor.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
                cursor.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION {}').format(
                    sql.Identifier(schema), sql.Identifier(user)
                ))
        except psycopg.errors.InsufficientPrivilege:
            with conn.transaction():
                cursor.execute('DROP OWNED BY CURRENT_USER CASCADE')


def create_database(conn, name):
    with conn.cursor() as cursor:
        cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))


def drop_database(conn, name):
    with conn.cursor() as cursor:
        cursor.execute(sql.SQL('DROP DATABASE IF EXISTS {}').format(sql.Identifier(name)))
