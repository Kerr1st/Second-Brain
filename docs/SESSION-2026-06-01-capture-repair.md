# Session Record — Capture Pipeline Review & Repair (2026-06-01)

Status snapshot for context compaction / continuation. Scope: the Second Brain
**capture channels** (the collection/ingestion layer) and supporting infra.

## Outcome: capture layer fully reviewed, repaired, verified, committed, backed up

All capture channels are working and producing fresh data (verified landing in the
DB the same day). 553 tests pass. Work committed as `7db3a6c` and pushed to both
remotes (`mini` = Mac Mini, `origin` = GitHub `Kerr1st/Second-Brain`).

### Channel status
| Channel | Status |
|---|---|
| Quick Desktop (memories, KG, chats, feed, slack) | Fixed (profile-path repoint) + backfilled; capturing |
| Web / bookmarks (Crawlee) | Fixed (venv runner + Playwright install + wired `crawlee_ingest`); 0 → 2,200 article rows |
| YouTube | Fixed (pivoted blocked watch-history → Liked videos `list=LL`); verified end-to-end |
| CLI chat | Parser reads new kiro-cli `history` format; skips automated sub-agent convs |
| Capture API | DB pool self-heals after Postgres/container restarts |
| IDE chat / QD one-time ingests | Dead/static sources — expected stale, not broken |

## What was fixed (root causes)
- **Quick Desktop**: Quick migrated to per-profile storage (`~/.quickwork/profiles/<profile>/`);
  the 5 sync scripts pointed at the old frozen top-level paths. Added `src/qd_profile.py`
  (active-profile resolver) and repointed all scripts. Chunked the KG edge query (SQLite
  variable limit). Adapted slack import to the new `slack_builtin_cache` schema.
- **Capture API**: long-lived `SimpleConnectionPool` handed out dead connections after the
  DB restarted. `src/db.get_connection()` now probes `SELECT 1` and recycles dead conns.
- **CLI chat**: kiro-cli now stores turns in structured `history` (not `> `-prefixed
  `transcript`); parser updated, and automated dream-cycle sub-agent convs are excluded.
- **Web/bookmarks**: `job_wrapper.sh` ran `.py` jobs under system python3 (no `browser_cookie3`);
  now runs them under the venv. Playwright browsers were missing (installed chromium).
  `crawlee_ingest.py` was never scheduled — wired into `ingest_staged.sh` (daily).
- **YouTube**: watch-history playlist (`list=HL`) permanently blocked by YouTube; pivoted to
  Liked videos (`list=LL`) and fixed a `--print url` → `--print webpage_url` bug.
- **Infra**: `docker-compose.yml` now `restart: unless-stopped` so the DB auto-recovers.

## Backups — handled durably (S3 intentionally de-scoped)
- **Durable copies**: local + Google Drive (data, byte-verified current) + git on two remotes
  (source code: `mini` + GitHub). This is sufficient and reliable.
- **S3 was dropped on purpose**: the nightly S3 leg depends on AWS SSO that expires by the
  2 AM job and can't refresh non-interactively — too brittle to maintain. Not an outstanding
  item; do not re-raise. Backups are covered by the above.

## Outstanding (optional, low priority)
1. **Doc bug**: `docs/ARCHITECTURE.md` claims migration 007 adds a `schema_type` column — it
   only adds indexes; no code uses `schema_type`. Quick correction.
2. **~12K null-embedding memories** (mostly the dead `kiro_ide_chat` source) are invisible to
   vector search. Low value to backfill since the source is dead. No backfill script exists.
3. **`dream-cycle` shows a stale `exit 2`** in `launchctl` (the May 24 Explorer 0-slice abort).
   Cosmetic; clears on the next successful run. Dream cycle itself is verified working.
4. **Roadmap (not bugs)**: entity extraction on ingest; dream-cycle ↔ knowledge-graph
   integration (Explorer graph traversal). See `docs/ENHANCEMENT-PLAN.md`.

## Files changed this session
`src/db.py`, `src/parsers/cli_chat.py`, `src/qd_profile.py` (new),
`scripts/migrate/migrate_quick_desktop.py`, `scripts/jobs/job_wrapper.sh`,
`scripts/jobs/ingest_staged.sh`, `scripts/jobs/youtube_scrape.sh`, `docker-compose.yml`.
(Note: prior uncommitted QD-integration work was also included in commit `7db3a6c`.)

## Suggested next areas (not yet reviewed)
- Search / retrieval layer (`src/search.py` hybrid + rerank)
- Dream-cycle synthesis pipeline (`src/dream_cycle/`)
