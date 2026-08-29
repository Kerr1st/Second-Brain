"""Integration tests for the production schema migration runner.

Every test uses a uniquely named disposable database.  The protected development,
test, and production databases are never selected by this module.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import psycopg2
from psycopg2 import sql


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "migrations"
PROTECTED_DATABASES = {
    "memory_bank",
    "memory_bank_test",
    "second_brain_codex_dev",
    "second_brain_codex_test",
}


def _connection_config(database: str) -> dict[str, object]:
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": database,
        "user": os.environ.get("DB_USER", "memory_bank"),
        "password": os.environ.get("DB_PASSWORD", "memory_bank"),
    }


def _create_disposable_database() -> str:
    database = f"second_brain_runner_it_{uuid.uuid4().hex}"
    assert database not in PROTECTED_DATABASES
    conn = psycopg2.connect(**_connection_config("postgres"))
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    finally:
        conn.close()
    return database


def _drop_disposable_database(database: str) -> None:
    assert database not in PROTECTED_DATABASES
    conn = psycopg2.connect(**_connection_config("postgres"))
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
    finally:
        conn.close()


def _runner_environment(database: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DB_HOST": str(_connection_config(database)["host"]),
            "DB_PORT": str(_connection_config(database)["port"]),
            "DB_NAME": database,
            "DB_USER": str(_connection_config(database)["user"]),
            "DB_PASSWORD": str(_connection_config(database)["password"]),
            "PGPASSWORD": str(_connection_config(database)["password"]),
        }
    )
    return env


def _run_runner(database: str, migrations_dir: Path) -> subprocess.CompletedProcess[str]:
    env = _runner_environment(database)
    env["MIGRATIONS_DIR"] = str(migrations_dir)
    return subprocess.run(
        ["bash", str(MIGRATIONS_DIR / "migrate.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_failing_migration_is_atomic_and_unrecorded(tmp_path: Path) -> None:
    """A SQL error rolls back the entire migration and its tracking row."""
    database = _create_disposable_database()
    try:
        test_migrations = tmp_path / "migrations"
        test_migrations.mkdir()
        shutil.copy2(
            MIGRATIONS_DIR / "000_migrations_table.sql",
            test_migrations / "000_migrations_table.sql",
        )
        (test_migrations / "001_intentional_failure.sql").write_text(
            "CREATE TABLE partial_write (id INTEGER);\n"
            "SELECT definitely_missing_migration_function();\n",
            encoding="utf-8",
        )

        result = _run_runner(database, test_migrations)

        with psycopg2.connect(**_connection_config(database)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.partial_write')")
                partial_table = cur.fetchone()[0]
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = %s)",
                    ("001_intentional_failure.sql",),
                )
                migration_recorded = cur.fetchone()[0]

        assert result.returncode != 0, result.stdout + result.stderr
        assert partial_table is None
        assert migration_recorded is False
    finally:
        _drop_disposable_database(database)


def test_real_runner_builds_fresh_database_and_reruns_cleanly() -> None:
    """The production migration set builds a fresh selected database exactly once."""
    database = _create_disposable_database()
    try:
        first = _run_runner(database, MIGRATIONS_DIR)
        assert first.returncode == 0, first.stdout + first.stderr

        expected_versions = [
            "001_initial_schema.sql",
            "002_v2_columns.sql",
            "003_dream_cycle.sql",
            "004_evaluator_d.sql",
            "005_question_weighted_search.sql",
            "006_encoding_context.sql",
            "007_schema_type.sql",
            "008_knowledge_graph.sql",
            "009_hnsw_recall.sql",
            "010_express_feedback.sql",
            "011_backend_provenance.sql",
            "012_agent_task_capture.sql",
        ]
        with psycopg2.connect(**_connection_config(database)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM schema_migrations ORDER BY version")
                applied_versions = [row[0] for row in cur.fetchall()]
                cur.execute("SHOW hnsw.ef_search")
                ef_search = cur.fetchone()[0]

        assert applied_versions == expected_versions
        assert ef_search == "200"

        second = _run_runner(database, MIGRATIONS_DIR)
        assert second.returncode == 0, second.stdout + second.stderr
        assert second.stdout.count("(already applied)") == len(expected_versions)

        with psycopg2.connect(**_connection_config(database)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM schema_migrations")
                assert cur.fetchone()[0] == len(expected_versions)
    finally:
        _drop_disposable_database(database)


def test_concurrent_runners_serialize_and_apply_once(tmp_path: Path) -> None:
    """Concurrent runners coordinate through the database and record one application."""
    database = _create_disposable_database()
    try:
        test_migrations = tmp_path / "concurrent_migrations"
        test_migrations.mkdir()
        shutil.copy2(
            MIGRATIONS_DIR / "000_migrations_table.sql",
            test_migrations / "000_migrations_table.sql",
        )
        (test_migrations / "001_concurrent.sql").write_text(
            "SELECT pg_sleep(0.25);\n"
            "CREATE TABLE concurrency_probe (id INTEGER);\n",
            encoding="utf-8",
        )
        env = _runner_environment(database)
        env["MIGRATIONS_DIR"] = str(test_migrations)
        command = ["bash", str(MIGRATIONS_DIR / "migrate.sh")]

        first = subprocess.Popen(
            command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        second = subprocess.Popen(
            command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        first_stdout, first_stderr = first.communicate(timeout=30)
        second_stdout, second_stderr = second.communicate(timeout=30)

        assert first.returncode == 0, first_stdout + first_stderr
        assert second.returncode == 0, second_stdout + second_stderr
        with psycopg2.connect(**_connection_config(database)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.concurrency_probe')")
                assert cur.fetchone()[0] == "concurrency_probe"
                cur.execute(
                    "SELECT count(*) FROM schema_migrations WHERE version = %s",
                    ("001_concurrent.sql",),
                )
                assert cur.fetchone()[0] == 1
    finally:
        _drop_disposable_database(database)
