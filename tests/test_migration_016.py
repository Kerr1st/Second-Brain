"""Migration 016 against a database that looks like production at schema 014.

The live ledger is 001..014 (no 015 file exists; 016 reserves it). These tests
seed representative rows -- expired edges of every kind the R1 expiry rule
distinguishes, populated ``candidates_deferred`` / ``deferred_twice_rejected``
columns, and every ``source_type`` / ``type`` / ``status`` / ``mem_class`` /
``relation_type`` value present in the live inventory of 2026-10-05 -- then run
``migrate.sh`` over the full directory so only 016 is applied.
"""
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from src.validation import SOURCE_TYPES, MEMORY_TYPES, MEMORY_STATUSES, MEMORY_CLASSES, RELATION_TYPES

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
SIXTEEN = MIGRATIONS / "016_data_integrity.sql"

# Live inventory (read-only, 2026-10-05): every distinct non-NULL value.
LIVE_SOURCE_TYPES = [
    "kiro_ide_chat", "quick_desktop_doc", "distilled_chat", "kiro_cli_chat", "article",
    "quick_desktop_feed", "youtube", "slack", "quick_desktop_session_event", "quick_desktop_people",
    "quick_desktop_chat", "outlook_email", "codex_task", "distilled_agent_task", "outlook_calendar",
    "quick_desktop_terminology", "quick_desktop_decision", "quick_desktop_source", "browser_history",
    "quick_desktop_procedure", "quick_desktop_tool_strategy", "quick_desktop_preference",
    "quick_desktop_anti_pattern", "quick_desktop", "quick_desktop_profile", "weekly_digest",
]
LIVE_TYPES = ["source", "insight", "decision", "research", "correction_episode", "synthesis", "connection", "question"]
LIVE_STATUSES = ["active", "superseded"]
LIVE_CLASSES = ["semantic", "episodic", "source", "procedural"]
LIVE_RELATIONS = ["related_to", "derived_from", "extends", "supports", "contradicts", "superseded_by"]
# Labels the R3 writers emit that have no live rows yet.
R3_WRITER_LABELS = ["api_browser", "api_email", "api_slack", "dream_cycle", "manual", "file"]


def _config(name):
    return {"host": os.environ["DB_HOST"], "port": int(os.environ["DB_PORT"]), "user": os.environ["DB_USER"], "dbname": name}


def _run(name, directory):
    env = {**os.environ, "DB_NAME": name, "MIGRATIONS_DIR": str(directory)}
    return subprocess.run(["/bin/bash", str(MIGRATIONS / "migrate.sh")], env=env, capture_output=True, text=True, timeout=120)


