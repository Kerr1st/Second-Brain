# Start here: Second Brain

Reviewed against the September 26 refactor branch. Read [the completion ledger](REFACTOR-COMPLETION-2026-09-26.md) for exact verification and deployment boundaries. This is an implementation guide, not a claim about a running installation.

## Purpose

Capture context across work surfaces so the system can discover useful connections that ordinary recall or search would miss. A useful discovery names a current problem, cites evidence from another project, explains what transfers and what needs adapting, and identifies a concrete next step. Storing more records, generic reminders and agreement between reviewers do not establish that purpose.

The intended loop is capture → atomic ingestion → distillation where appropriate → retrieval → discovery → evidence review → storage → delivery → user feedback → improved work. Imported pre-distilled records enter later in the loop. Capture, distillation and discovery are different operations.

## Identify the code and the installation

This branch repairs the original PostgreSQL implementation. The earlier GitHub experimental implementation has a different history and its own documentation. Do not substitute its repaired components into a comparison without recording that change. Upstream fixes are ported deliberately; neither a profile name nor a GitHub branch establishes a remote deployment.

The installed checkout was `/Users/kerrfir/second-brain` at the September 26 inspection. Scheduled jobs execute that directory, so replacing its source is deployment even without restarting a service. Its pre-existing backend/documentation/specification work remains preserved. The isolated refactor checkout, historical GitHub snapshot and frozen comparison inputs have different purposes.

Before operational work record the host, checkout, HEAD, diff, schema ledger, selected profile, loaded schedules and recent outputs. Never infer current health from this guide. Keep **implemented**, **configured**, **installed**, **observed working**, **tested**, and **useful to the user** separate.

## Read by component

| Contract | Canonical guide | Code authority |
|---|---|---|
| Pipeline, storage, retrieval, discovery, delivery | [Architecture](ARCHITECTURE.md) | `src/db/`, `src/search.py`, `src/dream_cycle/`, `src/express.py` |
| Sources, identity, refresh and exclusions | [Capture](CAPTURE-COMPONENTS.md) | `src/capture/`, `src/parsers/`, `src/ingest.py` |
| Roles, provider boundaries and spend | [Models](MODEL-BACKENDS.md) | `config/backends.toml`, `src/backends/` |
| Schedules, diagnostics and release sequence | [Operations](OPERATIONS.md) | `scheduling/`, `scripts/jobs/`, `src/operations/` |
| Encrypted backups and isolated restore | [Recovery](DISASTER-RECOVERY.md) | `scripts/maintenance/backup_bundle.py` |
| Interfaces and error contracts | [Interfaces](INTERFACES.md) | `src/mcp_server.py`, `src/mcp_readonly.py`, `src/validation.py` |
| Tests, effectiveness and owned hazards | [Validation](VALIDATION.md) | `scripts/test_isolated.py`, `tests/`, `scripts/eval/` |
| Requirements and research history | [Spec index](SPEC-INDEX.md) | Active specifications terminate on named source/tests |

[Generated inventory](generated/runtime-inventory.md) counts adapters, profiles, roles, migrations and schedule templates directly from source. Regenerate it with `python scripts/docs/inventory.py`; its regression check detects drift. Counts are not repeated across guides.

## After compaction

1. Read this file and the completion ledger, then the relevant component guide.
2. Inspect current Git and installed state before any operational change. Preserve dirty work.
3. Use the isolated test runner. A database name ending in `_test` is not isolation.
4. For private evaluation, freeze exact source versions, task, providers, destinations, spend threshold and omitted stages. Previous authorization for 35 records does not authorize a new corpus transfer.
5. Record human judgments as missing until supplied. A panel is a heuristic vote, not Bayesian updating or a proof of truth.
6. Update the one guide owning the changed contract and regenerate counted facts. Archived docs retain history, not authority.
