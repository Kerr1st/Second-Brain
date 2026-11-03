# Mac mini migration — plan (2026-10-10)

**Status:** plan only; nothing has been installed or moved. Supersedes the archived [2026-09-26 version](archive/2026-09-26/MAC-MINI-MIGRATION.md), whose mini probe (2026-06-13) is the only data on the mini: Apple M4, 16 GB shared with OpenClaw, ~67 GB free, Python 3.14, rclone remote `gdrive-backup:`, Tailscale installed but stopped; no postgresql@17, pgvector, gnupg or awscli. On 2026-10-10 the mini was unreachable from the MacBook (different subnet; no Tailscale on the MacBook), so none of that was re-verified.

**Why:** the MacBook sleeps with the lid closed. Dark wakes have cost scheduled runs their network: the 2026-10-09 02:00 dream cycle and backup failed on DNS. Moving synthesis and backups to an always-on host removes that class of failure. Capture can't move, because its sources live on the MacBook.

## Target architecture: split

- **Mini:** PostgreSQL 17 + pgvector (the corpus), dream-cycle, weekly-digest, backup, verify, plus a mini-side liveness and credentials sweep. Profile `controlled`, not `mini`: both exclude Kiro/Fable, but `controlled` keeps the Thinker tool-less on Bedrock and costs ~$3–6 per run.
- **MacBook:** every capture job (qd-sync, chat-extract, codex-capture, capture-browser, bookmarks, ingest, youtube, capture-slack, capture-outlook), capture-api, and the editor MCP server. They reach the mini's database through an SSH local forward `127.0.0.1:15432 → mini 127.0.0.1:5432`, kept up by a LaunchAgent with keepalive. Postgres keeps `listen_addresses = localhost`.

| Job | Needs | Where |
|---|---|---|
| dream-cycle, weekly-digest, verify | DB + Bedrock | mini |
| backup | `pg_dump`, `.backup-key`, rclone, Crawlee `knowledge/sources` | mini; Crawlee sources need a real mirror (rsync from the MacBook), not a symlink |
| qd-sync | `~/.quickwork` (Quick Desktop) | MacBook → remote DB |
| chat-extract | `~/.kiro` session stores | MacBook → remote DB |
| codex-capture | `~/.codex` | MacBook → remote DB |
| capture-browser, bookmarks, ingest | Chrome History/Bookmarks, Crawlee | MacBook → remote DB |
| youtube | yt-dlp with Chrome cookies | MacBook → remote DB |
| capture-slack, capture-outlook | slack-mcp / grasp-mcp, Midway | MacBook (corporate tooling stays on the corporate laptop) |
| capture-api | loopback, Kiro hooks | MacBook → remote DB |
| credentials, liveness | per-host checks | split: one sweep per host; results must reach the user (osascript notifications don't work headless) |

## Stages (each reversible; the MacBook stays authoritative until stage 5)

0. **By hand (user):**
   - Make the mini reachable: same LAN, or Tailscale on both machines.
   - Add the MacBook's SSH public key to `kerrclaw`'s `authorized_keys`.
   - `pmset -a sleep 0 autorestart 1` on the mini.
   - Decide how logins after a reboot work. LaunchAgents only run in a logged-in session; with FileVault, a reboot waits for an unlock.
   - **Policy check first:** moving Slack/Outlook/Quick/Kiro work data onto a personal, unmanaged machine, and whether Tailscale is allowed on the corporate laptop.
1. **Environment:**
   - On the mini, install postgresql@17, pgvector, gnupg, awscli and uv, plus Claude Code on Bedrock (Explorer only). Clone from the bare repo and build the venv from `requirements.lock`.
   - Code prerequisites, on a branch:
     - template `$HOME` in the 17 plists and `mcp_serve.sh` (today they hard-code the operator's home directory);
     - `src/db/pool.py` sends a password only when `DB_PASSWORD` is set, so `~/.pgpass` works;
     - accept `PGSSLMODE`.
   - Verify: the isolated suite passes on the mini.
2. **Credentials:**
   - Issue a second Roles Anywhere client certificate for the mini from the local CA (`docs/OPERATIONS.md` "Credentials"), with a distinct CN added to the role trust policy so either host can be revoked alone.
   - Copy `.backup-key` (0600). Point rclone at the `gdrive:` remote name or set `SECOND_BRAIN_BACKUP_REMOTE`.
   - Verify: `aws sts get-caller-identity --profile second-brain-jobs`, `rclone lsf`, credentials job.
3. **Rehearsal:**
   - Restore the newest Drive bundle on the mini ([DISASTER-RECOVERY.md](DISASTER-RECOVERY.md)), then run `migrate.sh` as the owner (re-grants 017) and set `hnsw.ef_search = 200`.
   - Switch `pg_hba` from `trust` to `scram-sha-256` for local and host, and give `memory_bank` and `memory_bank_owner` passwords held in 0600 `.pgpass`/`.env`, never in plists. Trust on the mini would let any local process (OpenClaw included) connect as any role.
   - Verify: row counts match, `smoke_app_role.py` is all ok, recall check ≥ 0.85.
   - Undo: drop the mini database.
4. **Shadow, a few days:**
   - Run `verify` and one manual dream cycle against the mini's copy.
   - Point a read-only MCP session on the MacBook through the tunnel at it.
   - Fix anything that only shows up under launchd.
5. **Cutover:**
   - Window: outside 01:30–05:00 and Mon/Wed/Fri 12:30–16:30, not near the top of the hour.
   - Quiesce all 16 MacBook labels and check `pg_stat_activity` is empty.
   - Take a fresh bundle and restore it on the mini, then `migrate.sh` and `ef_search`.
   - Bootstrap the mini jobs.
   - Bootstrap the MacBook capture jobs, capture-api and the tunnel with `DB_PORT=15432`, and restart the editor sessions.
   - `brew services stop postgresql@17` on the MacBook, keeping its data directory.
   - Verify:
     - the next backup receipt shows `remote_verified`;
     - liveness is green on both hosts;
     - the first Mon/Wed/Fri run sets `completed_at` and `cost_usd`.
6. **Settle:**
   - Move the MacBook's 00:45–03:00 capture jobs into working hours, since capture now just catches up when the laptop is online. Check that the two-day Slack/Outlook windows still cover weekends.
7. **Retire** the MacBook data directory after 2–4 clean weeks.

**Rollback:**
- Before stage 5: boot out the mini labels and bootstrap the MacBook ones.
- After stage 5: dump the mini's database and restore it on the MacBook first; otherwise anything written to the mini is lost.

## Risks

- **Policy (inferred):** corporate work data on a personal machine. Decide this before stage 1.
- **Offline capture:** the laptop is often off the home LAN. Capture jobs then exit 69 (DB unreachable) and catch up next run. Alert only on repeated misses.
- **Memory:** the mini's 16 GB is shared with OpenClaw. An HNSW rebuild needs ~2 GB of `maintenance_work_mem`.
- **Headless alerts:** a mini nobody looks at needs alerts sent somewhere, e.g. an email digest of liveness.
