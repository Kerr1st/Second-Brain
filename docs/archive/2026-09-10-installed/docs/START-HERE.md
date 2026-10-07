# Start here — original Second Brain

**Authority:** current orientation for the original implementation. Reviewed 2026-09-10 against original commit `7bf8d4112ea91c24d04fe5623066014f9d302374` and its existing local backend changes. Review establishes source behavior, not deployment or user benefit.

## Purpose and success

Capture useful context across the user's work surfaces so that the system can find connections the user might miss. A useful discovery names a current problem, cites relevant prior evidence from another project or source, explains what transfers and what must change, and proposes a concrete next step. A generic productivity suggestion, more stored records, or reviewer agreement alone does not meet that standard.

The intended loop is **capture → ingestion → distillation where appropriate → retrieval → multi-agent discovery → storage → delivery → user feedback → improved work**. Already-distilled Quick Desktop memories can enter after the distillation stage. Some paths bypass common ingestion entirely; this is an actual implementation detail, not a unified pipeline guarantee.

## Identify the implementation before acting

| Item | Original implementation |
|---|---|
| Checkout | The original `second-brain` working tree on the user's laptop; do not infer a remote installation from a profile name |
| Baseline | `7bf8d41`; record the full current hash and worktree diff before any follow-up |
| Local unfinished work at review | Modified `src/backends/agentic_cli.py`, `base.py`, `kiro.py`; untracked `bedrock.py`, `direct_api.py`, and direct-API spec directory. Preserve them |
| Registered reasoning adapters | Kiro, Claude Code, Codex. Direct Bedrock files exist locally but are not in `DEFAULT_ADAPTERS` |
| Default dream-cycle profile | `laptop`: all six roles use Kiro with configured model `claude-opus-4.8` |
| Alternative configured profile | `mini`: all six roles use Claude Code with configured Bedrock model ID. This is configuration intent, not a verified Mac mini deployment |
| Embedding path | Bedrock Titan text v2, 1024 dimensions; separate from reasoning profile |
| Store and transport | PostgreSQL + pgvector, MCP over stdio, CLI entrypoints, optional HTTP capture and Gmail delivery |
| Experimental implementation | Separate Git history, not a feature flag in this tree. Evaluate its own code/configuration and runbook separately |

Use the [spec index](SPEC-INDEX.md) to distinguish current code from historical `.kiro` requirements and plans.

Read [model backends](MODEL-BACKENDS.md) before provider changes: the profile does **not** select the distiller, weekly digest, or Express editor. Those still use the Kiro compatibility alias.

## Component map

| Component | What it does | Primary guide |
|---|---|---|
| Source acquisition | Kiro CLI/IDE files, Quick Desktop stores, browser bookmarks/Crawlee, YouTube; manual export/API/MCP paths | [Capture](CAPTURE-COMPONENTS.md) |
| Ingestion and distillation | Normalize, store raw parents/chunks, embed; separately extract durable decisions and insights | [Capture](CAPTURE-COMPONENTS.md) |
| Storage and retrieval | Memory records, typed relationships, full-text + vector search, reranking and retrieval counts | [Architecture](ARCHITECTURE.md) |
| Dream cycle | Explorer, Thinker, Skeptic, Advocate, Epistemologist, Methodologist; three-of-four review vote | [Architecture](ARCHITECTURE.md#discovery-and-learning) |
| Provider execution | CLI adapters, prompts, startup checks, authentication and metrics | [Model backends](MODEL-BACKENDS.md) |
| Express | Briefing composition/edit/render, optional email gate, delivery preferences | [Architecture](ARCHITECTURE.md#delivery-and-feedback) |
| Operations | Twelve schedule templates, logs, liveness, setup and diagnostics | [Operations](OPERATIONS.md) |
| Backup/recovery | Encrypted dump/JSON, plain source/config copies, Google Drive transport and restore checks | [Recovery](DISASTER-RECOVERY.md) |
| Evaluation | Retrieval benchmarks, mocked/component tests, full-loop validation still to perform | [Validation](VALIDATION.md) |

## Current local observation

A read-only audit of the original laptop installation at **2026-09-10 19:58 PDT** found PostgreSQL responding and the capture API reporting a DB connection. All twelve schedule templates were installed; six pipeline jobs were disabled: distill, weekly-digest, ingest, qd-sync, liveness, and dream-cycle. Kiro reported not logged in, and the AWS identity probe reported expired credentials. No model request was used to claim Bedrock availability. The migration ledger recorded only 001–005, so it must be reconciled with actual columns/tables before schema changes. These are timestamped observations, not continuing health guarantees; detailed private source counts/output timestamps are retained in the local operational inventory.

## Three distinctions that prevent confusion

1. **Capture is not distillation, and distillation is not discovery.** A parser stores conversation content; a distiller extracts durable claims from one conversation; the dream cycle combines memories into candidate connections.
2. **Reasoning models are not embedding models.** Using Codex or Kiro for agents does not remove this implementation's Bedrock embedding dependency.
3. **Voting is not Bayesian updating.** The four-reviewer quorum is a heuristic called “BFT” in older code/specs. Correlated LLM judgments do not inherit distributed-system Byzantine guarantees. Quick Desktop's documented Bayesian memory confidence is upstream; this code imports confidence snapshots and has no demonstrated end-to-end Bayesian feedback update loop.

## Resume checklist after compaction

1. Read this file, then the guide for the stage being changed. Record host, checkout, `git rev-parse HEAD`, `git status --short`, selected profile, and configuration override names without printing secrets.
2. Distinguish **code present**, **configured**, **installed**, **loaded**, **last attempted**, **last successful output**, and **user benefit**. A plist or old successful log does not satisfy the later levels.
3. Consult the current local operational inventory before changing anything. This repository intentionally does not embed private source counts or credential state as timeless facts. Preserve paused jobs and unfinished code.
4. For an evaluation, state the exact raw input set, source versions, current task, provider/model, DB/state isolation, external transfers, and omitted stages before running it.
5. Read [validation gaps](VALIDATION.md#prioritized-code-review-findings). Record unresolved findings instead of silently treating documentation or passing unit tests as a repair.
6. Update the **one canonical guide for the changed contract** and its evidence date. Old links are redirects; historical snapshots are not operating instructions. See [documentation ownership](VALIDATION.md#documentation-maintenance).

## Evidence boundary

The controlled comparison conducted during this investigation used 35 frozen, already-stored records and actual multi-agent synthesis through a common Codex transport. It generated seven candidates and surfaced two. It substituted frozen evidence for live retrieval and did not exercise raw capture, distillation, real persisted delivery, or demonstrated human benefit. It is **not an end-to-end success result** for either implementation.

No current production success, credential validity, remote deployment, restore-time target, or usefulness score is certified by this documentation pass. The full validation protocol is in [Validation](VALIDATION.md).
