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

## Leading hypothesis (unverified — could not reproduce locally)
A **transient environmental condition specific to 04:00 Sunday under launchd** — most likely
the host asleep / network not yet up when the job fires, so the Explorer's first model call
returns a fast degraded (empty) response. This matches the signature that recurs ONLY on
scheduled runs (24s, exit 0, 0 slices, no crash) and never on manual runs.

## Change made this session
`orchestrator.invoke_explorer` now logs the **raw agent output** (truncated) whenever it
produces 0 usable slices, so the next scheduled run's log (`logs/dream-cycle-YYYYMMDD.log`)
reveals whether the agent returned an error, an empty model response, or a deliberate `[]`.

## Recommended next steps (need the user / next data point)
1. Check `logs/dream-cycle-2026-06-07.log` (next Sunday) for the new "no usable slices" raw line.
2. If it confirms sleep/network: reschedule off 04:00 to a waking hour, or add a network-wait /
   short Explorer retry-with-backoff.
3. The pipeline itself is healthy — manual runs work — so supersession + consolidation remain
   functional on demand in the meantime (`.venv/bin/python scripts/dream_cycle_run.py --run-type scheduled`).
