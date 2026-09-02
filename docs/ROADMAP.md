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

## 2. Codex operations — active-task capture enabled; raw-source vector fill active

The one-task production canary proved capture, semantic processing, retry behavior, Exact
Provenance, and local embeddings. The hourly production job now scans all active Codex Tasks and
captures only User-Owned Tasks that have been inactive for six hours. Delegated and
Unknown-Ownership Tasks remain excluded and reported. Archived history and unrestricted backfill
remain disabled pending a separate explicit decision.

The canary for task `01a014fd-89cf-73c0-a4ef-001e0f89d231` first retained a null Semantic
Processing Cursor after Titan returned `NoCredentialsError`. Local Ollama BGE-M3 then retried that
exact tail successfully, created one provenance-complete topic segment, and advanced the cursor
without task recapture or a duplicate. The canary was replaced by
`com.second-brain.codex-capture` after acceptance. Every preserved derived memory now has a local
vector. A low-priority hourly LaunchAgent is gradually filling the remaining raw-source rows.

## 3. Codex acceptance review — local path accepted; Titan retirement deferred

Real canary evidence accepted local BGE-M3 for new ingestion and retrieval. HNSW recall passed; a
small relevance inspection exposed two genuine misses that remain evaluation inputs. Titan stays
preserved until the raw-source fill completes and those real queries are rerun. See
[Local Embedding and Codex Canary Evidence](LOCAL-EMBEDDING-CANARY-2026-08-29.md).

## 4. Claude Code second adapter — next proof after local fill review

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
