# Second Brain V2 — Implementation Task List

Generated 2026-03-16. Dependency-driven ordering.

## Guiding Principles

- Every task that touches `db.py`, `mcp_server.py`, or `ingest.py` gets tests
- Schema migration is one batch (all three columns), applied once
- Reranking changes are sequenced so each one is testable in isolation
- Ingest-time changes come after the reranking formula is stable
- Consolidation is last — it benefits from all Layer 1 improvements

## Dependency Graph

```
Task 0: Test Infrastructure
  │
Task 1: Schema Migration (mem_class, project, last_accessed_at)
  │
  ├── Task 2: Spaced Retrieval ──────────────────────┐
  │     (last_accessed_at + rerank formula)           │
  │                                                   │
  ├── Task 3: Memory Classification ─────────────┐    │
  │     (classify.py + ingest + rerank boost)    │    │
  │                                              │    │
  ├── Task 4: Depth Scoring ─────────────────┐   │    │
  │     (numeric score + rerank factor)      │   │    │
  │                                          │   │    │
  ├── Task 5: Project Scoping ───────────┐   │   │    │
  │     (project col + rerank penalty)   │   │   │    │
  │                                      │   │   │    │
  ├── Task 6: Relationship Discovery ────┤   │   │    │
  │     (post-ingest search + relate)    │   │   │    │
  │                                      │   │   │    │
  └── Task 7: Temporal Contiguity ───────┤   │   │    │
        (search result enrichment)       │   │   │    │
                                         │   │   │    │
                              All rerank changes merged
                                         │
                              Task 8: Consolidation Pipeline
                                (clustering + templates + storage)
                                         │
                              Task 9: Doc Sync
                                (update all docs to match reality)
```

Tasks 2–5 each modify `rerank()` independently. They don't depend on each
other, but are sequenced to keep diffs reviewable. Tasks 3, 5, and 6 also
modify `ingest.py`. Task 7 modifies `memory_search` output. Task 8 depends
on classification existing (to cluster by `mem_class`).

---

## Task 0: Test Infrastructure

**Why:** Every subsequent task modifies core search/ranking behavior against
74K live memories. Without regression tests, we can't verify V2 changes
don't degrade V1 quality.

**Scope:**
- Add `pytest`, `pytest-cov` to `requirements.txt`
- Create `tests/conftest.py` with:
  - Test database fixture (`memory_bank_test` DB in same Docker container)
  - Schema setup/teardown (run migrations against test DB)
  - Connection fixture that overrides `db.DB_CONFIG` for test runs
  - Sample memory factory fixture (creates memories with known content)
  - Embedding mock fixture (deterministic fake embeddings, no Bedrock calls)
- Create `tests/test_db.py` — baseline tests for current behavior:
  - `test_create_and_get_memory`
  - `test_hybrid_search_returns_results`
  - `test_rerank_preserves_order_for_exact_match`
  - `test_increment_access_count`
  - `test_create_and_get_relationship`
- Create `tests/test_mcp_server.py` — smoke tests for tool functions:
  - `test_memory_create_returns_id`
  - `test_memory_search_returns_list`
  - `test_memory_create_depth_warning`
- Create `pytest.ini` or `pyproject.toml` `[tool.pytest]` section
- Verify: `pytest tests/ -v` passes with Docker running

**Files created:**
- `tests/__init__.py`
- `tests/conftest.py`
- `tests/test_db.py`
- `tests/test_mcp_server.py`
- `pytest.ini`

**Files modified:**
- `requirements.txt` (add pytest, pytest-cov)

**Definition of done:** `pytest tests/ -v` runs, all baseline tests pass,
no Bedrock calls during tests (embeddings mocked).

---

## Task 1: Schema Migration

**Why:** Tasks 2–7 all need at least one of these columns. Doing them in
one migration avoids multiple ALTER TABLE passes on a 74K-row table.

**Scope:**
- Create `migrations/002_v2_columns.sql`:
  ```sql
  ALTER TABLE memories ADD COLUMN IF NOT EXISTS mem_class TEXT;
  ALTER TABLE memories ADD COLUMN IF NOT EXISTS project TEXT;
  ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ;

  CREATE INDEX IF NOT EXISTS idx_memories_mem_class ON memories (mem_class);
  CREATE INDEX IF NOT EXISTS idx_memories_project ON memories (project);
  CREATE INDEX IF NOT EXISTS idx_memories_last_accessed ON memories (last_accessed_at DESC);

  COMMENT ON COLUMN memories.mem_class IS 'Memory classification: semantic, episodic, procedural (Tulving 1972)';
  COMMENT ON COLUMN memories.project IS 'Project scope for encoding specificity (Tulving & Thomson 1973)';
  COMMENT ON COLUMN memories.last_accessed_at IS 'Last retrieval time for spaced retrieval (Roediger & Karpicke 2006)';
  ```
