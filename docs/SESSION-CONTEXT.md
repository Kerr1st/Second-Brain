# Second Brain — Session Context (2026-03-08 through 2026-03-13)

Complete context for continuing V2 development. Read this if you need
background on decisions, architecture, or current state.

## What Exists (V1 Complete)

### Database
- PostgreSQL + pgvector running in Docker (`second-brain-db`, port 5432)
- Credentials: memory_bank/memory_bank/memory_bank
- **74,013 memories** (73,257 IDE chat + 752 CLI chat + 4 YouTube)
- Schema: memories table (id, type, title, content, summary, embedding vector(1024),
  tags, source_url, source_type, metadata JSONB, status, confidence, parent_id,
  created_at, updated_at, search_vector TSVECTOR, access_count INTEGER)
- memory_relationships table (source_id, target_id, relation_type, note)
- Trigger auto-populates search_vector on INSERT/UPDATE
- Embedding model: Amazon Bedrock Titan Embed Text v2, 1024 dimensions

### MCP Server (8 tools)
- `memory_create` — with depth enforcement (warns if shallow)
- `memory_search` — hybrid retrieval (BM25 + vector + RRF + utility reranking)
- `memory_read` — full content with datetime serialization
- `memory_update` — explicit params, field allowlist, re-embeds on content change
- `memory_relate` — typed relationships (supports, contradicts, extends, etc.)
- `memory_list` — filtered listing
- `memory_graph` — memory + all relationships
- `memory_learn` — elaborative interrogation (V2 item #6, done)
- Runs via `scripts/mcp_serve.sh` wrapper (handles cd to project root)
- Config in `~/.kiro/settings/mcp.json` as "second-brain"
- Read tools auto-approved, write tools require approval

### Search Architecture (Hybrid Retrieval)
- BM25 full-text via PostgreSQL tsvector/tsquery + GIN index
- Vector cosine via pgvector HNSW index
- Reciprocal Rank Fusion (RRF): score = 1/(k+rank_vec) + 1/(k+rank_bm25), k=60
- Utility reranking: fusion (0.35) + token overlap (0.20) + title overlap (0.20) +
  recency decay τ=60d (0.15) + content length (0.10) + type boost + access_count reinforcement
- Retrieval reinforcement: access_count incremented on every search, boost = 0.03 * log(1 + count)

### Ingestion Pipeline
- `src/ingest.py`: parse_metadata_header → chunk_by_section (4000 char max) → embed → store
- Parent record (full content, no embedding) + child chunks (with embeddings)
- `src/parsers/`: crawlee.py, cli_chat.py, ide_chat.py
- `scripts/chat_extract.py`: Phase 1 deterministic stripping, backfill complete (5,561 files)
- `scripts/ingest_chats.py`: batch ingest, completed all 5,561 → 74,013 memories
- `scripts/scrape_bookmarks.py`: Chrome bookmarks with cookie auth for internal Amazon links + Crawlee batch for public
- `scripts/crawlee_ingest.py`: daily Crawlee knowledge store ingestion

### Scheduled Jobs (all launchd plists installed)
| Time | Job | Script | Status |
|---|---|---|---|
| Sat 1:00 AM | Bookmark scrape | scrape_bookmarks.py | Working |
| Daily 1:30 AM | YouTube scrape | youtube_scrape.sh | Stubbed (needs playlist URL) |
| Daily 2:00 AM | Backup | backup.sh | Working (Google Drive OK, S3 partial) |
| Daily 2:30 AM | Chat extraction | chat_extract.py | Working |
| Daily 3:00 AM | Staged ingestion | ingest_staged.sh | Stubbed (Kiro headless, untested) |
| Sun 3:00 AM | Backup verify | verify_backup.sh | Working |

All jobs use `scripts/job_wrapper.sh` which sends macOS notification on failure.

### Backup & DR
- GPG AES-256 encryption with `~/second-brain/.backup-key`
- rclone configured: `gdrive:` (Google Drive) + `s3:` (AWS S3, env_auth)
- S3 bucket: `memory-bank-backups`
- Encryption key stored locally + Google Secret Manager + AWS Secrets Manager
- Full recovery procedures in `docs/DISASTER-RECOVERY.md`

### Crawlee Integration (~/Work/Tools/Crawlee/)
- Content acquisition layer for Second Brain
- Web scraper + YouTube transcript extractor
- All content writes to `knowledge/sources/` with metadata headers
- Registers in `knowledge/index.md` for discovery
- `batch-scrape.js`: single Crawlee instance for URL lists (3 concurrent pages)
- Product steering doc explains Second Brain relationship

### kiro-memory Reference (~/KiromemoryMCP/mainline/)
- Inspiration project for hybrid retrieval and cognitive science features
- Key ideas we adopted: hybrid search, RRF, utility reranking, retrieval reinforcement,
  depth enforcement, memory_learn, consolidation concept
- Key ideas we improved on: PostgreSQL vs in-memory Qdrant, relationship graph,
  chunking pipeline, multi-source ingestion, backup/DR

## V2 Roadmap — What to Build

Full spec in `docs/V2-ROADMAP.md`. Implementation plan:

### Layer 1 — Ingest-time enhancements (free, automatic)

1. **Memory classification** (Tulving 1972)
   - Auto-classify as semantic/episodic/procedural based on content signals
   - Add `mem_class` column to schema
   - Boost semantic memories in reranking (+0.06 already exists for idea/synthesis/insight/decision, extend to mem_class)

2. **Depth scoring** (Craik & Lockhart 1972)
   - Compute numeric depth_score 0.0–1.0 at ingest time
   - Store in metadata JSONB
   - Use as reranking factor (deeper = higher rank)

3. **Project tagging** (Tulving & Thomson 1973)
   - Add `project` column to schema
   - Set from workspace path during chat ingestion
   - Apply cross-project penalty (0.15) in reranking

4. **Automatic relationship discovery**
   - After storing new memory, search for top-3 similar existing memories
   - Create `related_to` relationships automatically
   - Also find temporal neighbors (±24 hours) and relate them

5. **Spaced retrieval** (Roediger & Karpicke 2006)
   - Add `last_accessed_at` column
   - Update reranking: `reinforcement = 0.03 * log(1 + access_count) * spacing_bonus`
   - `spacing_bonus = min(1.0, days_since_last_access / 7)`

### Layer 2 — Weekly synthesis (Kiro headless, controlled cost)

6. **Consolidation pipeline** (Stickgold 2005, Walker 2009)
   - Weekly job clusters the week's new episodic memories by topic similarity
   - Generates consolidation templates with source excerpts
   - Runs through Kiro headless to synthesize principles/patterns
   - Writes semantic memories with "Questions this answers:" sections
   - Links back to source episodes via relationships
   - Produces a weekly digest for the user

### Schema Changes Needed
```sql
ALTER TABLE memories ADD COLUMN mem_class TEXT;  -- semantic, episodic, procedural
ALTER TABLE memories ADD COLUMN project TEXT;
ALTER TABLE memories ADD COLUMN last_accessed_at TIMESTAMPTZ;
```

### Files to Create/Modify
- `src/db.py` — add mem_class/project to create_memory, update reranking formula
- `src/classify.py` — new module for memory classification
- `src/ingest.py` — add classification, depth scoring, project tagging, relationship discovery
- `src/mcp_server.py` — update memory_search with project scoping, update reranking
- `scripts/weekly_synthesis.py` — new consolidation pipeline script
- `scheduling/com.second-brain.synthesis.plist` — weekly Sunday job
- `migrations/002_v2_columns.sql` — schema changes

## Key Design Decisions (from docs/DESIGN-DECISIONS.md)
- PostgreSQL + pgvector over separate vector DB (one database for everything)
- Hybrid chat extraction (deterministic Phase 1 + LLM Phase 2)
- One chat per Kiro session (isolation, predictable cost)
- Kiro headless for Phase 2 (reuses auth, model selection, MCP)
- Crawlee outputs markdown files, not database writes (separation of concerns)
- JSONB metadata over dedicated columns (schema flexibility)
- Hybrid retrieval over pure vector (dual-process memory theory)
- Retrieval reinforcement (testing effect)
- Depth enforcement (levels of processing)

## User Preferences
- Consistency across all documents is critical
- Single discovery mechanism for all content types
- Never lose data — failed files go to staging/failed/
- Python for all implementation
- Test end-to-end before writing dependent code
- Docker Desktop is licensed and running
- Crawlee web pages are for Kiro knowledge base, NOT for Second Brain
- YouTube transcripts and chat histories ARE for Second Brain
