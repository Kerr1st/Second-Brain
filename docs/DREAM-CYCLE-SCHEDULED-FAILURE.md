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