- Update `ALLOWED_UPDATE_FIELDS` in `db.py` to include `mem_class`, `project`, `last_accessed_at`
- Update `create_memory()` signature in `db.py` to accept `mem_class` and `project` params
- Write migration test: `tests/test_migrations.py` — verify columns exist after migration
- Run migration: `bash migrations/migrate.sh`

**Files created:**
- `migrations/002_v2_columns.sql`
- `tests/test_migrations.py`

**Files modified:**
- `src/db.py` (ALLOWED_UPDATE_FIELDS, create_memory signature)

**Definition of done:** Migration applies cleanly, new columns exist,
`create_memory()` accepts new params, existing tests still pass.

---

## Task 2: Spaced Retrieval

**Why:** Prevents popularity bias. A memory retrieved 50 times in one
session shouldn't permanently outrank one retrieved weekly for a year.
Smallest rerank change — good first test of the modification pattern.

**Research:** Roediger & Karpicke 2006, Bjork 1975 (desirable difficulties).

**Scope:**
- Modify `increment_access_count()` in `db.py` to also set
  `last_accessed_at = now()` on each retrieval
- Modify `rerank()` in `db.py`:
  - Read `last_accessed_at` from each result
  - Compute `spacing_bonus = min(1.0, days_since_last_access / 7.0)`
    (0.0 if accessed today, 1.0 if 7+ days ago, None/never → 1.0)
  - Change reinforcement line from:
    `reinforcement = 0.03 * log1p(access_count)`
    to:
    `reinforcement = 0.03 * log1p(access_count) * spacing_bonus`
- Backfill: set `last_accessed_at = updated_at` for all existing memories
  (one-time SQL in migration or a separate backfill script)
- Tests in `tests/test_spaced_retrieval.py`:
  - `test_spacing_bonus_zero_for_same_day_access`
  - `test_spacing_bonus_one_for_week_old_access`
  - `test_spacing_bonus_one_for_never_accessed`
  - `test_rerank_with_spaced_retrieval` (two memories, same access_count,
    different last_accessed_at — verify the spaced one ranks higher)

**Files modified:**
- `src/db.py` (increment_access_count, rerank)

**Files created:**
- `tests/test_spaced_retrieval.py`
- Optionally: `migrations/003_backfill_last_accessed.sql`

**Definition of done:** Retrieval updates `last_accessed_at`, reranking
applies spacing bonus, tests pass, existing search tests still pass.

---

## Task 3: Memory Classification

**Why:** Semantic memories (principles, decisions) should outrank episodic
memories (raw chat logs) in search results. This is the biggest retrieval
quality improvement in V2.

**Research:** Tulving 1972, Squire 1992.

**Scope:**
- Create `src/classify.py`:
  - `classify_memory(type: str, content: str) -> str` — returns
    "semantic", "episodic", or "procedural"
  - Classification rules (deterministic, no LLM needed):
    - `type` in (idea, synthesis, insight, decision, connection) → semantic
    - `type` in (source,) AND `source_type` in (kiro_cli_chat, kiro_ide_chat) → episodic
    - `type` in (source,) AND `source_type` in (youtube, article, pdf, course) → episodic
    - `type` in (priority, project, question) → semantic
    - Content signal override: if content contains procedural markers
      (step-by-step, "how to", numbered instructions) → procedural
    - Default: episodic
- Modify `ingest.py` → `ingest_content()`: call `classify_memory()`,
  pass `mem_class` to `create_memory()`
- Modify `mcp_server.py` → `memory_create()`: call `classify_memory()`,
  pass `mem_class` to `create_memory()`
- Modify `rerank()` in `db.py`:
  - Add `mem_class` boost: semantic +0.04, procedural +0.02, episodic +0.00
  - This stacks with the existing `type_boost` (they measure different things:
    type_boost rewards ideas/insights specifically, mem_class_boost rewards
    the broader category of abstracted knowledge)
- Backfill script: `scripts/backfill_mem_class.py` — classify all 74K
  existing memories based on their `type` and `source_type` fields
  (no content analysis needed for backfill, just the type mapping)
- Tests in `tests/test_classify.py`:
  - `test_classify_idea_is_semantic`
  - `test_classify_chat_source_is_episodic`
  - `test_classify_howto_content_is_procedural`
  - `test_rerank_boosts_semantic_over_episodic` (two memories with same
    RRF score, different mem_class — verify semantic ranks higher)

