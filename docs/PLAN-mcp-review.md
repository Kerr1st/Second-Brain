# Plan: MCP server review — why "it's not working" and what to fix

Branch: `mcp-review` (off `main` at `3efb224`). Started 2026-09-22.
Status legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[-]` dropped (reason in Decisions).
**Resume here after a restart:** read "Status", then the first unchecked task in "Phases".
Commands run from the repo root with `.venv/bin/python`; live DB `memory_bank`; the test suite
uses `memory_bank_test` (created by `tests/conftest.py`). Hermetic run:
`AWS_PROFILE= AWS_ACCESS_KEY_ID= AWS_SECRET_ACCESS_KEY= AWS_DEFAULT_REGION=us-east-1 .venv/bin/pytest -q -p no:cacheprovider`
(1,374 passing at branch point).

## 1. What was checked (2026-09-22 morning)

- Transport: `scripts/jobs/mcp_serve.sh` → `python -m src.mcp_server` (FastMCP, mcp 1.26.0,
  stdio). `initialize` in 0.7–0.9 s, 10 tools listed. Kiro CLI (`~/.kiro/logs/*/mcp.log`) and
  Kiro IDE (`Kiro - MCP Logs.log`) both show `mcp.connect.ok` for `second-brain` on every
  recent session; connect times 1.6–7.6 s (the venv imports boto3/psycopg2/numpy; slow only
  when several servers start at once). Claude Code and Codex have **no** second-brain
  server configured (`~/.claude.json` project entry has `mcpServers: {}`; `~/.codex/config.toml`
  has none) — if the user tested from either, that is the whole story.
- Every tool exercised over stdio with realistic inputs: all respond; read tools 0.0–6 s,
  `memory_brief` 0.1 s, `memory_learn` 3.5 s, `memory_create` 0.5 s.
- Defects found (details in §2):
  1. **Corpus pollution — the real reason search "doesn't work".** One YouTube transcript
     ("5 Red Flags for Weak Abs", `PSROJpBIZ4w`) exists **6,163 times** (4,051 parent rows +
     ~2,110 chunks with embeddings, 69 MB), re-ingested 42×/night at 03:00 since 2026-06-02 by
     `scripts/crawlee_ingest.py`. Root cause: `src/parsers/crawlee.py` yields 42 index rows
     whose URL parses to `""`; `"" in text` matches the first file `os.listdir` returns, so
     all 42 resolve to the same transcript, and `already_processed` (keyed by URL) can never
     skip an empty URL. 645 further rows have their cells shifted (a `|` inside the title), so
     the "URL" is a blog name (one of those re-ingests nightly too: "Research & Deployment").
     Effects: the article channel's counts are ~85% this one video (the "239 articles/week"
     workload figure was mostly it), Monday's skim clustered "42 documents in 1 cluster", the
     Explorer's inventory and the guaranteed slice carry copies, and both search legs return
     duplicates that the near-duplicate filter then has to eat.
  2. **Mutating tools accept anything** (the finding memory `3dbd6505` recorded): `memory_create`
     accepted `type="bogus_type"`, `memory_update` accepted `status="bogus_status"` and
     `type="bogus_type"`, `memory_relate` accepted `relation_type="bogus_relation"`; all landed
     in the live DB (removed again).
  3. **Raw Postgres errors leak to the client**: invalid UUIDs (`memory_read/graph/update`) and a
     missing relate target return the SQL text and the FK constraint name.
  4. Search quality on short queries is weak independent of the pollution: the FTS leg ORs
     every term (`skim | stage`), so common words swamp rare ones; `skim stage` returned nothing
     from the five memories of the last three days that contain "skim".

## 2. Design

- **Parser fix** (`src/parsers/crawlee.py`): parse a row by locating the markdown link
  `[http…](http…)` anywhere in the line (title may contain `|`); skip rows without an http(s)
  URL; dedupe URLs within the index; resolve the source file by an exact `Source: <url>`
  header match built once into a URL→file map (today it re-reads up to 1,352 files per
  entry); never match on an empty string. `scripts/crawlee_ingest.py` refuses empty or
  non-http URLs with a logged SKIP. Unit tests with synthetic index/sources in `tmp_path`.
- **Cleanup** (`scripts/cleanup_duplicate_sources.py`, live DB): for every `source_url` in
  `article`/`youtube` with more than one parent row and identical `md5(content)`, keep the
  oldest parent (and its chunks), copy the rest to `memories_dedup_backup_2026_09_22`
  (same columns) and delete them (FKs cascade: `memory_relationships`, `memory_entities`,
  `memory_skim`; `dream_cycle_candidates.created_memory_id` has no cascade — the script
  aborts if any doomed row is referenced). `--dry-run` prints counts only. Restore =
  `INSERT INTO memories SELECT * FROM memories_dedup_backup_2026_09_22`.
- **MCP hardening** (`src/mcp_server.py`): validate at the boundary and return a clean
  `{"error": ...}` / message instead of raising — `type` ∈ {idea, synthesis, research,
  insight, question, decision, priority, project, connection, source}; `status` ∈ {active,
  superseded, archived}; `relation_type` ∈ {supports, contradicts, extends, inspires,
  derived_from, related_to, superseded_by}; ids must be UUIDs; relate checks both memories
  exist; `limit` clamped 1–100; `title`/`content` non-empty; `tags` a list of non-empty
  strings. No behaviour change for valid calls. Tests in `tests/test_mcp_server.py`.
- **Search**: AND-first FTS (`skim & stage`) falling back to OR when the AND leg returns
  fewer than `limit` rows; measured before/after on five queries, kept only if it helps.

## 3. Constraints

1. No corpus content in subagent prompts or outputs; subagents work on code and synthetic
   fixtures only. Cleanup runs in-process with a backup table.
2. Hermetic suite green at every commit; local branch only, no push; merge on approval.
3. The 03:00 ingest LaunchAgent runs the main tree — the parser fix must land on `main`
   before tonight or the 42 copies come back.

## 4. Phases

### Phase 0 — Diagnosis  `[x]`  (this document, §1)
### Phase 1 — Stop the bleeding: Crawlee parser + ingest guard  `[ ]`
### Phase 2 — Clean the corpus (backup table + delete)  `[ ]`
### Phase 3 — MCP input validation + clean errors  `[ ]`
### Phase 4 — Search: AND-first FTS, measured  `[ ]`
### Phase 5 — Re-probe all tools, update docs (`docs/SKIM-STAGE.md` workload numbers,
`docs/user-guide/connect-ai-agent.md` if it claims Claude Code is wired), merge  `[ ]`

## 5. Status (newest first)

- 2026-09-22 — Diagnosis done; branch `mcp-review` created; probe leftovers removed from the
  live DB (3 synthetic memories, 1 bogus relationship).

## 6. Decisions

- **2026-09-22 Delete duplicates, don't supersede.** `hybrid_search` has no default status
  filter, so superseded rows would still pollute search; deletion with a same-schema backup
  table is reversible and removes 69 MB.

## 7. Useful queries

```sql
-- duplicate sources by URL (parents only)
SELECT source_url, count(*) FROM memories WHERE parent_id IS NULL AND source_type IN ('article','youtube')
 GROUP BY 1 HAVING count(*) > 1 ORDER BY 2 DESC LIMIT 10;
-- the offender
SELECT count(*), count(*) FILTER (WHERE parent_id IS NULL) parents FROM memories
 WHERE source_url = 'https://www.youtube.com/watch?v=PSROJpBIZ4w';
-- nightly ingest log
-- logs/ingest-YYYYMMDD.log  ("OK: " with an empty URL = the bug)
```
