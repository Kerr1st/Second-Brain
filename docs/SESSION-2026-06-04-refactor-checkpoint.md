# Checkpoint — Retrieval-Value Refactor (2026-06-04)

Resume doc for the refactor in `docs/REFACTOR-PLAN.md`. Read that for the full plan;
this captures current state, hard-won learnings, and exactly what to do next.

## Where we are
- Branch `quick_desktop_integration`, **HEAD `63cd332`**, clean tree, pushed to both
  remotes (`mini` + `origin`). 551 tests pass. All 5 liveness channels green.
- DB: **114,124 memories** (distilled_chat 1,419 = 683 decisions + 736 insights;
  kiro_ide_chat 73,250 with **0 legacy** left; was 187K before P2b).
- Pre-P2b backup: `~/sb-backups/pre-p2b-dedup-20260603-170127.sql.gz` (1.6 GB).

## Done
- **P1 — distill on ingest:** `scripts/distill_sessions.py` (uses `AgentInvoker` →
  kiro-cli) distills chat-session parents into `type=decision|insight`,
  `source_type=distilled_chat`, `source_url distill://{session}#{n}` (idempotent).
  Backfilled cli+qd (370 sessions → 1,419 memories). Scheduled daily 05:00 via
  `com.second-brain.distill` (symlinked + loaded). Test: `tests/test_distill.py`.
- **P2a — retrieval dedup:** `hybrid_search` fetches a larger pool, drops
  near-identical content, caps 2 per parent. Fixed the duplicate-flooding.
- **P2b — IDE dedup:** deleted 73,257 legacy `ide_*.md` rows (all had `chat://`
  twins) via batched background job `scripts/jobs/dedup_ide_backfill.sh`.

## Benchmark (success metric)
Re-run: `.venv/bin/python scripts/eval/refactor_probe.py`
- baseline → now: distilled-in-top5 **3/12 → 8/12** (target met); decision-recall
  **0/4 → 3/4**; near-dup **9/12 → 0/12**.

## Key learnings (don't relearn the hard way)
1. **kiro-cli moved conversation storage** to `~/.kiro/sessions/cli/*.jsonl` (~Mar
   2026); the old `data.sqlite3 conversations_v2` is dead. The cli_chat parser reads
   the JSONL store now.
2. **Distiller prompt must frame input as a transcript to analyze, not reply to**
   ("do not reply to it" + triple-quote delimit) — else kiro-cli continues the
   conversation instead of distilling. See `build_user_message`.
3. **Bulk HNSW deletes are pathologically slow** (~37ms/row) and a single delete
   holds locks for tens of minutes (looks like a hang). Always **batch** + run
   **`VACUUM ANALYZE` after** — skipping the vacuum dropped recall 7/12→5/12;
   vacuum restored it to 8/12. (Now baked into `dedup_ide_backfill.sh`.)
4. A cancelled `docker exec psql` leaves the **server-side query running** (orphan).
   Check `pg_stat_activity`; `pg_terminate_backend(pid)` rolls it back safely.

## Next steps (in order)
1. **IDE distillation backfill (unblocked by P2b):** ~5,493 canonical IDE sessions.
   Run `.venv/bin/python scripts/distill_sessions.py --source-type kiro_ide_chat`
   in the background — **~25–30 hrs, ~5,500 LLM calls** (idempotent; safe to stop/
   resume). DECISION PENDING: full run vs scope to recent/most-accessed first.
   (No vacuum needed — distillation only inserts.)
2. **P3 — weekly digest:** aggregate tags/entities/dates + 1 LLM summary → a
   `synthesis` "weekly digest" memory + notification. Targets the one failing query
   class ("what am I spending my time on"). Additive/safe. `dream_cycle/digest.py`
   may be reusable.
3. **P4 — wire KG into retrieval:** entity-match query → expand via graph
   (memory_entities + 1-hop) into candidate set before rerank.

## Minor / deferred
- Distiller 5-item cap dropped the "retire session_events" decision (→ 3/4 not 4/4);
  raising the cap or re-distilling that session would likely fix it.
- `normalize_project_tag` sets chat `project` to session UUIDs (meaningless) — hygiene.
- Supersession: an older "dual-site incl. S3" decision can outrank the newer "S3
  dropped" one — consider marking superseded.
- `scripts/backfill_projects.py` still reads dead `conversations_v2` (non-pipeline).
