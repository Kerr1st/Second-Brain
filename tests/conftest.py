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
import glob

import psycopg2
import pytest

import src.db as db
from src.db import close_pool


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


def _create_test_db():
    """Create the test database if it doesn't already exist."""
    conn = _admin_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,)
            )
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        conn.close()


def _apply_migrations(conn):
    """Apply all SQL migrations from migrations/ in order.

    Uses savepoints so that already-applied migrations (e.g. CREATE TABLE
    without IF NOT EXISTS) are silently skipped instead of crashing.
    """
    migrations_dir = os.path.join(os.path.dirname(__file__), "..", "migrations")
    migration_files = sorted(glob.glob(os.path.join(migrations_dir, "[0-9]*.sql")))

    for migration_file in migration_files:
        with open(migration_file, "r") as f:
            sql = f.read()
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
        except psycopg2.errors.DuplicateTable:
            conn.rollback()
        except psycopg2.errors.DuplicateObject:
            conn.rollback()


@pytest.fixture(scope="session")
def test_db():
    """Session-scoped fixture: create memory_bank_test DB, apply migrations, override DB_CONFIG.

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

    # Invalidate any pool created with old config
    close_pool()

    # Apply all migrations
    with db.get_connection() as conn:
        _apply_migrations(conn)

    yield db.DB_CONFIG

    # Restore original config
    db.DB_CONFIG.update(original_config)
    close_pool()


@pytest.fixture()
def clean_tables(test_db):
    """Function-scoped fixture: truncate memories and memory_relationships between tests."""
    yield
    with db.get_connection() as conn:
        with conn.cursor() as cur:
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
