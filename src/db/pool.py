"""Thread-safe bounded pooling and explicit transaction ownership."""
import os
import threading
from contextlib import contextmanager

import psycopg2
from psycopg2.pool import ThreadedConnectionPool, PoolError

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "memory_bank"),
    "user": os.environ.get("DB_USER", "memory_bank"),
    "password": os.environ.get("DB_PASSWORD", "memory_bank"),
}
_pool = None
_slots = None
_init_lock = threading.Lock()


def _initialize():
    global _pool, _slots
    with _init_lock:
        if _pool is None:
            maximum = int(os.environ.get("DB_POOL_MAX", "5"))
            minimum = min(int(os.environ.get("DB_POOL_MIN", "2")), maximum)
            if not 1 <= minimum <= maximum:
                raise ValueError("DB_POOL_MIN/MAX must be positive and ordered")
            config = dict(DB_CONFIG)
            config.setdefault("connect_timeout", int(os.environ.get("DB_CONNECT_TIMEOUT", "5")))
            _pool = ThreadedConnectionPool(minimum, maximum, **config)
            _slots = threading.BoundedSemaphore(maximum)
        return _pool, _slots


@contextmanager
def get_connection():
    """Borrow one connection, bounded by DB_POOL_TIMEOUT seconds.

    Callers explicitly commit writes. Read transactions and uncommitted changes
    are rolled back before return, so no transaction leaks to the next borrower.
    A failed rollback discards the connection. Pool initialization is serialized.
    """
    pool, slots = _initialize()
    if not slots.acquire(timeout=float(os.environ.get("DB_POOL_TIMEOUT", "5"))):
        raise PoolError("Timed out waiting for a database connection")
    conn = None
    discard = False
    try:
        for _ in range(pool.maxconn + 1):
            candidate = pool.getconn()
            if candidate.closed:
                pool.putconn(candidate, close=True)
                continue
            try:
                with candidate.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = %s", (int(os.environ.get("DB_HEALTHCHECK_TIMEOUT_MS", "2000")),))
                    cur.execute("SELECT 1")
                candidate.rollback()
            except psycopg2.Error:
                pool.putconn(candidate, close=True)
                continue
            conn = candidate
            break
        if conn is None:
            raise psycopg2.OperationalError("No live database connection available")
        yield conn
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except psycopg2.Error:
                discard = True
            pool.putconn(conn, close=discard or bool(conn.closed))
        slots.release()


#: Per-transaction timeouts (milliseconds; 0 disables), all overridable by env.
#:
#: ``default`` -- every ordinary writer (capture ingest, dream-cycle storage,
#: MCP memory_create/update/relate, express feedback): a statement that runs
#: longer than ``DB_STATEMENT_TIMEOUT_MS`` (default 30 s) or waits for a lock
#: longer than ``DB_LOCK_TIMEOUT_MS`` (default 5 s) fails and the whole
#: transaction rolls back. Ingest prepares all embeddings *before* opening the
#: transaction, so even a 1.3 M-char document is a few hundred fast inserts.
#:
#: ``maintenance`` -- corpus repairs (dedup / rechunk / metadata manifests,
#: purge of superseded chunks), which legitimately scan JSONB columns for
#: references and rewrite many rows: no statement timeout
#: (``DB_MAINTENANCE_STATEMENT_TIMEOUT_MS`` default 0) and a longer lock wait
#: (``DB_MAINTENANCE_LOCK_TIMEOUT_MS`` default 60 s). Schema migrations do not
#: go through this pool at all (psql via migrations/migrate.sh; 016 sets its own
#: ``lock_timeout``).
TIMEOUT_PROFILES = {
    "default": ("DB_STATEMENT_TIMEOUT_MS", 30000, "DB_LOCK_TIMEOUT_MS", 5000),
    "maintenance": ("DB_MAINTENANCE_STATEMENT_TIMEOUT_MS", 0, "DB_MAINTENANCE_LOCK_TIMEOUT_MS", 60000),
}


def _timeout_ms(env_name: str, default: int) -> int:
    value = int(os.environ.get(env_name, str(default)))
    if value < 0:
        raise ValueError(f"{env_name} must be >= 0 (0 disables the timeout)")
    return value


@contextmanager
def transaction(*, timeouts: str = "default"):
    """One connection, one commit; all statements roll back on any failure.

    ``timeouts`` names a :data:`TIMEOUT_PROFILES` entry (``"default"`` for
    ordinary writers, ``"maintenance"`` for long legitimate repairs).
    """
    if timeouts not in TIMEOUT_PROFILES:
        raise ValueError(f"Unknown transaction timeout profile {timeouts!r}")
    statement_env, statement_default, lock_env, lock_default = TIMEOUT_PROFILES[timeouts]
    with get_connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = %s", (_timeout_ms(lock_env, lock_default),))
                cur.execute("SET LOCAL statement_timeout = %s", (_timeout_ms(statement_env, statement_default),))
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


@contextmanager
def connection_scope(conn=None, *, write=False):
    """Use a supplied connection without committing, or own a new scope."""
    if conn is not None:
        yield conn
    else:
        with (transaction() if write else get_connection()) as owned:
            yield owned


def close_pool():
    """Close an idle pool; callers must finish borrowed connections first."""
    global _pool, _slots
    with _init_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None
            _slots = None


def is_reachable():
    try:
        with get_connection():
            return True
    except (psycopg2.Error, PoolError):
        return False
