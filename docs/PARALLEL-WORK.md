# Parallel Work — Worktrees & Lanes

> **Spawned agent? Read this first.** You are (or should be) working in a **git
> worktree** assigned to **one lane**. Stay in your lane's files, use your lane's
> branch and test database, and integrate by merging to `main`. Do **not** edit
> another active lane's files or run the live system.
>
> Last updated: 2026-06-16.

## Why this exists

Multiple agents editing the **same working directory** step on each other: one
agent's half-finished edits, branch switches, and new files show up in another's
`git status` and test runs. (Real example, 2026-06-16: a parallel session's edit
to `src/backends/base.py` moved another session's test count 683→685 mid-task.)

The fix is **physical separation** — each agent gets its own folder. Two ideas,
both needed:

- **Worktrees = WHERE (mechanism).** `git worktree` gives each agent its own
  directory + branch, backed by the same `.git`. One agent's uncommitted work is
  invisible to the others until merged.
- **Lanes = WHAT (policy).** Each agent owns a non-overlapping slice of the repo,
  so when branches merge there are no conflicts.

> Lane discipline **in a single shared folder does not work** — that's the setup
> that already failed us. Separation must mean separate folders (worktrees, or
> separate clones).

The **canonical checkout** `/Users/kerrfir/second-brain` on `main` is the
integration target **and** the only place the live system runs (dream cycle,
ingest, MCP server, launchd jobs, backups). It is never an agent's scratch space.

## Active lanes & assignments

The current roster — each agent works **only** in its own worktree, on its branch, in its
lane's files. **Update this table whenever a lane is staffed or retired.**

| Lane | Worktree (under `/Users/kerrfir/second-brain-worktrees/`) | Branch | Current task | Key guardrail |
|---|---|---|---|---|
| **backends** | `claude-code-adapter` | `feat/claude-code-adapter` | Build the ClaudeCodeInvoker (see `MODEL-BACKENDS.md`) | New file + `base`/`resolver` registration — **do not touch `orchestrator.py`** |
| **docs** | `docs` | `feat/docs` | User guide + keep docs consistent with code | Docs only — never `src/`, `tests/`, `config/` |
| **dream-cycle** *(create when staffing)* | `dream-cycle` | `feat/dream-cycle` | Pipeline logic: `src/dream_cycle/`, `src/prompts/` | Sole owner of `orchestrator.py`; pure lane (no DB) |

These three are mutually **file-disjoint** (as long as `backends` stays out of `orchestrator.py`),
so all three can run concurrently and merge into `main` with **zero conflicts**. Do **not** staff
a second DB-heavy lane (`retrieval` / `ingestion` / `express+mcp`) next to another without giving
each a unique `TEST_DB_NAME` — they share Postgres and `db.py`.

Create a worktree when staffing a lane:
```bash
git worktree add /Users/kerrfir/second-brain-worktrees/<lane> -b feat/<lane>
```

## The lanes

| Lane | Owns (paths) | Tests hit Postgres? | Parallel-safe | Notes |
|---|---|---|---|---|
| **backends** | `src/backends/`, `src/agent_invoker.py`, `config/backends.toml` | **No (pure)** | ✅ freely | The ClaudeCode-adapter work. Unit tests mock the subprocess. |
| **dream-cycle** | `src/dream_cycle/`, `src/dream_cycle_db.py`, `src/prompts/` | **No (DB mocked)** | ✅ freely | Owns `orchestrator.py` (the pipeline). |
| **docs** | `docs/`, `.kiro/specs/`, `README.md` | **No** | ✅ freely | Zero shared resources. Safest lane. (`docs/user-guide/` lives here.) |
| **retrieval** | `src/search.py`, `src/depth.py`, `src/classify.py`, `src/embeddings.py` | **Yes** | ⚠️ isolate DB | `embeddings.py` is also imported by ingestion (shared-read). |
| **ingestion** | `src/ingest.py`, `src/parsers/`, `src/capture/`, `src/capture_api.py`, `scripts/migrate/`, ingest scripts | **Yes** | ⚠️ isolate DB | Reads external read-only SQLite (Quick Desktop, Kiro). |
| **express/mcp** | `src/express.py`, `src/mcp_server.py`, `scripts/brief.py`, `scripts/express_push.py`, `scripts/weekly_digest.py` | **Yes** | ⚠️ isolate DB | Sends Gmail; reads dream-cycle output. |
| **ops/infra** | `scripts/jobs/`, `scheduling/`, `migrations/`, `src/qd_profile.py` | schema + live | 🚫 serialize | Migrations + launchd touch the live system. Highest coordination. |

**Three lanes are test-pure** (no Postgres): **backends, dream-cycle, docs**. Run
these in parallel freely — they have no database contention at all. The other
lanes hit the shared Postgres and need DB isolation (below) if two run at once.

## Hot files — single-owner / serialize

Worktrees turn these from silent live races into reviewable merge conflicts, but
they're still cross-lane pressure points. Only one active lane should edit each at
a time:

- `src/dream_cycle/orchestrator.py` — **dream-cycle** owns it; **backends** also
  touches the invoker wiring. Coordinate before a second lane edits it. *(This is
  the file that collided on 2026-06-16.)*
- `src/db.py` — foundational; imported by retrieval/ingestion/express/dream-cycle.
  Schema/API changes ripple everywhere → **serialize**.
- `tests/conftest.py` — the shared test harness.
- `migrations/` — a new/edited migration changes every worktree's test DB and the
  live DB → **serialize**.
- `src/prompts/`, `config/backends.toml` — coordinate if two lanes need them.

## Shared resources worktrees do NOT isolate

| Risk | Resource | Mitigation |
|---|---|---|
| **HIGH** | Postgres test DB name `memory_bank_test` is hardcoded in `conftest.py` | Make it env-overridable (one-liner below) + unique `TEST_DB_NAME` per worktree |
| **HIGH** | Postgres connection ceiling (`max_connections≈100` vs per-process `DB_POOL_MAX=20`) | Bound `DB_POOL_MAX` per worktree, or raise the server limit |
| **HIGH** | `migrations/` — schema applies to every test DB + live | Serialize migration work |
| **MED** | Live `memory_bank` DB (MCP server, dream cycle, launchd) | Never run the live system from a worktree; or point `DB_NAME` at a scratch DB |
| **MED** | LLM metering — kiro-cli→Amazon Q + Bedrock embeddings share one account | Parallel LLM-heavy runs = parallel spend + throttling; be deliberate |
| **MED** | `~/.kiro/` (agents `<uuid>.json`, sessions, `kiro-cli/data.sqlite3`) | Shared Kiro state; uuid-named writes → low collision |
| **LOW** | Read-only external SQLite (Quick Desktop `~/.quickwork`, Kiro IDE/CLI) | Opened `mode=ro`; concurrent reads safe |
| **LOW** | Fixed `/tmp` paths (`second-brain-verify`, `redistill_capped.lock`, `sb_verify_err`) | Launchd-only or deliberate cross-run mutex |
| **LOW** | `capture_api` port 8100 (deprecated), Gmail SMTP, rclone GDrive, `.backup-key` | Launchd-only / deprecated / stateless |
| **LOW** | Git refs/remotes; `.venv` (not in a fresh worktree) | Branch-per-lane; reuse the canonical `.venv` |

## Rules (the short version)

1. **Pure lanes parallelize freely:** backends ‖ dream-cycle ‖ docs.
2. **DB-heavy lanes in parallel** → each worktree sets a unique `TEST_DB_NAME`; watch the connection ceiling.
3. **Serialize:** `migrations/`, `src/db.py`, `tests/conftest.py`, and any two-lane edit of `orchestrator.py`.
4. **Live system stays on canonical `main`** — never run dream cycle / ingest / MCP / backups from a worktree.
5. **Cost:** parallel LLM-heavy runs burn Amazon Q / Bedrock in parallel.

## Worktree setup

> **Per-worktree test DBs are enabled.** `tests/conftest.py` reads `TEST_DB_NAME`
> from the environment (default `memory_bank_test`), so each worktree can point at
> its own database. DB-heavy lanes running in parallel **must** set a unique value
> (see below); pure lanes (backends/dream-cycle/docs) need nothing.

Worktrees live **outside** the repo (never inside it):

```bash
# From the canonical repo. Create a worktree on a new branch for a work item:
git worktree add /Users/kerrfir/second-brain-worktrees/<name> -b feat/<name>

# List / remove:
git worktree list
git worktree remove /Users/kerrfir/second-brain-worktrees/<name>
```

Per-worktree environment (export in the agent's shell, or a local `.env`):

```bash
# Only needed for DB-heavy lanes (after the conftest one-liner above):
export TEST_DB_NAME=memory_bank_test_<name>
export DB_POOL_MAX=10          # optional: keep the shared server's connections sane
```

Run tests from the worktree, reusing the canonical venv (deps come from the venv,
project code from the worktree's cwd):

```bash
cd /Users/kerrfir/second-brain-worktrees/<name>
/Users/kerrfir/second-brain/.venv/bin/python -m pytest -q
```

Integrate when green:

```bash
# in the worktree
git add <your lane's files>          # explicit paths, never `git add .`
git commit -m "..."
git push origin feat/<name>          # (push mini too if used)
# then merge feat/<name> -> main (review the diff), and the canonical checkout pulls
```

## Integration & conflict resolution

A **commit** and an **integration** are different events — this is the part people trip on:

- **Commit** = an agent saving to *its own* branch. Local, frequent, and **invisible to the
  other worktrees**. Committing on `feat/<lane>` does nothing to anyone else's folder, branch,
  or uncommitted work. **No one re-checks-out in response to someone else's commit.**
- **Integrate (merge)** = bringing a lane's branch into `main`. Done by the **integrator** (you /
  the orchestrator) in the canonical checkout, **one branch at a time**. This is the only place
  conflicts can occur.

### Merging a lane into `main` (integrator)

```bash
cd /Users/kerrfir/second-brain          # the [main] worktree
git merge feat/<lane>                    # bring in one lane's thread
# resolve conflicts if any (see below), then:
git push origin main && git push mini main
```

What happens depends on whether two branches touched the **same lines of the same file**:

- **Disjoint lanes → clean auto-merge, zero conflicts.** This is the whole point of the lane
  map: e.g. `backends` (`src/backends/`) and `docs` (`docs/`) share no file, so their merges
  never conflict — merge and push, nothing to resolve.
- **Overlapping lines → git reports a conflict** (`<<<<<<<` / `=======` / `>>>>>>>` markers) in
  the affected file. Edit it to pick or combine the two sides, then `git add <file>` and
  `git commit` to finish the merge. This only happens on a **hot file** (`orchestrator.py`,
  `db.py`, `conftest.py`, `migrations/`) or a blurred lane boundary — which is exactly why those
  are serialized.

### Catching up an in-flight worktree

After `main` advances, a still-running lane pulls the new state **when it wants it** — not via a
re-checkout, and never forced:

```bash
cd /Users/kerrfir/second-brain-worktrees/<lane>
git merge main        # or: git rebase main
```

It keeps working on its own branch meanwhile; it only needs to sync if it depends on the new
code or before its own final merge. Conflicts there resolve the same way.

### Keeping merges trivial

- **Disjoint lanes** (the table above) → most merges auto-resolve with nothing to do.
- **Serialize the hot files** — only one lane touches `orchestrator.py` / `db.py` /
  `conftest.py` / `migrations/` at a time.
- **Integrate in small, frequent merges**, not week-long branches — small diffs mean small or no
  conflicts.
- This file (`PARALLEL-WORK.md`) is owned by the **integrator**, not the docs-content lane, so it
  isn't a cross-lane conflict point.

## Checklist for a spawned agent

Before you start work, confirm:

- [ ] I know my **lane** and the exact paths it owns (table above).
- [ ] My cwd is my **worktree** (`/Users/kerrfir/second-brain-worktrees/<name>`), on my branch — not the canonical `main` checkout.
- [ ] (DB-heavy lanes) `TEST_DB_NAME` is set to a unique value.
- [ ] I will not edit another active lane's files, the **hot files** owned by another lane, or `migrations/` / `db.py` / `conftest.py` without coordinating.
- [ ] I will not run the **live system** (dream cycle, ingest, MCP server, backups).
- [ ] I integrate by committing to my branch and merging to `main` — I never commit on someone else's behalf.

## For the spawning (orchestrator) agent

When you spawn parallel agents: assign each a **lane + worktree + branch** from the
table, prefer the **pure lanes** for true parallelism, keep **DB-heavy lanes**
serialized (or DB-isolated), and route all `migrations` / `db.py` / `orchestrator.py`
changes through a single agent at a time.
