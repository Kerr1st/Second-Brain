"""Exercise atomicity, reruns, and concurrent runners on disposable databases."""
import os
from pathlib import Path
import shutil
import subprocess

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


def config(name, user=None):
    """Owner-role connection by default: the ledger is owner-only until 017 grants the app role SELECT."""
    return {"host": os.environ["DB_HOST"], "port": int(os.environ["DB_PORT"]), "user": user or os.environ["DB_MIGRATE_USER"], "dbname": name}


def command_env(name, directory):
    return {**os.environ, "DB_NAME": name, "MIGRATIONS_DIR": str(directory)}


def run(name, directory):
    return subprocess.run(["/bin/bash", str(MIGRATIONS / "migrate.sh")], env=command_env(name, directory), capture_output=True, text=True, timeout=90)


def test_failure_rolls_back_schema_and_ledger(tmp_path):
    name = "sb_migration_failure_test"
    shutil.copyfile(MIGRATIONS / "000_migrations_table.sql", tmp_path / "000_migrations_table.sql")
    (tmp_path / "001_failure.sql").write_text("CREATE TABLE partial_write(id integer); SELECT missing_migration_function();")
    result = run(name, tmp_path)
    assert result.returncode != 0
    with psycopg2.connect(**config(name)) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT to_regclass('partial_write')")
            assert cursor.fetchone()[0] is None
            cursor.execute("SELECT count(*) FROM schema_migrations")
            assert cursor.fetchone()[0] == 0


def test_full_schema_and_repeat_are_consistent():
    name = "sb_migration_fresh_test"
    for attempt in range(2):
        result = run(name, MIGRATIONS)
        assert result.returncode == 0, result.stdout + result.stderr
        expected = sorted(p.name for p in MIGRATIONS.glob("[0-9]*.sql") if not p.name.startswith("000_"))
        with psycopg2.connect(**config(name)) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
                assert [row[0] for row in cursor.fetchall()] == expected
                cursor.execute("SHOW hnsw.ef_search")
                assert cursor.fetchone()[0] == "200"
        if attempt:
            assert result.stdout.count("(already applied)") == len(expected)


def test_concurrent_runners_apply_once(tmp_path):
    name = "sb_migration_concurrent_test"
    shutil.copyfile(MIGRATIONS / "000_migrations_table.sql", tmp_path / "000_migrations_table.sql")
    (tmp_path / "001_concurrent.sql").write_text("SELECT pg_sleep(0.2); CREATE TABLE one_application(id integer);")
    processes = [subprocess.Popen(["/bin/bash", str(MIGRATIONS / "migrate.sh")], env=command_env(name, tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stdout + stderr
    with psycopg2.connect(**config(name)) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM schema_migrations WHERE version='001_concurrent.sql'")
            assert cursor.fetchone()[0] == 1
