# Second Brain Delivery Roadmap

> **Status: canonical execution plan.** Last updated: 2026-08-29.

This roadmap sequences work through real Vertical Slices. A later integration starts only after the
preceding integration has demonstrated source evidence, durable processing, delivery, and a
recorded outcome. Real source behavior determines shared interfaces; fixtures preserve behavior
afterward but do not substitute for the proof.

## 1. Codex vertical proof — complete

Codex Desktop proved Task Ownership filtering, six-hour eligibility, monotonic Task Refresh, the
combined Task Semantic Pass, Correction Episodes, Steering Governance, bounded context delivery,
reviewed `AGENTS.md` publication, and a later-task outcome receipt.

## 2. Codex operational canary — active, semantic retry waiting

Run the production capture command hourly for one explicitly allowlisted User-Owned Codex Task.
Keep archived history and unrestricted backfill disabled. After the task becomes inactive for six
hours, inspect capture, semantic processing, retry behavior, Exact Provenance, and logs. Expansion
beyond the allowlist requires the acceptance review below.

The LaunchAgent is loaded for task `01a014fd-89cf-73c0-a4ef-001e0f89d231`. Its first production
run captured the source task and correctly retained a null Semantic Processing Cursor after the
embedding provider returned `NoCredentialsError`. The job now exits cleanly with
`waiting_for_embedding_credentials` until Bedrock credentials are restored, then retries the same
unprocessed tail automatically.

## 3. Codex acceptance review — blocked on embedding authentication

Use real canary evidence rather than a synthetic scoring harness. Fix only observed defects. Record
whether the task was captured correctly, whether derived memories were independently useful, and
whether later recall was followed, corrected, unused, or still unknown. Decide full active-task
coverage and historical backfill separately.

## 4. Claude Code second adapter

Establish native Claude Code task identity, parent/subagent evidence, timestamps, resumptions, and
visible prompt/outcome records before implementing capture. Prove one real Claude Code task through
the same complete lifecycle. The existing Claude export importer and Claude Code model backend are
not native Agent Task Capture.

## 5. Shared Agent Task Capture seam

Compare the proven Codex and Claude adapters. Extract only behavior that demonstrably varies behind
one small interface. Keep native discovery, source locking, ownership evidence, and parsing in each
Adapter; share orchestration, behavioral obligations, reporting, and durable processing only where
both implementations support the same contract.

## 6. Remaining agent integrations

Apply the same proof gate individually to Kiro, Quick Desktop, Amazon Q Developer, Amazon Quick,
and later sources. A connector without accessible native evidence remains `not_ready`; it does not
gain a speculative Adapter or an activation claim.

## 7. Evidence-supported adaptation

Add automatic context injection, additional steering publishers, skills, hooks, tests, lint, or CI
only when explicit context packs and outcome receipts show the adaptation would improve behavior.
Every publisher retains preview, explicit approval, versioning, rollback, and least privilege.

## Activation rules

- No unrestricted historical backfill without a separate explicit decision.
- Unknown Task Ownership is skipped, never promoted to User-Owned by assumption.
- A successful dry run is not an operational proof.
- A model invocation backend is not an Agent Task Capture Adapter.
- Real source absence is a recorded readiness condition, not a reason to create synthetic success.
- The physical repository reorganization remains last; proven interfaces come first.

## Related

- [Project Charter](PROJECT-CHARTER.md)
- [Codex Task Capture build record](CODEX-TASK-CAPTURE-BUILD-PLAN.md)
- [Code-module roadmap](COMPONENTIZATION-PLAN.md)
- [Architecture Module Index](components/index.md)
