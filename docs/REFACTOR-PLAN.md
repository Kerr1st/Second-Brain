# Second Brain — Retrieval-Value Refactor Plan

Supersedes the prior capture-enhancement plan (its Phase 1 shipped; remaining
ideas parked at the end of this doc). Created 2026-06-03.

## Why
Purpose of the system: persistent memory that makes agents *thinking partners* —
the value is in **retrieval of distilled knowledge**, not accumulation.

Evaluation (2026-06-03, two rounds of real `hybrid_search + rerank` probes) found
the system has inverted that purpose — it is write-mostly:

- 185,763 memories; **0.83% ever retrieved**; ~674 distinct touched in 30 days.
- **99.8% are `type=source`** (raw); only 5 syntheses + 133 insights exist.
- Decision recall fails: "S3 de-scope" → 202 matches in store, **0 distilled**;
  "cli_chat JSONL" → 246 matches, 0 distilled. The reasoning exists only as raw
  chat chunks, so it never surfaces.
- **Distilled-in-top5: 3/12** queries. **Near-duplicate-in-top10: 9/12.**
- ~39% of the store is duplicate IDE chat (5,493 chats under two `source_url`
  schemes — `chat://{id}` and `ide_{id}.md`).
- The KG (25K entities / 41K edges: Kiro pipeline, products, people, goals) is
  rich but unused in retrieval (only 1,861 memory↔entity links).

Capture is healthy; **distillation is the missing half.**

## Principles
- Surgical, minimal change; reuse existing infra (embeddings, `create_memory`,
  the Bedrock LLM the dream cycle uses).
- Protect what works: live capture (CLI/IDE/QD/YouTube), document retrieval, KG
  data. S3 stays de-scoped.
- Measure every phase against the round-2 probe benchmark. Destructive steps are
  gated on explicit approval (dry-run + review first).

## Benchmark (re-run after each phase)
| Metric | Baseline | Target |
|---|---|---|
| Distilled memory in top-5 | 3/12 | ≥ 8/12 |
| Near-duplicate in top-10 | 9/12 | ≤ 2/12 |
| Decision recall (S3, JSONL, session_events) | 0 | crisp hit in top-3 |

## Pillars (priority order)

### P1 — Distill on ingest (anchor)
Lean distiller: one LLM pass per chat session → 0–5 structured `decision`/`insight`
memories ("Decided X because Y", "Learned Z"), `source_url distill://{session}/{n}`
for idempotency. Raw chunks stay for deep recall; distilled layer ranks for
reasoning queries. Reuse `generate_embedding` + `create_memory` + Bedrock client.
- **Scope (decided):** backfill last 90 days of chat sessions + all future on a
  schedule; expand if it proves out.
- **Phase-1 step 1:** assess `src/dream_cycle/` for reuse vs a new lean distiller
  (do not run two synthesis systems long-term).
- **Validate:** the three decision queries return a distilled decision in top-3.

### P2 — Dedup + cut raw-chunk noise
- (a) Retrieval-time dedup (safe, non-destructive): in `rerank`, collapse results
  sharing `parent_id`/near-identical content to the best one.
- (b) Data cleanup (destructive, gated): delete the legacy `ide_*.md` scheme
  (keep `chat://`), ~73K redundant rows. **Dry-run + review + backup first.**
- Hygiene: fix `normalize_project_tag` (chats get UUID "projects"); stop retired
  `quick_desktop_session_event` rows surfacing.

### P3 — Aggregate / proactive digest
Weekly job: aggregate tags/entities/dates + one LLM summarization → a `synthesis`
"weekly digest" memory + notification ("what you're working on / learning"). The
proactive half of the purpose; answers questions retrieval cannot.

### P4 — Wire the KG into retrieval
Detect query entities, expand via graph (linked memories + 1-hop neighbors) into
the candidate set before rerank; extend entity-linking on ingest. Most complex;
phased last; incremental.

## Sequence
P1 → P2a → P2b (after approval) → P3 → P4. Each phase: minimal code, tests,
re-run benchmark, commit + push both remotes.

## Decisions (settled 2026-06-03)
1. Distiller scope: 90 days first, expand if it proves out.
2. Dream cycle: leave running; revisit after P1/P3.
3. P2b IDE dedup: dry-run + review before any deletion.

## Absorbs prior backlog
#7 (IDE 2× dedup) → P2b. #3/#6 (monitoring/tests) folded in as those areas are
touched.

## Progress
- 2026-06-03: **P1** distiller built, backfilled (cli+qd: 1,419 distilled memories), and scheduled daily (com.second-brain.distill). **P2a** retrieval dedup shipped (hybrid_search collapses near-dups, caps per parent). Benchmark: distilled-in-top5 3→7/12, near-dup 9→0/12, decision-recall 0→3/4.
- 2026-06-04: **P2b** complete — deleted 73,257 legacy `ide_*.md` rows (187K→114K total; all 5,493 chats preserved via `chat://`). Done as a batched background job (single bulk delete is pathologically slow on the HNSW index). NOTE: a bulk HNSW delete REQUIRES `VACUUM ANALYZE` after — skipping it dropped distilled-in-top5 to 5/12; the vacuum restored it to **8/12** (target met), decision-recall 3/4, near-dup 0/12. Full pg_dump backup taken first (~/sb-backups/).
- Next: IDE distillation (now unblocked — ~5,493 canonical sessions, scope before running), then P3 (digest), P4 (KG).

## Parked ideas (salvaged from the retired enhancement plan)
Unbuilt, not part of the current P1–P4 sequence, but worth revisiting:
- **embed_priority tiering** — skip vector embeddings for low-value content (keep it
  in full-text), and demote old low-access memories out of the HNSW index to reclaim
  space/cost. Directly relevant to the HNSW bloat seen in P2b.
- **Per-channel friction config** (`capture_config.json`) — capture liberally vs.
  selectively per source (Slack DMs vs. channels, Outlook summaries-only). Ties into
  the future Slack/Outlook capture work.
- **Inverse-metric monitoring** — track "missed content" (searched-for but filtered)
  and over-consolidation rates, weekly.
