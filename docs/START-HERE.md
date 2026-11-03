# Start here: Second Brain

Reviewed against the R7 head of the release line (see [RELEASES.md](RELEASES.md) for heads and status). This is an implementation guide, not a claim about a running installation. The pre-refactor orientation of 2026-09-10 and the refactor branch's own version of this file are preserved in [the installed-snapshot archive](archive/2026-09-10-installed/README-ARCHIVE.md); what they got right about evidence boundaries is kept below.

## Purpose

Capture context across the user's work surfaces so the system can discover useful connections that ordinary recall or search would miss. A useful discovery names a current problem, cites evidence from another project or source, explains what transfers and what needs adapting, and identifies a concrete next step. Storing more records, generic reminders and agreement between reviewer seats do not establish that purpose.

The intended loop is capture → atomic ingestion → distillation where appropriate → retrieval → discovery → grounding audit → panel vote → deterministic authorization and storage → delivery → user feedback → improved work. Imported pre-distilled records (Quick Desktop) enter after the distillation stage. Capture, distillation and discovery are different operations; a parser stores conversation content, a distiller extracts durable claims from one conversation, the dream cycle combines memories into candidate connections.

## Identify the code and the installation

| Item | Fact at the R7 head |
|---|---|
| Lineage | The original PostgreSQL implementation, repaired in place. The earlier experimental GitHub implementation has a separate history and is not a feature flag in this tree. |
| Installed checkout | The `second-brain` working tree on the user's Mac; its launchd jobs execute that directory, so replacing its source is deployment even without restarting a service. Which release is installed is recorded in [RELEASES.md](RELEASES.md), not inferred from this guide. |
| Release line | `main` moves only by fast-forward to a reviewed `release/*` head; each runbook hard-gates on the installed `HEAD` and the migration ledger. |
| Adapters | `kiro`, `claude_code`, `codex` (agentic CLIs over MCP) and `bedrock` (tool-less Converse). Corpus roles never resolve to Kiro or a `fable` model; `controlled` is the default profile. |
| Store and transport | PostgreSQL 17 + pgvector; MCP over stdio; CLI entry points; HTTP capture API behind a bearer token. |
| Database roles (R7) | `memory_bank_owner` owns the schema and runs migrations; `memory_bank` holds DML only once the operator has run the separate demotion step. |
| Embeddings | Bedrock Titan Text Embeddings V2, 1024 dimensions, independent of the reasoning profile. |

Before operational work record the host, checkout, `git rev-parse HEAD`, `git status --short`, the schema ledger (`SELECT max(version) FROM schema_migrations`), the selected profile, the loaded launchd labels and the last `logs/jobs/*.json` status. Never infer current health from this guide. Keep **implemented**, **configured**, **installed**, **loaded**, **last attempted**, **last successful output**, **tested** and **useful to the user** as separate statuses; a plist or an old green log does not satisfy the later ones.

## Read by component

| Contract | Canonical guide | Code authority |
|---|---|---|
| Pipeline, storage, retrieval, discovery, delivery | [Architecture](ARCHITECTURE.md) | `src/db/`, `src/search.py`, `src/retrieval.py`, `src/dream_cycle/`, `src/express.py` |
| Sources, identity, refresh and exclusions | [Capture](CAPTURE-COMPONENTS.md) | `src/capture/`, `src/parsers/`, `src/ingest.py` |
| Roles, provider boundaries and spend | [Models](MODEL-BACKENDS.md) | `config/backends.toml`, `src/backends/` |
| Schedules, job runner, liveness, roles, release sequence | [Operations](OPERATIONS.md), [scheduling/README.md](../scheduling/README.md) | `scheduling/`, `scripts/jobs/`, `src/operations/`, `migrations/migrate.sh` |
| Encrypted bundles, restore, key custody | [Recovery](DISASTER-RECOVERY.md) | `scripts/maintenance/backup_bundle.py`, `config/backup.toml` |
| Interfaces and error contracts | [Interfaces](INTERFACES.md) | `src/mcp_server.py`, `src/mcp_readonly.py`, `src/validation.py` |
| Skim stage | [Skim](SKIM-STAGE.md) | `src/skim/`, `src/dream_cycle/stages/skim_stage.py` |
| Tests, effectiveness and owned hazards | [Validation](VALIDATION.md) | `scripts/test_isolated.py`, `tests/`, `scripts/eval/` |
| Requirements and research history | [Spec index](SPEC-INDEX.md) | active specifications terminate on named source files and tests |
| Design rationale | [Design decisions](DESIGN-DECISIONS.md) | — |
| Release history and remaining work | [Releases](RELEASES.md), [Refactor plan](PLAN-refactor.md) | — |

[The generated inventory](generated/runtime-inventory.md) counts adapters, profiles, roles, migrations and schedule templates directly from source. Regenerate it with `.venv/bin/python scripts/docs/inventory.py`; `tests/test_repo_hygiene.py` fails when it drifts. Counts are not repeated across guides.

## Three distinctions that prevent confusion

1. **Capture is not distillation, and distillation is not discovery.** They have different inputs, costs and failure modes, and each is monitored separately by liveness.
2. **Reasoning models are not embedding models.** Routing the agents to Claude Code or Bedrock does not remove the Titan embedding dependency; ingestion, search and `memory_create` send text to Bedrock even when no discovery model runs.
3. **A panel vote is a heuristic, not a proof.** Four seats return exact ACCEPT/REJECT verdicts and three accepts are required; the controlled profile puts two model families on the panel so no insight is accepted without cross-family agreement. Correlated model judgments carry no distributed-systems guarantee and no Bayesian update; Quick Desktop's imported confidence values are upstream snapshots, not a learning loop here.

## After compaction

1. Read this file, then [RELEASES.md](RELEASES.md), then the component guide for the stage being changed.
2. Inspect current Git and installed state before any operational change (see "Identify" above). Preserve dirty work; never `stash pop` the Sep-10 snapshot over the current docs.
3. Use the isolated test runner only. A database name ending in `_test` is not isolation; the runner's own roles and socket are.
4. For a private evaluation, freeze exact source versions, task, providers, destinations, spend threshold and omitted stages first. Earlier authorization for 35 records does not authorize a new corpus transfer.
5. Record human judgments as missing until supplied. Model acceptance rates, access counts and notification delivery are not usefulness.
6. Update the one guide owning the changed contract and regenerate counted facts. Archived documents retain history, not authority.

## Evidence boundary

The 35-record controlled comparison of September 2026 used frozen, already-stored records and real multi-seat synthesis; it substituted frozen evidence for live retrieval and did not exercise raw capture, distillation, persisted delivery or demonstrated human benefit. It is not an end-to-end success result. The isolated suite proves component contracts on synthetic data; the rehearsals in the private release notes prove migrations and scripts against restored production dumps; neither proves useful discoveries. The protocol for that is in [VALIDATION.md](VALIDATION.md).
