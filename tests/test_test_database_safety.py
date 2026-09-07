"""Safety checks for the destructive, isolated test-database fixture."""

import pytest

from tests.conftest import _require_disposable_test_database


def test_test_database_recreation_rejects_non_test_database_names():
    _require_disposable_test_database("memory_bank_test")
    _require_disposable_test_database("second_brain_codex_test")

    unsafe_names = (
        "memory_bank",
        "postgres",
        "template0",
        "template1",
        "contest",
        "latest",
        "production_test_backup",
        "second_brain_production_test",
        "test",
    )
    for protected in unsafe_names:
        with pytest.raises(RuntimeError, match="refusing to recreate"):
            _require_disposable_test_database(protected)


def test_connection_and_application_use_the_same_disposable_database(db_connection):
    from src.db import get_connection

    with db_connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        fixture_database = cursor.fetchone()[0]
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        application_database = cursor.fetchone()[0]
    assert fixture_database == application_database
    _require_disposable_test_database(application_database)
