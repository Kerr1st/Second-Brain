"""Synthetic database and fail-closed provider fixtures.

Run via scripts/test_isolated.py; guards load before importing application code.
"""
from tests._isolation import install
install()

import hashlib
import math
import os
from pathlib import Path
import subprocess

import psycopg2
from psycopg2 import sql
import pytest

import src.db as db
from src.db import close_pool

TEST_DB_NAME = os.environ["TEST_DB_NAME"]
_TEST_CONFIG = {
    "host": os.environ["DB_HOST"], "port": int(os.environ["DB_PORT"]),
    "dbname": TEST_DB_NAME, "user": os.environ["DB_USER"], "password": "",
}
# Environment was redirected before pytest imports. Never restore a live target.
db.DB_CONFIG.update(_TEST_CONFIG)


@pytest.fixture(autouse=True)
def _offline_services(monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("Live service access is disabled in the isolated suite")
    monkeypatch.setattr("src.skim.store.get_connection", refuse)
    monkeypatch.setattr("src.embeddings._get_client", refuse)
    from src.backends.bedrock import BedrockInvoker
    def injected_client_only(self, timeout):
        if self._injected_client is None:
            return refuse()
        return self._injected_client
    monkeypatch.setattr(BedrockInvoker, "_get_client", injected_client_only)


@pytest.fixture(scope="session", autouse=True)
def _test_connection_config():
    close_pool()
    db.DB_CONFIG.update(_TEST_CONFIG)
    yield
    close_pool()


def _apply_migrations():
    """Exercise the actual transactional runner, including its version ledger."""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["/bin/bash", str(root / "migrations/migrate.sh")],
                            capture_output=True, text=True, timeout=90, check=False)
    if result.returncode:
        raise RuntimeError("Test schema migration failed:\n" + result.stdout + result.stderr)


@pytest.fixture(scope="session")
def test_schema(_test_connection_config):
    _apply_migrations()
    close_pool()  # reconnect after ALTER DATABASE SET so session GUCs take effect
    yield _TEST_CONFIG.copy()
    close_pool()


@pytest.fixture(scope="session")
def test_db(test_schema):
    """Compatibility alias for existing database unit tests (not shadowable safety)."""
    return test_schema


def _truncate_test_tables():
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'schema_migrations'")
            tables = [row[0] for row in cur.fetchall()]
            if tables:
                cur.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(sql.Identifier("public", table) for table in tables)))
        conn.commit()


@pytest.fixture()
def clean_tables(test_schema, _clean_each_database_test):
    """Compatibility alias; automatic cleanup surrounds every DB fixture user."""
    yield


@pytest.fixture(autouse=True)
def _clean_each_database_test(request):
    uses_database = any(name in request.fixturenames for name in
                        ("test_db", "test_schema", "test_conn", "sample_memory_factory"))
    if uses_database:
        request.getfixturevalue("test_schema")
        _truncate_test_tables()
    yield
    if uses_database:
        _truncate_test_tables()


@pytest.fixture()
def test_conn(test_schema, clean_tables):
    conn = psycopg2.connect(**test_schema)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


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
