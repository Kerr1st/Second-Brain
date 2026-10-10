# Generated runtime inventory

Source-derived configuration, not installed or healthy service state. Regenerate with `.venv/bin/python scripts/docs/inventory.py`; `tests/test_repo_hygiene.py` fails on drift.

Adapters: 4 (bedrock, claude_code, codex, kiro). Profiles: 4 (bedrock, controlled, laptop, mini). Roles per profile: 10 (advocate, codex_semantic, distill, editor, epistemologist, explorer, methodologist, skeptic, skim, thinker). Schedule templates: 17.
Default profile: `controlled`. Migrations: 18 files, high-water `018_retire_knowledge_graph.sql`.
Scheduled dream-cycle spend threshold: $9; library default: $5. Both stop after a metered response and can overshoot.

| Schedule | Calendar (launchd local time) / interval | Program (via job_wrapper.sh) | Deadline s | Network preflight | KeepAlive |
|---|---|---|---|---|---|
| `com.second-brain.backup` | {"Hour": 2, "Minute": 0} | `scripts/jobs/backup.sh backup` | 7200 | yes | false |
| `com.second-brain.bookmarks` | {"Hour": 1, "Minute": 0, "Weekday": 6} | `scripts/scrape_bookmarks.py bookmarks` | 10800 | yes | false |
| `com.second-brain.capture-api` | on demand / RunAtLoad | `scripts/jobs/capture_api.sh capture-api` | service | no | true |
| `com.second-brain.capture-browser` | {"Hour": 1, "Minute": 15} | `scripts/jobs/capture_browser.sh capture-browser` | 3600 | no | false |
| `com.second-brain.capture-outlook` | {"Hour": 0, "Minute": 45} | `scripts/jobs/capture_outlook.sh capture-outlook` | 3600 | yes | false |
| `com.second-brain.capture-slack` | {"Hour": 1, "Minute": 0} | `scripts/jobs/capture_slack.sh capture-slack` | 3600 | yes | false |
| `com.second-brain.chat-extract` | {"Hour": 2, "Minute": 30} | `scripts/jobs/capture_chats.sh chat-capture` | 3600 | no | false |
| `com.second-brain.codex-capture` | 21600 seconds | `scripts/jobs/codex_capture.sh codex-capture` | 7200 | yes | false |
| `com.second-brain.credentials` | {"Hour": 7, "Minute": 0} | `scripts/jobs/verify_credentials.sh credentials` | 3600 | yes | false |
| `com.second-brain.distill` | {"Hour": 5, "Minute": 0} | `scripts/distill_sessions.py distill` | 7200 | yes | false |
| `com.second-brain.dream-cycle` | [{"Hour": 12, "Minute": 30, "Weekday": 1}, {"Hour": 12, "Minute": 30, "Weekday": 3}, {"Hour": 12, "Minute": 30, "Weekday": 5}] | `scripts/jobs/dream_cycle_scheduled.sh dream-cycle` | 21600 | yes | false |
| `com.second-brain.ingest` | {"Hour": 3, "Minute": 0} | `scripts/crawlee_ingest.py crawlee-ingest` | 3600 | yes | false |
| `com.second-brain.liveness` | {"Hour": 9, "Minute": 0} | `scripts/jobs/verify_liveness.sh liveness` | 3600 | no | false |
| `com.second-brain.qd-sync` | 3600 seconds | `scripts/jobs/qd_sync.sh qd-sync` | 3600 | no | false |
| `com.second-brain.verify` | {"Hour": 3, "Minute": 0, "Weekday": 0} | `scripts/jobs/verify_backup.sh verify` | 7200 | yes | false |
| `com.second-brain.weekly-digest` | {"Hour": 6, "Minute": 0, "Weekday": 1} | `scripts/jobs/weekly_digest.sh weekly-digest` | 7200 | yes | false |
| `com.second-brain.youtube` | {"Hour": 1, "Minute": 30} | `scripts/jobs/youtube_scrape.sh youtube` | 3600 | yes | false |
