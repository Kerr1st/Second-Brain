"""Migration 018 (knowledge-graph retirement, D4) and scripts/maintenance/export_graph_tables.py.

The contract: populated graph tables are dropped only behind a verified, encrypted
export whose marker (a ``corpus_maintenance`` row) records the row counts 018 then
re-checks; empty tables (fresh clusters) drop without ceremony; the archive restores
the three tables as the owner role.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

import psycopg2
import psycopg2.errors
import pytest

from scripts.maintenance import backup_bundle as backup
from scripts.maintenance import export_graph_tables as export_mod

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
APP, OWNER = os.environ["DB_USER"], os.environ["DB_MIGRATE_USER"]
NAME = "sb_migration_018_graph_test"
GRAPH = ("entities", "entity_edges", "memory_entities")


def _connect(dbname, user, autocommit=False):
    conn = psycopg2.connect(host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]), dbname=dbname, user=user)
    conn.autocommit = autocommit
    return conn


def _migrate(dbname, directory=MIGRATIONS):
    env = {**os.environ, "DB_NAME": dbname, "MIGRATIONS_DIR": str(directory)}
    return subprocess.run(["/bin/bash", str(MIGRATIONS / "migrate.sh")], env=env, capture_output=True, text=True, timeout=120)


def _dir_through(tmp_path, last):
    for path in sorted(MIGRATIONS.glob("[0-9]*.sql")):
        if int(path.name[:3]) <= last:
            shutil.copyfile(path, tmp_path / path.name)
    return tmp_path


def _key(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(b"synthetic test key never used outside tests")
    key.chmod(0o600)
    return key


def _present(cur):
    cur.execute("SELECT t FROM unnest(%s::text[]) AS t WHERE to_regclass('public.' || t) IS NOT NULL ORDER BY 1", (list(GRAPH),))
    return [r[0] for r in cur.fetchall()]


def _ledger(cur):
    cur.execute("SELECT version FROM schema_migrations ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def _markers(cur):
    cur.execute("SELECT manifest_sha256, details FROM corpus_maintenance WHERE operation_key='graph-export' ORDER BY applied_at")
    return cur.fetchall()


@pytest.fixture(scope="module")
def populated(tmp_path_factory):
    """Through 017 as the owner, then graph rows written by the app role (the installed shape)."""
    result = _migrate(NAME, _dir_through(tmp_path_factory.mktemp("through017"), 17))
    assert result.returncode == 0, result.stdout + result.stderr
    with _connect(NAME, APP) as conn, conn.cursor() as cur:
        assert _present(cur) == sorted(GRAPH)
        cur.execute("INSERT INTO memories (type, title, content) VALUES ('idea', 'linked', 'x') RETURNING id")
        memory = cur.fetchone()[0]
        cur.execute("INSERT INTO entities (category, name) VALUES ('Channel', 'tfc-ngde'), ('Person', 'Priya') RETURNING id")
        a, b = [r[0] for r in cur.fetchall()]
        cur.execute("INSERT INTO entity_edges (from_entity, to_entity, relation) VALUES (%s, %s, 'member_of')", (b, a))
        cur.execute("INSERT INTO memory_entities (memory_id, entity_id, relation) VALUES (%s, %s, 'mentions')", (memory, b))
        conn.commit()
    return {"rows": {"entities": 2, "entity_edges": 1, "memory_entities": 1}, "memory": memory}


def test_018_refuses_populated_tables_without_an_export_marker(populated):
    result = _migrate(NAME)
    assert result.returncode != 0
    assert "apply: 018_retire_knowledge_graph.sql" in result.stdout
    assert "no graph-export marker" in result.stderr and "export_graph_tables.py" in result.stderr
    with _connect(NAME, OWNER) as conn, conn.cursor() as cur:
        assert _present(cur) == sorted(GRAPH)                      # the file's transaction rolled back
        assert "018_retire_knowledge_graph.sql" not in _ledger(cur)
        cur.execute("SELECT count(*) FROM entities")
        assert cur.fetchone()[0] == 2


def test_018_refuses_a_marker_whose_row_counts_do_not_match(populated):
    with _connect(NAME, APP, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO corpus_maintenance (manifest_sha256, operation_key, details) VALUES ('stale', 'graph-export', %s::jsonb)",
                    (json.dumps({"rows": {"entities": 1, "entity_edges": 1, "memory_entities": 1}, "archive": "stale.gpg"}),))
    try:
        result = _migrate(NAME)
        assert result.returncode != 0
        assert "does not match the current row counts" in result.stderr and "stale" in result.stderr
        with _connect(NAME, OWNER) as conn, conn.cursor() as cur:
            assert _present(cur) == sorted(GRAPH)
    finally:
        with _connect(NAME, APP, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM corpus_maintenance WHERE manifest_sha256='stale' AND operation_key='graph-export'")


def test_export_refuses_when_a_writer_changes_the_tables_mid_export(populated, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_NAME", NAME)
    real = export_mod.command

    def racing(args, **kwargs):
        if args[0] == "pg_dump":
            with _connect(NAME, APP, autocommit=True) as other, other.cursor() as cur:
                cur.execute("INSERT INTO entities (category, name) VALUES ('Channel', 'late-arrival')")
        return real(args, **kwargs)
    monkeypatch.setattr(export_mod, "command", racing)
    with _connect(NAME, APP) as conn:
        with pytest.raises(ValueError, match="changed during the export"):
            export_mod.export(tmp_path / "backups", _key(tmp_path), conn=conn)
    with _connect(NAME, APP, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM entities WHERE name='late-arrival'")
        assert _markers(cur) == []
    assert not list((tmp_path / "backups" / "graph-export").glob("*.gpg")), "a refused export leaves no archive"
    assert not list((tmp_path / "backups" / "graph-export").glob("*.receipt.json"))


def test_export_then_018_drops_the_tables_and_the_archive_restores_them(populated, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_NAME", NAME)
    key = _key(tmp_path)
    with _connect(NAME, APP) as conn:
        record = export_mod.export(tmp_path / "backups", key, conn=conn)
    archive, receipt = Path(record["archive"]), Path(record["receipt"])
    assert archive.parent == tmp_path / "backups" / "graph-export" and archive.name.startswith("graph-export_") and archive.name.endswith(".dump.gpg")
    assert receipt.exists() and archive.stat().st_mode & 0o777 == 0o600 and receipt.stat().st_mode & 0o777 == 0o600
    written = json.loads(receipt.read_text())
    assert written["rows"] == populated["rows"] == record["rows"]
    assert written["sha256"] == backup.digest(archive) == record["sha256"] and written["verified"] is True
    assert key.read_text() not in json.dumps(record) and str(key) not in json.dumps(record)  # the receipt never names the key
    assert key.read_text() not in receipt.read_text() and str(key) not in receipt.read_text()
    assert not list(archive.parent.glob(".graph-export-*")), "plaintext work area removed"
    with _connect(NAME, APP) as conn, conn.cursor() as cur:
        markers = _markers(cur)
        assert len(markers) == 1 and markers[0][0] == record["sha256"]
        assert markers[0][1]["rows"] == populated["rows"] and markers[0][1]["archive"] == str(archive)

    # The archive is a real, decryptable pg_dump of exactly the three tables.
    plain = tmp_path / "plain.dump"
    backup.gpg(["--output", plain, "--decrypt", archive], key, tmp_path)
    assert backup.digest(plain) == written["plain_sha256"]
    listing = backup.command(["pg_restore", "--list", plain]).stdout.decode()
    assert all(f"TABLE DATA public {t}" in listing for t in GRAPH) and "TABLE DATA public memories" not in listing

    result = _migrate(NAME)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "apply: 018_retire_knowledge_graph.sql" in result.stdout and "dropping" in result.stderr and record["sha256"] in result.stderr
    with _connect(NAME, OWNER) as conn, conn.cursor() as cur:
        assert _present(cur) == []
        cur.execute("SELECT count(*) FROM pg_class WHERE relname LIKE 'idx_entit%' OR relname LIKE 'idx_memory_entities%'")
        assert cur.fetchone()[0] == 0
        assert "018_retire_knowledge_graph.sql" in _ledger(cur)
        assert len(_markers(cur)) == 1                              # the pointer to the archive survives the drop
        cur.execute("SELECT count(*) FROM memories WHERE id=%s", (populated["memory"],))
        assert cur.fetchone()[0] == 1
    again = _migrate(NAME)
    assert again.returncode == 0 and "skip: 018_retire_knowledge_graph.sql (already applied)" in again.stdout

    # Recovery: the owner restores the archive into the live database (FK to memories intact).
    restore = subprocess.run([shutil.which("pg_restore", path=os.environ["PATH"]), "-h", os.environ["DB_HOST"], "-p", os.environ["DB_PORT"],
                              "-U", OWNER, "-d", NAME, "--exit-on-error", "--no-owner", "--no-acl", str(plain)],
                             capture_output=True, text=True, timeout=120)
    assert restore.returncode == 0, restore.stdout + restore.stderr
    with _connect(NAME, OWNER) as conn, conn.cursor() as cur:
        assert _present(cur) == sorted(GRAPH)
        for table, expected in populated["rows"].items():
            cur.execute(f'SELECT count(*) FROM public."{table}"')
            assert cur.fetchone()[0] == expected, table
        cur.execute("SELECT count(*) FROM memory_entities me JOIN memories m ON m.id = me.memory_id")
        assert cur.fetchone()[0] == 1
        # The ledger still says 018 applied, so the restored tables stay until an operator decides.
        cur.execute("DROP TABLE memory_entities, entity_edges, entities")
        conn.commit()


def test_export_refuses_when_nothing_is_left_to_export(populated, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_NAME", NAME)
    with _connect(NAME, APP) as conn:
        with pytest.raises(ValueError, match="already gone"):
            export_mod.export(tmp_path / "backups", _key(tmp_path), conn=conn)
    assert not (tmp_path / "backups" / "graph-export").exists() or not list((tmp_path / "backups" / "graph-export").iterdir())


def test_dry_run_prints_counts_and_writes_nothing(populated, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DB_NAME", NAME)
    assert export_mod.main(["--dry-run", "--output", str(tmp_path / "backups"), "--key", str(_key(tmp_path))]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["rows"] == {t: None for t in GRAPH}
    assert not (tmp_path / "backups").exists()


def test_main_reports_failures_as_json_without_the_key(populated, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DB_NAME", NAME)
    key = _key(tmp_path)
    assert export_mod.main(["--output", str(tmp_path / "backups"), "--key", str(key)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "failed" and "already gone" in out["reason"]
    assert key.read_text() not in json.dumps(out)


def test_suite_database_dropped_its_empty_graph_tables_without_a_marker(test_schema):
    """Fresh clusters: 008 creates the tables empty, 018 drops them with a NOTICE and no export."""
    with psycopg2.connect(**test_schema) as conn, conn.cursor() as cur:
        assert _present(cur) == []
        assert _markers(cur) == []
        assert "018_retire_knowledge_graph.sql" in _ledger(cur)


def test_018_is_the_last_word_on_the_graph_in_the_repo():
    """After R7 nothing a job can execute names the three tables, except the export
    script and the migrations (008 creates, 016 trims, 018 drops).  ``entity_edges``
    and ``memory_entities`` are unique names; ``entities`` is also a skim-record field,
    so for it only SQL-shaped references count."""
    import re
    unique = re.compile(r"\b(entity_edges|memory_entities)\b")
    sql_entities = re.compile(r"\b(FROM|INTO|JOIN|UPDATE|TABLE|TRUNCATE|ON)\s+(public\.)?entities\b|to_regclass\(\W*entities\b|'entities'", re.IGNORECASE)
    allowed = {"scripts/maintenance/export_graph_tables.py"}
    # Quick Desktop's own sqlite store has a table called ``entities``; its one reader
    # is pinned here and must query QD's sqlite connection, never PostgreSQL.
    qd_sqlite_reader = "src/capture/quick_desktop.py"
    tracked = subprocess.run(["git", "ls-files", "--", "src", "scripts", "scheduling", "config"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
    offenders = []
    for rel in tracked:
        path = ROOT / rel
        if rel in allowed or not path.is_file():
            continue
        text = path.read_text(errors="replace")
        if rel == qd_sqlite_reader:
            assert "qd_conn.execute" in text and "psycopg2" not in text and "get_connection" not in text.split("def fetch_kg_decisions")[1].split("def ")[0]
            text = text.replace("FROM entities e", "FROM <qd sqlite entities> e")
        for number, line in enumerate(text.splitlines(), 1):
            if unique.search(line) or sql_entities.search(line):
                offenders.append(f"{rel}:{number}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)
    for retired in ("src/capture/slack_graph.py", "scripts/maintenance/import_slack_graph.py", "tests/test_slack_graph.py"):
        assert not (ROOT / retired).exists(), retired
    qd = (ROOT / "src/capture/qd.py").read_text()
    assert "slack_graph" not in qd and '"graph"' not in qd
