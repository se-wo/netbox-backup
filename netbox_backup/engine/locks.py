"""
One lock, one writer: a PostgreSQL session-level advisory lock per policy (and one for restores). The lock lives
on its own connection, so it is released the moment the worker dies; no unlock command is needed after a crash.
"""
import contextlib
import zlib

from .. import conf

NAMESPACE = 0x4E4242  # "NBB"


class LockHeld(Exception):
    pass


def lock_key(name):
    return zlib.crc32(name.encode()) & 0x7FFFFFFF


@contextlib.contextmanager
def advisory_lock(name, db=None):
    """
    Hold the named lock for the duration of the block, on a dedicated connection. Raises LockHeld if another
    session has it.
    """
    db = db or conf.database()
    conn = db.connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(%s, %s)', [NAMESPACE, lock_key(name)])
            if not cursor.fetchone()[0]:
                raise LockHeld(f"{name} is locked by another job")
        yield conn
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
