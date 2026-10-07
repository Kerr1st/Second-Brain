# Mac Mini Migration — moving the Second Brain to the always-on host

> **Status: PLAN.** Date: 2026-06-15. Owns the **migration** of the Second Brain from this laptop to
> the Mac Mini (`KerrClaws-Mini`, `kerrclaw@192.168.1.224`).
> The **execution-seam refactor** that lets the dream cycle run without Kiro lives in
> `docs/MODEL-BACKENDS.md` and is a **prerequisite** for the Mini's cycle — it is *not* re-specified
> here. This doc was split out of MODEL-BACKENDS.md on 2026-06-15 to keep the refactor and the
> migration as separate workstreams.

## Scope boundary (this doc vs `docs/MODEL-BACKENDS.md`)

- **This doc (migration):** DB move + continuity, capture-source locality, interactive MCP access,
  the ~9 LaunchAgents, backups + secrets, the Mini environment, cutover + rollback.
- **MODEL-BACKENDS.md (refactor):** *how* the dream cycle's LLM calls run on a non-Kiro backend
  (Claude Code on Bedrock). The migration **consumes** it; it does not define it.

## Decision — complete migration, single host, DB preserved

- The Mini becomes the **sole host** of the brain's **data + scheduled cycle + backups**; the laptop
  is retired *from running the cycle*.
- The memory DB is **preserved (continuity), not forked** — Postgres data moves to the Mini via
  dump/restore (per `docs/MIGRATION-native-postgres.md` discipline). One brain whose data follows the
  host.
- Single active host ⇒ **no concurrent-writer / split-brain risk.** The only care is the **cutover
  window** — never have both machines' launchd noon jobs enabled against the same DB at once.
- **Why the refactor is a prerequisite:** the Mini has **no Kiro/Amazon Q**, so even a "same data,
  same model" move crosses the **Kiro → Claude-Code-on-Bedrock** execution seam (different
  system-prompt delivery, sampling, tool-loop). The move is a backend change regardless of model —
  see `docs/MODEL-BACKENDS.md`.

## OPEN — the capture-locality reconciliation (needs your decision)

"Complete migration" collides with the fact that **capture sources live on the laptop** —
`~/.quickwork` (Quick Desktop SQLite), `~/.kiro` (chats), `~/Work/Tools/Crawlee`, Chrome/YouTube —
produced by your interactive work there. The dream cycle is downstream of all of it. Options:

- **A — Split (recommended).** Mini = DB + cycle + backups; the laptop keeps the capture/ingest jobs,
  repointed `DB_HOST → Mini`. Each job stays where its inputs are. "Migration" = the brain's data +
  synthesis + delivery move; capture stays at the source. (Literally "retired from running the
  *cycle*.")
- **B — Full migration.** Sync the laptop sources to the Mini (rsync over Tailscale; the live SQLite
  DBs need a WAL-safe copy), run *all* jobs on the Mini. Cleaner "sole host," more moving parts.
- Either way, the **interactive MCP path** (laptop agents → Mini DB over Tailscale/LAN) belongs to
  this doc.

## Mini environment (from the 2026-06-13 probe)

- Apple M4, **16 GB shared with OpenClaw**, 67 GB free; Python 3.14.5; `rclone` (`gdrive-backup:`);
  `tailscale` (installed, not up); bare git repo `kerrclaw@…:repos/second-brain.git`.
- **Install (missing today):** `postgresql@17` + `pgvector`, `gnupg`, `awscli`.
- **Path rewrites:** runtime files hardcode `/Users/kerrfir` → `/Users/kerrclaw` (the 9 plists,
  `job_wrapper.sh`, `dream_cycle_scheduled.sh`, `mcp_serve.sh`).
- **Secrets to move out-of-band:** `.backup-key`, the rclone `gdrive:` remote, Express Gmail creds.
- **AWS creds required regardless of agent backend** (embeddings = Bedrock Titan). The robust
  unattended path is a dedicated **Bedrock-invoke-only IAM principal** + Claude-Code-on-Bedrock (see
  MODEL-BACKENDS "Mini notes").

## OPEN — recommendations pending your review

> You asked to review these separately — captured here, **not yet decided.**

- **Migration acceptance = the parity gate.** Define the tolerance for "Kiro-Opus ≈
  ClaudeCode-Bedrock-Opus" on the replay harness, and the **fallback if the Explorer port doesn't
  reach it** (e.g. keep the Explorer on Kiro/laptop, Mini runs the rest).
- **Scheduled-context shadow run.** Before cutover, run the Mini's cycle under launchd in shadow for
  a few days and compare — offline replay won't catch context bugs (the cwd-outage lesson).
- **Rollback discipline.** Laptop stays authoritative until Mini parity is confirmed; keep the laptop
  DB as rollback; flip the noon job in one direction only.
- **Migrate the rest as a tracked checklist:** qd-sync, chat-extract, ingest, youtube, bookmarks,
  verify, liveness, weekly-digest, backup — each with its source/cred dependency.

## Cross-links

- `docs/MODEL-BACKENDS.md` — the execution-seam refactor (prerequisite).
- `docs/MIGRATION-native-postgres.md` — DB dump/restore discipline.
- `docs/DISASTER-RECOVERY.md` — backup topology, recovery, encryption key.
- `docs/OPERATIONS.md` — the LaunchAgent inventory + monitoring checklist.
