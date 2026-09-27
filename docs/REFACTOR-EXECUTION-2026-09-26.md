# Refactor execution, September 26, 2026

## Judgment and purpose

Proceed with the correctness-first refactor, after correcting its order and data assumptions. The current implementation already includes capture expansion, arrival skimming, grounding audit/revision and a cross-family review panel. Those are substantive advances. The remaining product question is whether useful evidence from another project arrives in time to change a real decision. Neither more memories nor model acceptance answers it.

Three independent reviews covered strategy, routing/metering, and test/migration safety at committed original HEAD `1071a81687aa71575ee4489adc9d9025fb552f0b`. They agreed to establish safe verification first, then close execution gaps before another discovery trial. [PLAN-refactor.md](PLAN-refactor.md) records the revised sequence and decision boundaries.

## Scope and preservation

- Development branch: `codex/refactor-safety-routing`, based on `1071a81` in a separate worktree.
- The installed checkout `/Users/kerrfir/second-brain` remains unchanged. Its jobs target that directory, so a merge there would be deployment.
- A private workspace manifest records hashes of 128 pre-existing modified/untracked files, including documentation/specification edits and `src/backends/kiro.py`. Those changes are not included in this branch.
- The old repaired experimental checkout and frozen comparison artifacts remain historical references, not the base of this work.
- No live corpus maintenance, provider call, new private-data transfer, credential change, job resumption or GitHub publication is part of verification.

## Implemented in this branch

### Safe verification and migrations

The test runner provisions a fresh Unix-socket-only PostgreSQL cluster with synthetic databases and a non-superuser test owner. Before collection it installs exact-connection and provider guards, redirects HOME/source state/metrics to temporary paths, and denies outbound network access at the macOS process sandbox, including subprocess descendants. Direct pytest refuses without the disposable manifest. Formerly shadowed database fixtures share one route and clear test-owned tables.

The migration runner serializes concurrent runners with an advisory transaction lock and commits each SQL file with its ledger row. Errors stop processing and roll back the partial migration. Migration 009 quotes `current_database()` instead of naming the installed database. Shell job tests use injected probes and synthetic Codex state.

### Controlled execution and accounting

`controlled` is the default profile. Every configured corpus role rejects Kiro/Fable before invocation; programmatic resolvers and model overrides enforce the same policy. Distillation, Codex semantics and the briefing editor have dedicated direct Bedrock roles. The unused compatibility invoker shim is removed.

Returned usage survives malformed, truncated and explicitly refused provider responses. The shared invocation helper records it once and blocks further calls after an exhausted threshold or unknown usage. Dream Cycle, skim and consumer stages use it; manual replay/probe paths are included in the same boundary. Completed, failed and unknown-usage model attempts remain distinguishable; capture reports expose metered-call counts and an incomplete-spend flag. Aborts retain completed candidate counts and record reasoning cost plus unknown-spend status in the existing run stats JSON. A local CLI does not imply local model inference. The threshold is a stop-after-call control: a request already in flight can exceed it. Embedding charges are outside these reasoning-stage trackers; reported stage totals are not total system cost.

Express has an explicit deterministic path. Email configuration is checked before a normal push invokes the editor, without preventing CLI/MCP briefing use. Distillation shares one budget across extraction and contradiction checks. Capped redistillation has its own run budget and no contradiction pass. Both report failures and keep provider-explicit refusal quarantine scoped to the exact input/provider operation. Their dry runs make no model or embedding calls. Weekly digest dry-run remains an explicitly metered no-write preview.

### Capture, retrieval and panel input

Codex semantic work uses a dedicated role and a run-wide spend threshold. It freezes bounded batches so later arrivals cannot revive an abandoned batch; keeps previous-segment context bounded; preserves oversized source turns without sending a truncated version; separates infrastructure failures from model-contract failures; and caches validated output before embedding/storage. Successful batches advance the cursor transactionally. An abandoned batch requires review or a changed prompt/model contract; later turns are retained but do not leap over it.

Full-text search now reaches unembedded parents/sources while keeping vector-only eligibility on the vector leg. The regression suite checks reachability, filters and one result for dual-leg matches. Evaluator verdicts must be exactly ACCEPT or REJECT; malformed answers are contract failures rather than implicit rejection. Voting prompts omit the grounding-revision record while persisted candidates retain it. The existing three-of-four acceptance policy is unchanged; separating the grounding auditor from a voting role remains outstanding.

## Verification

Use the project environment's Python, from this worktree:

```sh
SECOND_BRAIN_PROFILE=controlled /Users/kerrfir/second-brain/.venv/bin/python scripts/test_isolated.py -- -q
```

Requires PostgreSQL 17 with pgvector, the project Python dependencies and macOS `sandbox-exec`. It fails if those safeguards are unavailable; there is no unguarded fallback. This runner is currently specific to macOS. It prints a private temporary evidence directory and stops the cluster on exit. These tests use synthetic/mock inference, not live model quality evaluation.

Implementation commit: `bf854cd` (`Enforce corpus routing and metered execution with isolated verification`).

- Complete isolated suite: **1,745 passed**, 67.76 seconds; one existing Starlette/httpx deprecation warning. Evidence: `/private/tmp/sbtest-m9j5ui_m`.
- Final capture/reporting, global route guard and verdict checks after naming the report field `semantic_metered_calls`: **66 passed**, 3.68 seconds. Evidence: `/private/tmp/sbtest-9dkoumau`.
- Earlier targeted checks exercised migration rollback, rerun/concurrency, refusal quarantine, accounting on failed output, raw capture after a budget stop, frozen retry batches and reuse after embedding failure. Independent reviewers identified two capture regressions during implementation; both were repaired and tested.
- `git diff --check` passed. All test clusters stopped. The installed checkout remained at `1071a81`; all 128 preserved file hashes matched their starting values.

The complete suite initially found ten stale tests tied to the old accounting interface or missing mock usage. Those were updated to verify actual accounting and the stronger voting-prompt boundary, then the full suite passed. Test totals overlap and should not be added together.

## Remaining gates before deployment and product proof

1. Finish the remaining run-integrity and reviewer-independence work: separate the grounding-auditor role, qualify durable abort behavior under database failure, and implement deterministic authorization for UPDATE/SUPERSEDE targets and a transaction spanning the memory, relationships and candidate ledger (F-146). Add an explicitly read-only Explorer tool surface (F-145). A favorable panel vote does not authorize arbitrary source or instruction changes.
2. Finish chunk bounds and transactional ingestion, then prepare provenance-preserving dedup/rechunk manifests. Never apply the proposed global parent URL uniqueness index: multiple Codex derived memories legitimately share a source URL.
3. Repair relationship expiry before enabling expired-edge filtering or backfills. Qualify Kiro IDE source freshness/format before claiming capture is restored.
4. Qualify operations separately: backups/graph retirement, roles and credentials, corpus rewrites, paid re-embedding and job schedules. Table deletion is not reversible through Git. The graph tables are still referenced by the backup script.
5. Reconcile the preserved dirty documentation with verified final behavior. Do not silently overwrite it with a fresh checkout.
6. Conduct a bounded current-decision trial, comparing retrieval with cross-project discovery and measuring grounded novelty, transferable evidence, timeliness, actual use/outcome and cost. Prepare the exact records/provider/spend manifest before new private transfers. Earlier 35-record authorization is not a blanket authorization for new data.

These are release and effectiveness gates, not claims that the entire seven-phase plan is complete. The new branch is development work until those boundaries are satisfied.
