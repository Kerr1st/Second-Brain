"""Shared test fixtures for the Second Brain test suite.

Provides:
- test_db: session-scoped fixture that creates/migrates the memory_bank_test database
- clean_tables: function-scoped fixture that truncates test tables between tests
- mock_embedding: deterministic 1024-dim embedding from input hash (no Bedrock calls)
- sample_memory_factory: factory fixture for creating memories with known content
"""

import hashlib
import math
import os
import subprocess
import socket
from pathlib import Path

import psycopg2
import pytest
from psycopg2 import sql

import src.db as db
from src.db import close_pool


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    """Classify by the shared database dependency; explicit markers handle exceptions.

    Fixture closure includes indirect dependencies, so mixed modules remain selectable
    without duplicating markers on every database test. Run before pytest's -m filter.
    """
    for item in items:
        requires_db = "test_db" in item.fixturenames
        integration = requires_db or item.get_closest_marker("integration") is not None
        if integration and item.get_closest_marker("behavior") is not None:
            raise pytest.UsageError(f"{item.nodeid}: behavior test requires integration")
        item.add_marker(pytest.mark.integration if integration else pytest.mark.behavior)


@pytest.fixture(autouse=True)
def isolated_services(request, monkeypatch):
    """No accidental HTTP/model calls; behavior tests also cannot open PostgreSQL.

    PostgreSQL's native driver does not use Python sockets. Integration tests retain
    that access through the guarded fixture; tests of external adapters mock calls.
    """
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must replace external network services at the boundary")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    if request.node.get_closest_marker("integration") is None:
        monkeypatch.setattr(psycopg2, "connect", forbidden)


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------

TEST_DB_NAME = os.environ.get("TEST_DB_NAME", "memory_bank_test")

# Connection params for the admin database (used to CREATE/DROP the test DB)
_ADMIN_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": "postgres",
    "user": os.environ.get("DB_USER", "memory_bank"),
    "password": os.environ.get("DB_PASSWORD", "memory_bank"),
}


def _admin_connection():
    """Connect to the default 'postgres' database for admin operations."""
    conn = psycopg2.connect(**_ADMIN_CONFIG)
    conn.autocommit = True
    return conn


def _require_disposable_test_database(database: str) -> None:
    allowed = {"memory_bank_test", "second_brain_codex_test"}
    if database not in allowed:
        raise RuntimeError(
            f"refusing to recreate database {database!r}; expected one of "
            f"{', '.join(sorted(allowed))}"
        )


def _create_test_db():
    """Recreate the isolated test database for a clean migration baseline."""
    _require_disposable_test_database(TEST_DB_NAME)
    conn = _admin_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (TEST_DB_NAME,),
            )
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(TEST_DB_NAME)
                )
            )
            cur.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB_NAME))
            )
    finally:
        conn.close()


def _apply_migrations():
    """Apply pending migrations through the production migration runner."""
    repo_root = Path(__file__).resolve().parents[1]
    runner = repo_root / "migrations" / "migrate.sh"
    env = os.environ.copy()
    env.update(
        {
            "DB_HOST": str(db.DB_CONFIG["host"]),
            "DB_PORT": str(db.DB_CONFIG["port"]),
            "DB_NAME": str(db.DB_CONFIG["dbname"]),
            "DB_USER": str(db.DB_CONFIG["user"]),
            "DB_PASSWORD": str(db.DB_CONFIG["password"]),
            "PGPASSWORD": str(db.DB_CONFIG["password"]),
        }
    )
    result = subprocess.run(
        ["bash", str(runner)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "test database migration failed:\n"
            f"{result.stdout}{result.stderr}"
        )


@pytest.fixture(scope="session")
def test_db():
    """Session-scoped fixture: recreate test DB, apply migrations, override DB_CONFIG.

    Yields the overridden DB_CONFIG dict. On teardown, restores the original config.
    """
    # Save original config
    original_config = db.DB_CONFIG.copy()

    # Create the test database
    _create_test_db()

    # Override DB_CONFIG to point at the test database
    db.DB_CONFIG.update({
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": TEST_DB_NAME,
        "user": os.environ.get("DB_USER", "memory_bank"),
        "password": os.environ.get("DB_PASSWORD", "memory_bank"),
    })

    try:
        close_pool()
        _apply_migrations()
        yield db.DB_CONFIG
    finally:
        close_pool()
        db.DB_CONFIG.update(original_config)


@pytest.fixture()
def db_connection(test_db, clean_tables):
    """Autocommit connection to the same guarded database used by application code."""
    connection = psycopg2.connect(**test_db)
    connection.autocommit = True
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def clean_tables(test_db):
    """Function-scoped fixture: truncate memories and memory_relationships between tests."""
    yield
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM context_receipts")
            cur.execute("DELETE FROM dream_cycle_candidates")
            cur.execute("DELETE FROM dream_cycle_runs")
            cur.execute("DELETE FROM memory_relationships")
            cur.execute("DELETE FROM memories WHERE parent_id IS NOT NULL")
            cur.execute("DELETE FROM memories")
        conn.commit()


# ---------------------------------------------------------------------------
# Embedding mock
# ---------------------------------------------------------------------------

def _deterministic_embedding(text: str) -> list[float]:
    """Generate a deterministic 1024-dim vector from the SHA-256 hash of the input.

    The hash bytes are cycled to fill 1024 dimensions, then each byte is mapped
    to a float in [-1, 1]. The resulting vector is L2-normalized so it behaves
    like a real embedding for cosine similarity.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()  # 32 bytes
    raw = []
    for i in range(1024):
        byte_val = digest[i % len(digest)]
        # XOR with dimension index for more variation across dimensions
        mixed = (byte_val ^ (i & 0xFF)) & 0xFF
        raw.append((mixed / 127.5) - 1.0)  # map to [-1, 1]

    # L2 normalize
    norm = math.sqrt(sum(x * x for x in raw))
    if norm > 0:
        raw = [x / norm for x in raw]
    return raw


@pytest.fixture()
def mock_embedding():
    """Fixture returning a callable that produces deterministic 1024-dim embeddings.

    Usage in tests:
        embedding = mock_embedding("some text")
    """
    return _deterministic_embedding


# ---------------------------------------------------------------------------
# Sample memory factory
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_memory_factory(test_db, clean_tables, mock_embedding):
    """Factory fixture that creates memories with known content via create_memory.

    Returns a callable:
        factory(title="...", content="...", type="idea", **kwargs) -> str (memory_id)

    Automatically generates a deterministic embedding from the content.
    """
    def _factory(title="Test Memory", content="Test content for memory.",
                 type="idea", embed=True, **kwargs):
        embedding = None
        if embed:
            embedding = _deterministic_embedding(content)
        return db.create_memory(
            type=type,
            title=title,
            content=content,
            embedding=str(embedding) if embedding else None,
            **kwargs,
        )
    return _factory
