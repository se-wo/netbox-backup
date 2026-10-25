"""
One lock, one writer: a PostgreSQL session-level advisory lock per policy. The lock lives on its own direct
connection (never through a transaction-mode pooler) with TCP keepalives, so it is released the moment the worker
dies; no unlock command is needed after a crash.
"""
import contextlib
import time
import zlib

from .. import conf
from ..core.pg import pooled_session

NAMESPACE = 0x4E4242  # "NBB"
KEEPALIVES = {'keepalives': 1, 'keepalives_idle': 30, 'keepalives_interval': 10, 'keepalives_count': 3}


class LockHeld(Exception):
    pass


class LockLost(Exception):
    pass


class PooledConnection(Exception):
    pass


def lock_key(name):
    return zlib.crc32(name.encode()) & 0x7FFFFFFF


def connect(db=None):
    """
    A dedicated session for locks; refuses a connection that goes through a transaction-mode pooler.
    """
    db = db or conf.database()
    conn = db.connect(**KEEPALIVES)
    reason = pooled_session(conn)
    if reason:
        conn.close()
        raise PooledConnection(
            f"{reason}. Advisory locks and snapshots need a direct session: set 'direct_connection' "
            f"(e.g. {{'port': 5432}}) in the plugin configuration."
        )
    return conn


def try_lock(conn, name):
    with conn.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s, %s)', [NAMESPACE, lock_key(name)])
        return cursor.fetchone()[0]


@contextlib.contextmanager
def advisory_lock(name, db=None):
    """
    Hold the named lock for the duration of the block, on a dedicated connection. Raises LockHeld if another
    session has it.
    """
    conn = connect(db)
    try:
        if not try_lock(conn, name):
            raise LockHeld(f"{name} is locked by another job")
        yield conn
    finally:
        conn.close()


def assert_held(conn, name):
    """
    Raise LockLost unless this connection still holds the lock (checked right before a set becomes visible).
    """
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
                "AND classid = %s AND objid = %s AND granted)", [NAMESPACE, lock_key(name)],
            )
            held = cursor.fetchone()[0]
    except Exception as e:
        raise LockLost(f"{name}: the lock connection is gone ({e})") from e
    if not held:
        raise LockLost(f"{name}: this job no longer holds the lock")


def holders(conn, name):
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND objid = %s AND granted",
            [NAMESPACE, lock_key(name)],
        )
        return [row[0] for row in cursor.fetchall()]


def reclaim(name, db=None, wait=5.0):
    """
    Free a lock whose job overran: terminate the sessions holding it (the NetBox role may terminate its own) and
    wait briefly for the lock to become free. Returns True if the lock was free or is now free.
    """
    conn = connect(db)
    try:
        if try_lock(conn, name):
            return True
        with conn.cursor() as cursor:
            for pid in holders(conn, name):
                cursor.execute('SELECT pg_terminate_backend(%s)', [pid])
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if try_lock(conn, name):
                return True
            time.sleep(0.2)
        return False
    finally:
        conn.close()


def is_locked(name, db=None):
    db = db or conf.database()
    with db.connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND objid = %s",
            [NAMESPACE, lock_key(name)],
        )
        return cursor.fetchone()[0] > 0
