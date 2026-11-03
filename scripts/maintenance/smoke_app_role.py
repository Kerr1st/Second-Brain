#!/usr/bin/env python3
"""Read-only smoke of the application paths as the connected (application) role.

R7 / migration 017: before and after the CONFIRM-REQUIRED operator step
``ALTER ROLE memory_bank NOSUPERUSER`` this script shows whether the role can
still do the application's work with nothing but the grants 017 gave it. It
exercises the real repositories (no model, provider or network call):

* memory_list / memory_read / memory_graph (src.db.list_memories, get_memory,
  get_relationships, get_memories_by_ids, get_memory_provenance);
* the retrieval legs (src.db.search_queries.fetch_candidates with a zero vector,
  which also issues ``SET LOCAL hnsw.iterative_scan``);
* dream-cycle repository reads (stats, rejections, dissents, tier-1 metrics,
  briefing time, the advisory run lock acquire/release);
* capture identity lookups (get_processed_source_urls, find_captured_source,
  get_capture_fingerprints);
* Express reads (latest_accepted_candidates, read_briefing -- REPEATABLE READ
  READ ONLY);
* one insert + relationship + delete round trip inside ``db.transaction()`` that
  is ROLLED BACK (the row never commits), with lock_source_identity;
* the structural statements the role must NOT be able to run after the demotion
  (CREATE TABLE, TRUNCATE, ALTER DATABASE SET, CREATE INDEX, DROP TABLE); while
  the role is still a superuser these are reported as ``still_allowed`` -- the
  script never executes them to completion (each runs in a transaction that is
  rolled back) but a superuser would have been allowed to.

Output: one JSON document (role facts, per-step wall time in ms, permission
errors, structural verdicts). Exit 0 when every application step succeeded and no
permission error occurred; 1 otherwise. Reads DB_* from the environment like the
jobs do; never prints a DSN or a password.

Usage: .venv/bin/python scripts/maintenance/smoke_app_role.py [--json-indent 2]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

STRUCTURAL = (
    ("create_table", "CREATE TABLE smoke_app_role_should_fail (id int)"),
    ("truncate", "TRUNCATE corpus_maintenance"),  # small table; a superuser run would hold its lock only until the rollback
    ("alter_database_set", "DO $$ BEGIN EXECUTE format('ALTER DATABASE %I SET hnsw.ef_search = 200', current_database()); END $$"),
    ("create_index", "CREATE INDEX smoke_app_role_should_fail ON memories (title)"),
    ("drop_table", "DROP TABLE corpus_maintenance"),
)


class Rollback(Exception):
    pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json-indent", type=int, default=None)
    args = parser.parse_args(argv)
    os.environ.setdefault("SECOND_BRAIN_PROFILE", "controlled")

    import psycopg2
    import psycopg2.errors
    from src import db
    from src import dream_cycle_db
    from src.db import express_queries, search_queries

    report = {"role": {}, "steps": [], "permission_errors": [], "structural": {}}

    def step(name, fn):
        started = time.perf_counter()
        entry = {"name": name}
        try:
            value = fn()
            entry["ok"] = True
            entry["result"] = value
        except psycopg2.errors.InsufficientPrivilege as exc:
            entry["ok"] = False
            entry["error"] = f"permission denied: {str(exc).strip().splitlines()[0]}"
            report["permission_errors"].append(name)
        except Exception as exc:  # noqa: BLE001 - every failure is evidence here
            entry["ok"] = False
            entry["error"] = f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:200]}"
        entry["ms"] = round((time.perf_counter() - started) * 1000, 1)
        report["steps"].append(entry)
        return entry.get("result")

    def role_facts():
        with db.get_connection() as conn, conn.cursor() as cur:
            cur.execute("""SELECT current_user, r.rolsuper, r.rolcreaterole, r.rolcreatedb, d.datdba::regrole::text,
                                  current_database(), pg_has_role(current_user, d.datdba, 'MEMBER'),
                                  has_schema_privilege('public', 'CREATE'),
                                  (SELECT max(version) FROM schema_migrations)
                           FROM pg_roles r, pg_database d WHERE r.rolname = current_user AND d.datname = current_database()""")
            name, su, cr, cdb, owner, dbname, member, can_create, ledger = cur.fetchone()
            return {"name": name, "superuser": su, "createrole": cr, "createdb": cdb, "database": dbname, "database_owner": owner,
                    "member_of_owner": member, "create_on_public": can_create, "ledger_max": ledger}
    report["role"] = step("role_facts", role_facts) or {}

    listed = step("memory_list", lambda: [m["id"] for m in db.list_memories(limit=5)]) or []
    first = str(listed[0]) if listed else None
    if first:
        step("memory_read", lambda: {"id": first, "has_content": bool(db.get_memory(first).get("content"))})
        step("memory_graph", lambda: {"edges": len(db.get_relationships(first))})
        step("memories_by_ids_and_provenance", lambda: {"rows": len(db.get_memories_by_ids(listed[:3])),
                                                         "provenance": len(db.get_memory_provenance(listed[:3]))})
    step("search_legs_fetch_candidates", lambda: {k: len(v) for k, v in zip(("vector", "fts", "rows"),
         search_queries.fetch_candidates("second brain retrieval", [0.0] * 1024, 20, status="active"))})
    step("dream_cycle_memory_stats", lambda: {"total": dream_cycle_db.get_memory_stats()["total_count"]})
    step("dream_cycle_feedback_reads", lambda: {"rejections": len(dream_cycle_db.get_recent_rejections(2)),
                                                "user_rejections": len(dream_cycle_db.get_user_rejections(2)),
                                                "dissents": len(dream_cycle_db.get_accepted_dissents(2))})
    step("dream_cycle_tier1_metrics", lambda: sorted(dream_cycle_db.get_tier1_metrics(3).keys()))
    step("dream_cycle_last_briefing_time", lambda: str(dream_cycle_db.get_last_briefing_time()))

    def run_lock():
        with dream_cycle_db.run_lock() as acquired:
            return {"acquired": bool(acquired)}
    step("dream_cycle_run_lock_advisory", run_lock)

    urls = step("capture_processed_source_urls", lambda: len(db.get_processed_source_urls("slack")))
    step("capture_fingerprints", lambda: len(db.get_capture_fingerprints("kiro_cli_chat")))

    def identity_lookup():
        with db.get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT source_type, source_url FROM memories WHERE parent_id IS NULL AND source_url IS NOT NULL AND type='source' ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
        if not row:
            return {"found": None}
        return {"found": db.find_captured_source(row[0], row[1]) is not None}
    step("capture_find_captured_source", identity_lookup)
    step("express_latest_accepted_candidates", lambda: {"candidates": len(express_queries.latest_accepted_candidates()[1])})
    step("express_read_briefing", lambda: {"items": len(express_queries.read_briefing(14, 30, contradiction_limit=3, resurface_limit=3,
                                                                                       resurface_min_age=30, question_limit=3)[0])})

    def write_round_trip_rolled_back():
        marker = f"smoke-app-role-{os.getpid()}"
        try:
            with db.transaction() as conn:
                db.lock_source_identity("manual", f"smoke://{marker}", conn=conn)
                a = db.create_memory("idea", marker, "rolled back", source_type="manual", source_url=f"smoke://{marker}", conn=conn)
                b = db.create_memory("idea", marker + "-b", "rolled back", conn=conn)
                db.create_relationship(a, b, "related_to", conn=conn)
                assert db.get_memory(a, conn=conn)["search_vector"] is not None
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM memory_relationships WHERE source_id IN (%s, %s) OR target_id IN (%s, %s)", (a, b, a, b))
                    cur.execute("DELETE FROM memories WHERE id IN (%s, %s)", (a, b))
                    deleted = cur.rowcount
                raise Rollback
        except Rollback:
            pass
        with db.get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM memories WHERE title LIKE %s", (marker + "%",))
            leaked = cur.fetchone()[0]
        return {"inserted": 2, "deleted_in_txn": deleted, "committed_rows": leaked}
    step("write_round_trip_rolled_back", write_round_trip_rolled_back)

    for name, statement in STRUCTURAL:
        try:
            with db.get_connection() as conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute(statement)
                    report["structural"][name] = "still_allowed"
                finally:
                    conn.rollback()
        except psycopg2.errors.InsufficientPrivilege:
            report["structural"][name] = "refused"
        except Exception as exc:  # noqa: BLE001
            report["structural"][name] = f"error: {type(exc).__name__}"
    db.close_pool()

    app_ok = all(s["ok"] for s in report["steps"])
    report["verdict"] = {
        "application_paths_ok": app_ok,
        "permission_errors": len(report["permission_errors"]),
        "structural_all_refused": all(v == "refused" for v in report["structural"].values()),
        "superuser": report["role"].get("superuser"),
    }
    print(json.dumps(report, indent=args.json_indent, sort_keys=True, default=str))
    return 0 if app_ok and not report["permission_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