def _pg(name, tool, *args, check=True):
    """Run a PostgreSQL client tool (pg_dump / pg_restore) against the test cluster."""
    binary = shutil.which(tool, path=os.environ["PATH"])
    assert binary, f"{tool} not on PATH"
    command = [binary, "-h", os.environ["DB_HOST"], "-p", os.environ["DB_PORT"], "-U", os.environ["DB_USER"], "-d", name, *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    assert not check or result.returncode == 0, result.stdout + result.stderr
    return result


def _dir_through_014(tmp_path):
    """A migrations directory holding 000..014 only (the live ledger shape)."""
    for path in sorted(MIGRATIONS.glob("[0-9]*.sql")):
        if int(path.name[:3]) <= 14:
            shutil.copyfile(path, tmp_path / path.name)
    return tmp_path


def _mem(cur, **kw):
    cols = {"type": "source", "title": "t", "content": "c", "status": "active"}
    cols.update(kw)
    names = ", ".join(cols)
    cur.execute(f"INSERT INTO memories ({names}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                list(cols.values()))
    return cur.fetchone()[0]


def _edge(cur, a, b, relation, *, created_days_ago, expired_days_ago=None):
    cur.execute(
        """INSERT INTO memory_relationships (source_id, target_id, relation_type, created_at, expired_at)
           VALUES (%s, %s, %s, now() - make_interval(days => %s),
                   CASE WHEN %s IS NULL THEN NULL ELSE now() - make_interval(days => %s) END)""",
        (a, b, relation, created_days_ago, expired_days_ago, expired_days_ago))


def test_016_sets_a_lock_timeout_before_any_ddl():
    text = SIXTEEN.read_text()
    first_statement = next(line for line in text.splitlines() if line.strip() and not line.startswith("--"))
    assert first_statement.strip().upper().startswith("SET LOCAL LOCK_TIMEOUT")


def test_live_vocabularies_are_admitted_by_016_checks_and_by_validation():
    """Every live value and every R3 writer label is inside both the CHECK lists and the validator."""
    text = SIXTEEN.read_text()
    for value in LIVE_SOURCE_TYPES + R3_WRITER_LABELS:
        assert f"'{value}'" in text, value
        assert value in SOURCE_TYPES, value
    for value in LIVE_TYPES:
        assert f"'{value}'" in text and value in MEMORY_TYPES, value
    for value in LIVE_STATUSES:
        assert f"'{value}'" in text and value in MEMORY_STATUSES, value
    for value in LIVE_CLASSES:
        assert f"'{value}'" in text and value in MEMORY_CLASSES, value
    for value in LIVE_RELATIONS:
        assert f"'{value}'" in text and value in RELATION_TYPES, value


@pytest.fixture(scope="module")
def upgraded(tmp_path_factory):
    name = "sb_migration_016_upgrade_test"
    result = _run(name, _dir_through_014(tmp_path_factory.mktemp("through014")))
    assert result.returncode == 0, result.stdout + result.stderr
    seeded = {}
    with psycopg2.connect(**_config(name)) as conn, conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        assert [r[0] for r in cur.fetchall()][-1] == "014_memory_skim.sql"
        # Every live vocabulary value, plus NULL source_type / mem_class rows.
        for st in LIVE_SOURCE_TYPES:
            _mem(cur, source_type=st, source_url=f"seed://{st}")
        for t in LIVE_TYPES:
            _mem(cur, type=t)
        for s in LIVE_STATUSES:
            _mem(cur, status=s)
        for c in LIVE_CLASSES:
            _mem(cur, mem_class=c)
        _mem(cur, source_type=None, mem_class=None)
        # Memories for edges: warm endpoint accessed 10 days ago, cold ones never.
        warm = _mem(cur, title="warm"); cur.execute("UPDATE memories SET last_accessed_at = now() - interval '10 days' WHERE id = %s", (warm,))
        cold = [_mem(cur, title=f"cold{i}") for i in range(6)]
        seeded["provenance_old_cold"] = (cold[0], cold[1]); _edge(cur, cold[0], cold[1], "derived_from", created_days_ago=200, expired_days_ago=100)
        seeded["superseded_old_cold"] = (cold[2], cold[3]); _edge(cur, cold[2], cold[3], "superseded_by", created_days_ago=200, expired_days_ago=100)
        seeded["young_cold"] = (cold[0], cold[2]); _edge(cur, cold[0], cold[2], "related_to", created_days_ago=5, expired_days_ago=4)
        seeded["old_warm"] = (warm, cold[4]); _edge(cur, warm, cold[4], "related_to", created_days_ago=200, expired_days_ago=50)
        seeded["old_cold_quick_expiry"] = (cold[4], cold[5]); _edge(cur, cold[4], cold[5], "related_to", created_days_ago=200, expired_days_ago=150)
        seeded["old_cold_late_expiry"] = (cold[1], cold[5]); _edge(cur, cold[1], cold[5], "extends", created_days_ago=400, expired_days_ago=10)
        seeded["live_edge"] = (cold[3], cold[5]); _edge(cur, cold[3], cold[5], "supports", created_days_ago=400)
        for rel in LIVE_RELATIONS:  # every live relation_type must survive the CHECK
            _edge(cur, cold[1], cold[4] if rel != "related_to" else cold[2], rel, created_days_ago=1)
        # Deferred columns populated (dropped by 016 after folding into jsonb).
        run_id = str(uuid.uuid4())
        cur.execute("INSERT INTO dream_cycle_runs (id, run_type, started_at, candidates_deferred, stats) VALUES (%s, 'scheduled', now(), 3, '{\"k\": 1}'::jsonb)", (run_id,))
        cur.execute("INSERT INTO dream_cycle_candidates (run_id, candidate_json, final_verdict, deferred_twice_rejected) VALUES (%s, '{\"title\": \"x\"}'::jsonb, 'REJECTED', TRUE) RETURNING id", (run_id,))
        seeded["candidate"] = cur.fetchone()[0]
        seeded["run"] = run_id
        conn.commit()
    # The pre-window backup of the runbook: a custom-format dump of the 014 state.
    seeded["pre_016_dump"] = str(tmp_path_factory.mktemp("dump") / "pre016.dump")
    _pg(name, "pg_dump", "-Fc", "-f", seeded["pre_016_dump"])
    result = _run(name, MIGRATIONS)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("apply:") == 1 and "apply: 016_data_integrity.sql" in result.stdout
    assert result.stdout.count("(already applied)") == 14
    return name, seeded


def _expired(cur, pair, relation):
    a, b = pair
    cur.execute("SELECT expired_at FROM memory_relationships WHERE source_id=%s AND target_id=%s AND relation_type=%s", (a, b, relation))
    return cur.fetchone()[0]


def test_016_applies_once_on_a_014_ledger_with_live_shaped_rows(upgraded):
    name, seeded = upgraded
    with psycopg2.connect(**_config(name)) as conn, conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        versions = [r[0] for r in cur.fetchall()]
        assert versions[-1] == "016_data_integrity.sql" and len(versions) == 15 and not any(v.startswith("015") for v in versions)
        cur.execute("SELECT conname FROM pg_constraint WHERE conrelid = 'memories'::regclass AND contype = 'c'")
        names = {r[0] for r in cur.fetchall()}
        assert {"memories_type_vocabulary", "memories_status_vocabulary", "memories_class_vocabulary", "memories_source_vocabulary"} <= names
        cur.execute("SELECT to_regclass('corpus_maintenance') IS NOT NULL, to_regclass('idx_memories_parent_id') IS NOT NULL")
        assert cur.fetchone() == (True, True)
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='dream_cycle_runs'")
        cols = {r[0] for r in cur.fetchall()}
        assert "cost_usd" in cols and "candidates_deferred" not in cols
        cur.execute("SELECT stats->>'legacy_candidates_deferred' FROM dream_cycle_runs WHERE id=%s", (seeded["run"],))
        assert cur.fetchone()[0] == "3"
        cur.execute("SELECT candidate_json->>'legacy_deferred_twice_rejected' FROM dream_cycle_candidates WHERE id=%s", (seeded["candidate"],))
        assert cur.fetchone()[0] == "true"


def test_016_backfill_matches_the_r1_expiry_rule(upgraded):
    """Provenance, young edges and edges with a warm endpoint come back; old+cold stay expired."""
    name, seeded = upgraded
    with psycopg2.connect(**_config(name)) as conn, conn.cursor() as cur:
        assert _expired(cur, seeded["provenance_old_cold"], "derived_from") is None
        assert _expired(cur, seeded["superseded_old_cold"], "superseded_by") is None
        assert _expired(cur, seeded["young_cold"], "related_to") is None
        assert _expired(cur, seeded["old_warm"], "related_to") is None
        assert _expired(cur, seeded["old_cold_quick_expiry"], "related_to") is not None
        assert _expired(cur, seeded["old_cold_late_expiry"], "extends") is not None
        assert _expired(cur, seeded["live_edge"], "supports") is None
        cur.execute("SELECT conname FROM pg_constraint WHERE conrelid = 'memory_relationships'::regclass AND contype='c'")
        assert {"relationship_provenance_permanent", "relationships_type_vocabulary"} <= {r[0] for r in cur.fetchall()}


def test_016_checks_reject_new_out_of_vocabulary_writes_but_keep_legacy_rows(upgraded):
    name, _ = upgraded
    conn = psycopg2.connect(**_config(name))
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT count(*) AS n FROM memories WHERE source_type IS NULL AND mem_class IS NULL")
            assert cur.fetchone()["n"] >= 1  # NULL passes the CHECK (live: 106 NULL source_type rows)
        for column, value in (("source_type", "notion_page"), ("type", "bogus"), ("status", "deleted"), ("mem_class", "other")):
            row = {"type": "source", "title": "t", "content": "c", column: value}
            with pytest.raises(psycopg2.errors.CheckViolation), conn.cursor() as c2:
                c2.execute(f"INSERT INTO memories ({', '.join(row)}) VALUES ({', '.join(['%s'] * len(row))})", list(row.values()))
            conn.rollback()
        with pytest.raises(psycopg2.errors.CheckViolation), conn.cursor() as c3:
            c3.execute("INSERT INTO memory_relationships (source_id, target_id, relation_type) SELECT a.id, b.id, 'bogus' FROM memories a, memories b WHERE a.id <> b.id LIMIT 1")
        conn.rollback()
    finally:
        conn.close()


def _post_016_state(cur):
    cur.execute("SELECT count(*), max(version) FROM schema_migrations")
    ledger = cur.fetchone()
    cur.execute("SELECT conname FROM pg_constraint WHERE conrelid IN ('memories'::regclass, 'memory_relationships'::regclass, 'dream_cycle_runs'::regclass) AND contype = 'c'")
    constraints = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'dream_cycle_runs'")
    run_columns = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT to_regclass('corpus_maintenance') IS NOT NULL, to_regclass('idx_memories_parent_id') IS NOT NULL, to_regclass('idx_memories_source_created') IS NOT NULL")
    objects = cur.fetchone()
    return ledger, constraints, run_columns, objects


EXPECTED_CHECKS = {"dream_cycle_runs_cost_nonnegative", "relationship_provenance_permanent", "memories_type_vocabulary",
                   "memories_status_vocabulary", "memories_class_vocabulary", "relationships_type_vocabulary",
                   "memories_source_vocabulary"}


def _assert_post_016(cur):
    ledger, constraints, run_columns, objects = _post_016_state(cur)
    assert ledger == (15, "016_data_integrity.sql")
    assert EXPECTED_CHECKS <= constraints
    assert "cost_usd" in run_columns and "candidates_deferred" not in run_columns
    assert objects == (True, True, True)


# These two tests leave the shared database in the post-016 state they found it in.
def test_016_rerun_after_a_restore_based_rollback_succeeds(upgraded):
    """Runbook §7.8: pg_restore --clean of the pre-window dump into the live DB.

    The dump has no corpus_maintenance, so --clean leaves 016's table behind
    while the ledger goes back to 014 and the tables to their 014 shape. The
    next migrate.sh must apply 016 as a whole again, not fail at the leftover.
    """
    name, seeded = upgraded
    # Restore like the runbook: object-level drops into the existing database. The
    # test role does not own the vector extension, so leave that entry out of the list.
    listing = _pg(name, "pg_restore", "-l", seeded["pre_016_dump"], check=True)
    entries = [line for line in listing.stdout.splitlines() if " EXTENSION " not in line and " COMMENT - EXTENSION " not in line]
    toc = Path(seeded["pre_016_dump"]).with_suffix(".list")
    toc.write_text("\n".join(entries) + "\n")
    restore = _pg(name, "pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges", "-L", str(toc), seeded["pre_016_dump"], check=False)
    assert restore.returncode == 0, restore.stdout + restore.stderr
    with psycopg2.connect(**_config(name)) as conn, conn.cursor() as cur:
        ledger, constraints, run_columns, objects = _post_016_state(cur)
        assert ledger == (14, "014_memory_skim.sql")                       # ledger restored
        assert not (EXPECTED_CHECKS & constraints)                         # 014-shaped tables
        assert "candidates_deferred" in run_columns and "cost_usd" not in run_columns
        assert objects[0] is True                                          # the leftover: corpus_maintenance survives --clean
        cur.execute("SELECT stats->>'legacy_candidates_deferred', candidates_deferred FROM dream_cycle_runs WHERE id=%s", (seeded["run"],))
        assert cur.fetchone() == (None, 3)
    result = _run(name, MIGRATIONS)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("apply:") == 1 and "apply: 016_data_integrity.sql" in result.stdout
    with psycopg2.connect(**_config(name)) as conn, conn.cursor() as cur:
        _assert_post_016(cur)
        cur.execute("SELECT stats->>'legacy_candidates_deferred' FROM dream_cycle_runs WHERE id=%s", (seeded["run"],))
        assert cur.fetchone()[0] == "3"
        assert _expired(cur, seeded["provenance_old_cold"], "derived_from") is None
        assert _expired(cur, seeded["old_cold_quick_expiry"], "related_to") is not None


def test_016_rerun_on_a_fully_migrated_schema_with_a_lost_ledger_row_succeeds(upgraded):
    """Every statement must tolerate its own effect already being present: with
    only the ledger row missing, 016 re-runs over existing constraints, the
    existing table, dropped legacy columns and existing indexes."""
    name, seeded = upgraded
    with psycopg2.connect(**_config(name)) as conn, conn.cursor() as cur:
        _assert_post_016(cur)
        cur.execute("DELETE FROM schema_migrations WHERE version = '016_data_integrity.sql'")
        conn.commit()
    result = _run(name, MIGRATIONS)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "apply: 016_data_integrity.sql" in result.stdout
    with psycopg2.connect(**_config(name)) as conn, conn.cursor() as cur:
        _assert_post_016(cur)
        cur.execute("SELECT count(*) FROM pg_constraint WHERE conname = ANY(%s)", (sorted(EXPECTED_CHECKS),))
        assert cur.fetchone()[0] == len(EXPECTED_CHECKS)                   # no duplicates, none dropped
        cur.execute("SELECT stats->>'legacy_candidates_deferred' FROM dream_cycle_runs WHERE id=%s", (seeded["run"],))
        assert cur.fetchone()[0] == "3"                                    # audit value kept, not re-folded
