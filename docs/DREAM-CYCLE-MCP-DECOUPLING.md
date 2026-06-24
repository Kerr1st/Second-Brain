# Dream Cycle — MCP / launchd cwd Outage: Post-Mortem (decoupling deferred)

> **Status: RESOLVED.** Date: 2026-06-13. This file began as a full "MCP decoupling plan";
> once the root cause was confirmed as a one-line launchd `cwd` bug, the plan dissolved into
> this post-mortem. Full decoupling (**B1**) is **deferred** (criterion below); only **B2**
> (the Thinker context packet) survives, folded into `docs/FABLE5-THINKER-PLAN.md` as Phase-1b.

## Impact

The daily scheduled dream cycle produced **0 candidates Jun 8–12**, *silently* (the job exited
and appeared to run). In fact the scheduled path had been failing with **rotating** causes: a
`UnicodeDecodeError` under the old Python 3.13 venv on **Jun 6** (since covered by
`errors="replace"` in `agent_invoker`), then the cwd / `-32002` failure under the rebuilt 3.14
venv (Jun 8 05:56) on **Jun 8–12**. The oft-cited **9 → 8 accepted** Jun-6 result was a
**manual** run (cwd=repo) — which masked that the *scheduled* path was already broken.

## Root cause (confirmed)

Under launchd the job ran with **cwd = `/`** (`job_wrapper.sh` never `cd`'d to the repo).
`agent_invoker` launches the MCP server as **`python -m src.mcp_server`**, which needs the repo
on `sys.path` — i.e. cwd=repo (or `PYTHONPATH`). With cwd=`/`, `src` was not importable, so the
spawned server **died during kiro's startup handshake**:

```
Error loading server second-brain: McpError(-32002, "connection closed: initialize response")
```

With no `--require-mcp-startup`, kiro proceeded **without** the second-brain tools, so the
Explorer fell back to the generic `knowledge` tool, found nothing, wandered the filesystem, and
returned no slices → abort. The same `-32002` is stamped on the real noon runs
(`2026-06-11T19:00:30Z`, `2026-06-12T19:00:10Z`).

(The agent config *does* set `cwd: repo` for the MCP server, but kiro spawns the server with the
**job's** cwd — not that field — so under launchd it stayed `/` regardless. That is why the
job-level `WorkingDirectory` is the real lever, not the config's `cwd`.)

**Why it hid:** every interactive / `env -i` reproduction ran with cwd=repo, so it always
*worked* — cwd was the one variable not exercised until we ran the job via `launchctl` in the
true launchd context.

It was a **braid** of distinct launchd-context faults — not all cwd-related:
- **Jun 11:** got past Explorer+Thinker, then crashed in `digest.py` with `OSError:
  Read-only file system: 'logs'` — a **cwd-relative** path fault (fixed Jun 12 in
  `ea180ce` / `46b6526`; now also covered by `WorkingDirectory`).
- **Jun 7:** `exit 127 — .venv/bin/python: No such file` — a **separate, transient
  venv breakage** (no run row created), **not** cwd-related and **not** addressed by
  this fix; moot now that the venv is present.

## The fix (shipped — commit `b4c3a7e`)

- **`WorkingDirectory=/path/to/second-brain`** on the dream-cycle plist → the whole job
  (and the kiro-spawned MCP server) runs with cwd=repo. Neutralizes the **entire cwd-relative
  fragility class**, not just the MCP load.
- **Loud-not-silent hardening:** `--require-mcp-startup` for tool-using calls (kiro exits 3
  rather than running tool-less) + a bounded exit-3 retry/backoff.
- **Exit-code correctness:** `dream_cycle_run.py` exits 0 when the pipeline *ran* (any
  accept/reject mix), non-zero only on a genuine failure-to-run (`aborted_early`). Rejections no
  longer false-report failure (a long-latent bug that would have mislabeled even the Jun-6 run).
- **Per-call token/cost metrics** (`logs/llm_metrics/`) — Fable Phase 0a.
- Removed the stale Docker-Desktop auto-start from `job_wrapper.sh` (native postgres now).

**Verified:** a `launchctl kickstart` in the real scheduled context loaded MCP; the Explorer
returned 3 real slices (second-brain tools, not the wander); the Thinker produced 8 candidates →
7 accepted / 1 rejected (≈ the Jun-6 *manual* run's 9→8, and likely the first clean
*scheduled-context* run in a while); `digest.py` wrote cleanly. Full test suite: 657 passed.

## Decoupling decision: B1 deferred, B2 → Fable Phase-1b

The original aim was to remove the live-MCP dependency entirely (in-process pre-fetch). The
confirmed cwd cause collapsed that justification:

- **Robustness** is satisfied by the cwd fix + loud-fail/retry — a future MCP break is now
  *visible* (no silent multi-day outage) and likely tractable.
- That leaves only **Fable convergence**, which lives in the Thinker → needs **B2** (the Thinker
  context packet), **not B1** (Explorer decoupling).
- **B1 is high crown-jewel risk for ~zero remaining necessity** — we just watched the *agentic*
  Explorer produce the cross-project/contradiction insights, and a deterministic replacement for
  the agentic strategies (#2 cross-project, #4 Q-A bridging, #5 contradiction hunting) is unsound
  as sketched (embeddings encode similarity, not opposition; existing `contradicts` edges only
  re-surface *known* ones) and #4 genuinely needs an iterative loop.

**B1 is deferred.** Revisit criterion: only if you want the scheduled cycle *fully independent of
kiro-cli on principle* **and** a future kiro break proves the loud-fail hardening insufficient.
The defer is cheap/reversible — B2 builds the in-process provider B1 would reuse.

## Operational follow-ups

- **Defense-in-depth (optional):** add `WorkingDirectory` to the other job plists; they run fine
  today but share the same latent cwd-relative risk class.
- **Email-delivery gap:** the scheduled run composes the Express briefing but does **not** send —
  `EXPRESS_EMAIL_TO`/creds aren't set in the launchd environment. Wire them in when you want
  insights to reach the inbox (independent of the above).

## Lesson

`python -m <pkg>` in a spawned subprocess is cwd-sensitive, and launchd jobs default to cwd=`/`.
Set `WorkingDirectory` (or `cd`) for any repo-relative launchd job, and reproduce
scheduled-context bugs with `launchctl kickstart`, not an interactive shell — the interactive cwd
masks the fault.
