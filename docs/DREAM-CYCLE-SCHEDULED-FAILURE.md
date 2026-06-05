# Dream-cycle scheduled-run failure — diagnosis (2026-06-04)

## Symptom
The weekly scheduled dream-cycle (launchd `com.second-brain.dream-cycle`, Sundays 04:00)
**aborts early**: the Explorer returns 0 slices in ~24s → circuit breaker → exit 2 (Basso
failure notification). Confirmed on 2026-05-24 and 2026-05-31. A **manual** run on 2026-05-30
(same `run_type=scheduled`) was fully healthy: 4 slices, 10 candidates, 10 accepted, digest written.

## Ruled out (verified 2026-06-04)
- **Code / parsing bug — NO.** A read-only chain test (kiro-cli + second-brain MCP + DB +
  `memory_search`) returned valid results in ~10s.
- **Missing env var (AWS_PROFILE, KIRO_*, etc.) — NO.** Re-ran the chain under a launchd-like
  minimal env (`env -i HOME PATH TERM`, no AWS_PROFILE / no KIRO_* session vars) — still worked
  (2 slices). kiro-cli auth is file-based (`~/.aws`, `~/.kiro`) and survives a stripped env.
- **MCP server / DB access — NO.** The MCP server launches via `sys.executable` + absolute
  cwd; the orchestrator's own DB stats query succeeds under launchd (logged total=163940).

## Leading hypothesis — kiro-cli SSO token expiry at 04:00 (strengthened 2026-06-04)
kiro-cli authenticates to the model via a **cached SSO token** (observed live during a
re-distill run: `Loading cached SSO token for kiro-admin-session`). SSO tokens are
short-lived (hours). At 04:00 Sunday the cached token from the user's last interactive
session is almost certainly **expired**, so the Explorer's kiro-cli model call fails auth
and returns a fast degraded/empty response → 0 slices → abort. This fits every observation:
works manually (fresh token) but never scheduled (stale token); the orchestrator's own DB
queries succeed under launchd (DB needs no SSO); 24s, exit 0, no crash. Secondary possibility:
host asleep / network not up on wake. The 0-slice raw-output logging added this session will
confirm from next Sunday's log.

## Change made this session
`orchestrator.invoke_explorer` now logs the **raw agent output** (truncated) whenever it
produces 0 usable slices, so the next scheduled run's log (`logs/dream-cycle-YYYYMMDD.log`)
reveals whether the agent returned an error, an empty model response, or a deliberate `[]`.

## Recommended next steps
1. Check `logs/dream-cycle-2026-06-07.log` (next Sunday) for the new "no usable slices" raw
   line — an auth/SSO error there confirms token expiry.
2. If SSO expiry is confirmed: refresh/re-auth immediately before the scheduled run (add a
   re-auth step to `scripts/jobs/dream_cycle_scheduled.sh`), use longer-lived credentials for
   the job, or schedule the cycle for a time a session is typically active.
3. The pipeline itself is healthy — manual runs work — so supersession + consolidation remain
   functional on demand in the meantime (`.venv/bin/python scripts/dream_cycle_run.py --run-type scheduled`).

## Update 2026-06-05 — noon run also failed (different mode); reschedule not yet validated
Rescheduled to daily noon (commit 051b5af). The first noon run (06-05 12:02–12:16) **timed out**:
`Agent timed out after 600s` → Explorer crashed/timed-out → aborted (exit 2). This is a
*different* failure mode than the 4am instant-"0 slices" — the kiro-cli subprocess hung the
full 10-minute limit (likely stalling on a token refresh with no interactive user to complete it).

**Crucially, today was not a valid test:** the user was traveling at noon, so the "active at
noon → fresh token" assumption did not hold. The consistent pattern across ALL scheduled
failures remains: **runs while the user is absent fail** (mode varies: fast-empty vs hung-timeout).
The pipeline works when run manually with a live session (e.g., 05-30: 10 accepted).

### Still to do (not yet implemented)
1. Validate by running the dream-cycle manually while present (token fresh) — confirms the noon
   schedule is sound on non-travel days.
2. Add a pre-flight SSO-token check to `scripts/jobs/dream_cycle_scheduled.sh`: if invalid, skip +
   notify instead of a 10-minute hang + exit-2 false alarm. Consider lowering the 600s Explorer timeout.
3. Structural truth: an unattended launchd job cannot obtain the interactive SSO token; realistic
   options are run-when-present, guard-and-skip-when-absent, or a non-interactive auth path if one exists.

## Decision 2026-06-05 — accept error-and-retry; no pre-flight guard (chosen for simplicity)
Decided NOT to add a pre-flight token probe or any auth guard. Rationale: keep the system simple;
the dream-cycle is a daily, opportunistic synthesis job, so a missed run is benign.

Accepted behavior when the SSO token is unavailable (user absent/unattended):
- The Explorer's kiro-cli subprocess hangs up to the 600s timeout (noon case) or returns empty
  (4am case); the run aborts (exit 2); job_wrapper fires a "failed" macOS notification.
- The next daily run retries from a fresh state. No data corruption, no lock, no backlog
  (synthesis is opportunistic over recent activity, not a queue).

Net: an occasional benign failure + notification on days the user is away; runs normally on days
present (the common case at noon). No code change. This closes the scheduled-auth item.
Only minimal future tweak if ever desired: lower the 600s Explorer timeout to shorten the hang.