**Files created:**
- `src/classify.py`
- `scripts/backfill_mem_class.py`
- `tests/test_classify.py`

**Files modified:**
- `src/db.py` (rerank)
- `src/ingest.py` (ingest_content)
- `src/mcp_server.py` (memory_create)

**Definition of done:** New memories get classified automatically,
reranking boosts semantic memories, backfill script ready to run,
all tests pass.

---

## Task 4: Depth Scoring

**Why:** Upgrades binary depth enforcement (warn/don't-warn) to a numeric
signal in reranking. A deep explanation of WHY should rank above a shallow
bullet list, even if both are the same `type` and `mem_class`.

**Research:** Craik & Lockhart 1972 (levels of processing).

**Scope:**
- Create `src/depth.py`:
  - `compute_depth_score(content: str) -> float` — returns 0.0 to 1.0
  - Scoring signals (each contributes to the score):
    - Causal connectors: "because", "when...then", "which causes/leads/means",
      "so that", "the fix was", "this means" — reuse `_DEPTH_RE` from
      mcp_server.py (move regex to depth.py, import in mcp_server.py)
    - Concrete examples: code blocks, specific numbers, named tools/libraries
    - "Questions this answers:" section present
    - Content length (longer ≠ deeper, but very short is usually shallow)
    - Connection to other knowledge: "extends", "contradicts", "relates to"
  - Formula: weighted sum of signal counts, normalized to 0.0–1.0
- Modify `ingest.py` → `ingest_content()`: compute depth_score, store in
  `metadata.depth_score`
- Modify `mcp_server.py` → `memory_create()`: compute depth_score, store
  in metadata, use for depth warnings (replace binary regex check)
- Modify `rerank()` in `db.py`:
  - Read `depth_score` from `metadata` JSONB
  - Add depth factor: `0.05 * depth_score` (small but meaningful)
- Tests in `tests/test_depth.py`:
  - `test_shallow_content_scores_low`
  - `test_deep_content_scores_high`
  - `test_depth_score_range` (always 0.0–1.0)
  - `test_rerank_boosts_deep_over_shallow`

**Files created:**
- `src/depth.py`
- `tests/test_depth.py`

**Files modified:**
- `src/db.py` (rerank)
- `src/ingest.py` (ingest_content)
- `src/mcp_server.py` (memory_create — replace _DEPTH_RE usage)

**Definition of done:** Depth score computed and stored at ingest time,
reranking uses it, depth warnings use numeric score instead of binary
regex, all tests pass.

---

## Task 5: Project Scoping

**Why:** Prevents cross-project pollution. A React convention from Project A
shouldn't override a Python decision in Project B.

**Research:** Tulving & Thomson 1973 (encoding specificity).

**Scope:**
- Modify `ingest.py` → `ingest_content()`: accept optional `project` param,
  pass to `create_memory()`
- Modify `mcp_server.py`:
  - `memory_create()`: accept optional `project` param
  - `memory_search()`: accept optional `project` param for scoping
- Modify `hybrid_search()` in `db.py`: add optional `project` filter
  (WHERE clause when project is specified)
- Modify `rerank()` in `db.py`:
  - Accept optional `query_project` param
  - If `query_project` is set and memory's `project` differs:
    apply -0.15 penalty (cross-project demotion)
  - If memory has no `project` set: no penalty (universal knowledge)
- Modify `memory_search` in `mcp_server.py` to pass project through
- Tests in `tests/test_project_scoping.py`:
  - `test_same_project_no_penalty`
  - `test_cross_project_penalty_applied`
  - `test_no_project_no_penalty` (universal memories)
  - `test_search_with_project_filter`

**Files created:**
- `tests/test_project_scoping.py`

**Files modified:**
- `src/db.py` (hybrid_search, rerank)
- `src/ingest.py` (ingest_content)
- `src/mcp_server.py` (memory_create, memory_search)

**Definition of done:** Memories can be tagged with a project, search can
filter by project, cross-project results are demoted, all tests pass.

---

## Task 6: Automatic Relationship Discovery

**Why:** The relationship graph is currently empty (no automatic connections).
If you worked on a problem across 5 chats in one day, retrieving one should
surface the others.

**Research:** Howard & Kahana 2002 (temporal context model), elaborative
encoding.

**Scope:**
- Modify `ingest.py` → `ingest_content()`: after storing parent + chunks,
  run relationship discovery:
  1. Semantic neighbors: embed the parent content, search for top-3 similar
     existing memories (excluding self and own chunks), create `related_to`
     relationships if similarity > threshold (e.g., 0.75)
  2. Temporal neighbors: find memories created within ±24 hours of the new
     memory's created_at, create `related_to` relationships for the top-3
     by recency
- Add `discover_relationships(memory_id, content, embedding)` function
  to `ingest.py` (or a new `src/relationships.py` if it gets complex)
- Use `search_similar()` from `db.py` for semantic neighbor lookup
- Use a new `find_temporal_neighbors()` in `db.py` for time-based lookup
- Guard: skip relationship discovery for chunk records (only run on parents)
- Guard: cap at 3 semantic + 3 temporal relationships per memory to avoid
  graph explosion
- Tests in `tests/test_relationships.py`:
  - `test_semantic_neighbor_discovered`
  - `test_temporal_neighbor_discovered`
  - `test_chunks_skip_relationship_discovery`
  - `test_max_relationships_capped`

**Files created:**
- `tests/test_relationships.py`
- Possibly `src/relationships.py`

**Files modified:**
- `src/db.py` (add find_temporal_neighbors)
- `src/ingest.py` (post-store relationship discovery)

**Definition of done:** New memories automatically get related to similar
and temporally adjacent memories, relationship graph starts populating,
all tests pass.

---

## Task 7: Temporal Contiguity

**Why:** When you retrieve a memory, you often want the context around it —
what else were you working on that day? This surfaces temporal neighbors
alongside search results.

**Research:** Howard & Kahana 2002 (temporal context model).

**Scope:**
- Modify `memory_search` in `mcp_server.py`:
  - After getting ranked results, for the top result (or top-N),
    query for temporal neighbors (±24 hours) not already in results
  - Append them as a separate `"temporal_context"` list in the response
  - Each entry: `{id, title, type, created_at, relation: "temporal_neighbor"}`
  - Limit to 3 temporal neighbors to keep response size reasonable
- Reuse `find_temporal_neighbors()` from Task 6
- Tests in `tests/test_temporal.py`:
  - `test_temporal_neighbors_returned`
  - `test_temporal_neighbors_exclude_duplicates`
  - `test_temporal_neighbors_limit`

**Files created:**
- `tests/test_temporal.py`

**Files modified:**
- `src/mcp_server.py` (memory_search)

**Definition of done:** Search results include temporal context for top
results, no duplicates, all tests pass.

---

## Task 8: Consolidation Pipeline (Python parts)

**Why:** Transforms 74K raw episodic memories into reusable semantic
principles. This is the single biggest quality improvement possible —
turning raw chat logs into searchable knowledge.

**Research:** Stickgold 2005, Walker 2009 (sleep consolidation).

**Scope:**
- Create `src/consolidate.py`:
  - `cluster_episodic_memories(since_days=7) -> list[Cluster]`
    - Query episodic memories from the last N days
    - Cluster by tag overlap + embedding similarity (simple agglomerative
      or keyword-based grouping — start simple)
    - Each cluster: list of memory IDs, shared tags, representative title
  - `generate_template(cluster: Cluster) -> str`
    - Build a consolidation prompt template with:
      - Source excerpts (trimmed to key passages)
      - Shared themes/tags
      - The depth framework: WHAT / WHAT HAPPENS when violated / WHY
      - "Questions this answers:" section requirement
    - Returns the template string (ready for LLM input)
  - `store_consolidated(title, content, source_ids, project=None) -> str`
    - Create a new semantic memory via `create_memory()`
    - Set `mem_class="semantic"`, `type="synthesis"`
    - Set `metadata.consolidated_from = source_ids`
    - Create `derived_from` relationships to each source memory
    - Returns the new memory ID
  - `synthesize(template: str) -> str`
    - Pluggable LLM interface: takes template, returns synthesized text
    - Default implementation: raises NotImplementedError with a message
      pointing to Kiro headless integration
    - For testing: accepts a mock/callable
- Create `scripts/weekly_synthesis.py`:
  - CLI entry point for the consolidation pipeline
  - Calls cluster → template → synthesize → store for each cluster
  - Writes a digest summary (how many clusters, how many consolidated
    memories created, which topics)
  - Logs to `logs/consolidation.log`
  - Exit codes: 0 success, 1 partial failure, 2 total failure
- Create `scheduling/com.second-brain.synthesis.plist`:
  - Weekly Sunday 4:00 AM (after backup at 2:00 AM, after chat extract
    at 2:30 AM, after staged ingest at 3:00 AM)
  - Uses `scripts/job_wrapper.sh` for failure notifications
- Tests in `tests/test_consolidate.py`:
  - `test_cluster_groups_similar_memories`
  - `test_generate_template_includes_depth_framework`
  - `test_store_consolidated_creates_relationships`
  - `test_pipeline_end_to_end_with_mock_synthesize`

**Files created:**
- `src/consolidate.py`
- `scripts/weekly_synthesis.py`
- `scheduling/com.second-brain.synthesis.plist`
- `tests/test_consolidate.py`

**Files modified:**
- None (new module, no changes to existing code)

**Definition of done:** Clustering and template generation work against
test data, storage creates proper relationships, pipeline runs end-to-end
with a mock synthesizer, launchd plist ready to install. Kiro headless
integration is a documented follow-up.

---

## Task 9: Documentation Sync

**Why:** SESSION-CONTEXT.md, AGENTS.md, V2-ROADMAP.md, and
DESIGN-DECISIONS.md all need to reflect the actual implementation.
Future sessions depend on accurate docs.

**Scope:**
- Update `docs/SESSION-CONTEXT.md`:
  - Mark completed V2 items
  - Update schema section with new columns
  - Update search architecture section with new reranking factors
  - Update "How to Resume" section
  - Reconcile Layer 1/Layer 2 grouping with actual implementation order
- Update `docs/AGENTS.md`:
  - Check off completed V2 items in the checklist
  - Update schema DDL block
  - Update search architecture description
  - Add test infrastructure to project status
- Update `docs/V2-ROADMAP.md`:
  - Mark completed items
  - Add implementation notes (what was actually built vs planned)
  - Note any deviations from the original design
- Update `docs/DESIGN-DECISIONS.md`:
  - Add decisions made during V2 implementation
  - Document the dependency-driven ordering rationale
  - Document the test infrastructure decision

**Files modified:**
- `docs/SESSION-CONTEXT.md`
- `docs/AGENTS.md`
- `docs/V2-ROADMAP.md`
- `docs/DESIGN-DECISIONS.md`

**Definition of done:** All four docs accurately reflect the implemented
state. A new agent reading SESSION-CONTEXT.md gets the correct picture.

---

## Summary Table

| Task | Name | Modifies | New Files | Depends On |
|------|------|----------|-----------|------------|
| 0 | Test Infrastructure | requirements.txt | tests/conftest.py, tests/test_db.py, tests/test_mcp_server.py, pytest.ini | — |
| 1 | Schema Migration | db.py | migrations/002_v2_columns.sql, tests/test_migrations.py | 0 |
| 2 | Spaced Retrieval | db.py | tests/test_spaced_retrieval.py | 1 |
| 3 | Memory Classification | db.py, ingest.py, mcp_server.py | src/classify.py, scripts/backfill_mem_class.py, tests/test_classify.py | 1 |
| 4 | Depth Scoring | db.py, ingest.py, mcp_server.py | src/depth.py, tests/test_depth.py | 1 |
| 5 | Project Scoping | db.py, ingest.py, mcp_server.py | tests/test_project_scoping.py | 1 |
| 6 | Relationship Discovery | db.py, ingest.py | tests/test_relationships.py, possibly src/relationships.py | 1 |
| 7 | Temporal Contiguity | mcp_server.py | tests/test_temporal.py | 6 |
| 8 | Consolidation Pipeline | — | src/consolidate.py, scripts/weekly_synthesis.py, scheduling/com.second-brain.synthesis.plist, tests/test_consolidate.py | 3 |
| 9 | Doc Sync | docs/*.md | — | all |

## Reranking Formula After All Tasks

Current V1:
```
rerank_score = 0.35 * rrf_score
             + 0.20 * token_overlap
             + 0.20 * title_overlap
             + 0.15 * recency
             + 0.10 * length_score
             + type_boost          (0.06 for idea/synthesis/insight/decision)
             + reinforcement       (0.03 * log1p(access_count))
```

After V2 (Tasks 2–5):
```
rerank_score = 0.30 * rrf_score
             + 0.18 * token_overlap
             + 0.18 * title_overlap
             + 0.12 * recency
             + 0.08 * length_score
             + 0.05 * depth_score
             + type_boost          (0.06 for idea/synthesis/insight/decision)
             + mem_class_boost     (0.04 semantic, 0.02 procedural)
             + reinforcement       (0.03 * log1p(access_count) * spacing_bonus)
             + project_penalty     (-0.15 if cross-project)
```

Note: base weights reduced slightly (sum from 1.00 to 0.86) to make room
for depth_score (0.05) while keeping the total in a similar range. The
boosts/penalties are additive on top. This will need tuning with real
queries — the test infrastructure from Task 0 enables that.
