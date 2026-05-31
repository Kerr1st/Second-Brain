# Second Brain — Enhancement Plan: Context-Aware Curation Layer

> Created: 2026-05-18
> Source: Dream cycle insights from run `4d5b8a63-1bbf-47c1-be70-09f34a29317c`
> Digest: `logs/dream-cycle-digest-2026-05-18.md`

## Phase 1: Operating Envelope (Lower Bounds)
**Priority:** Critical — prevents silent data loss
**Effort:** ~2 hours
**From insight:** #7 (The Operating Envelope Principle)

- [ ] Add `captures_this_cycle` counter to qd_sync.sh output
- [ ] Add `captures_this_cycle` counter to chat-extract output
- [ ] Add `captures_this_cycle` counter to backup.sh (verify both GDrive + S3 succeeded)
- [ ] Create a `verify_liveness.sh` script that checks all sources produced output in last 24h (weekdays)
- [ ] Add macOS notification alert when any source goes silent
- [ ] Schedule liveness check daily at 9 AM (new LaunchAgent)

## Phase 2: Soft Annotation at Capture Time
**Priority:** High — reduces embedding costs without losing data
**Effort:** ~3 hours
**From insight:** #3 (Quality Gate Timing Paradox)

- [ ] Add `embed_priority` field to memories table (migration 009)
- [ ] Modify ingest pipeline: annotate with cheap signals (message length, has_numbers, has_decisions, sender_frequency)
- [ ] Skip embedding generation for `embed_priority: low` content (still stored, still in full-text search, just not in vector search)
- [ ] Backfill: classify existing 164K memories and remove embeddings from low-value ones (reclaim HNSW index space)
- [ ] Measure: track embedding API calls before/after to quantify cost savings

## Phase 3: Batch Curator Agent
**Priority:** Medium — the big architectural piece
**Effort:** ~1 day
**From insights:** #3 (Quality Gate Timing Paradox) + #6 (Capture Bounding Already Practiced)

- [ ] Design curator agent prompt: access to active projects, priority deals, recent queries
- [ ] Build context assembly: pull user's recent memory_search queries, active project tags, high-access memories
- [ ] Implement promotion logic: upgrade `embed_priority: low` → generate embedding when context proves relevance
- [ ] Implement demotion logic: downgrade old low-access memories from vector search
- [ ] Schedule: daily or weekly (evaluate frequency after Phase 2 data)
- [ ] Add as new LaunchAgent or expand dream cycle's role

## Phase 4: Per-Channel Friction Calibration
**Priority:** Medium — reduces noise at source
**Effort:** ~3 hours
**From insight:** #1 (The Friction Dial)

- [ ] Create `capture_config.json` with per-channel settings:
  - Slack DMs from known contacts: capture liberally
  - Slack channels (high volume): only threads with mentions or active-project keywords
  - Outlook: meeting summaries + action items only, skip FYI forwards
  - QD monitors: respect importance >= "important" as soft signal
- [ ] Modify ingest pipeline to read channel config and set `embed_priority` accordingly
- [ ] Document each channel's friction setting and failure mode

## Phase 5: Inverse Metric Monitoring
**Priority:** Low — instrument after Phases 1-3 are live
**Effort:** ~2 hours
**From insight:** #2 (Solution Inversion)

- [ ] Track "missed content" rate: content user searches for that was filtered/not-embedded
- [ ] Track "over-consolidation" rate: user retrieves merged memory but needed specific variant
- [ ] Weekly digest comparing noise metrics (memories created) vs coverage metrics (search misses)
- [ ] Add to monitoring checklist in OPERATIONS.md

---

## Context for Next Session

**What was accomplished 2026-05-17:**
- Fixed all 9 LaunchAgents (chat-extract permission, ingest false-failure, dream-cycle + capture-api installed)
- Batch ingested 5,282 Kiro CLI/IDE chats (parallel, 21 min)
- Implemented 6 phases of QD data capture (tags, feed events, interactions, Slack graph, doc chunks, session events)
- 72 new tests, 553 total, all passing
- DR test passed (GDrive backup → decrypt → restore → search works)
- Fixed backup JSON export (COPY double-escaping bug)
- Fixed dream cycle bugs (classify_memory args, update_memory dict serialization)
- Ran dream cycle successfully: 7 insights generated, all accepted
- Memory count: 74K → 164K
- Entity count: 3.2K → 3.9K

**Key files:**
- Dream cycle digest: `logs/dream-cycle-digest-2026-05-18.md`
- This plan: `docs/ENHANCEMENT-PLAN.md`
- All docs updated and synced to GDrive
