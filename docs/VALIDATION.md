# Validation and effectiveness

A passing test proves its stated contract. It does not prove capture completeness, provider availability, safe deployment or improved work. [RELEASES.md](RELEASES.md) records which release reached which boundary (built, reviewed, deployed); the private release notes hold the rehearsal evidence. The 2026-09-10 version of this document, written against the unrepaired code, is preserved in [the installed-snapshot archive](archive/2026-09-10-installed/docs/VALIDATION.md); its code-review findings were folded into the audit register and the refactor plan.

## What the isolated suite establishes

Run only `SECOND_BRAIN_PROFILE=controlled .venv/bin/python scripts/test_isolated.py -- -q`. The runner validates a disposable manifest before collecting tests, starts a socket-only PostgreSQL 17 cluster under `/private/tmp`, applies the real migrations through `migrations/migrate.sh` as `sb_test_owner`, runs every application path as the NOSUPERUSER role `sb_test` (DML only, exactly what migration 017 grants), keeps a SUPERUSER `sb_test_legacy` for the 017 bootstrap drill, isolates HOME/metrics/credentials and denies outbound network to the process and its shell descendants. Direct `pytest` refuses to run; `tests/_isolation.py` refuses any other role. Details: [tests/README.md](../tests/README.md).

| Test family | Contract checked | Still outside that evidence |
|---|---|---|
| `test_migration_016/017/018`, `test_schema_migration_runner`, `test_data_integrity` | Transactional migrations under the advisory lock; owner/app privilege split; graph retirement refuses without a matching export marker; restore of an app-role dump into a fresh database and 017 re-grant; dedup/rechunk planners and appliers on disposable data | Production restore timing; the operator demotion step; corpus repair on the live rows |
| `test_db`, `test_pool_properties`, `test_search*`, `test_retrieval_quality_gate`, `test_rerank*` | Pool bounds and clean return, scalar projections, FTS reach of unembedded parents, RRF and dedup, 36-query synthetic ranking gate (hit@3 = 1.0, MRR ≥ 0.98, Kendall ≥ 0.8) | Real-world semantic quality, recall across the private corpus |
| `test_capture_*`, `test_codex_*`, `test_ingest_*`, `test_qd_*`, `test_crawlee_parser`, `test_slack_*`, `test_outlook*` | Preflight → window → render → ingest → outcome, identity = channel + URL, atomic parent/chunk refresh, DST windows, exit codes, Retry-After budget, bounded Codex turn assembly | Complete historical coverage of each surface, live credentials, upstream format changes |
| `test_dream_cycle*`, `test_discovery_integrity`, `test_grounding`, `test_budget_order`, `test_verdict_validation`, `test_consensus` | Exact verdicts, three-of-four rule, audit kept out of voting projections, deterministic target authorization, ordered locks, one transaction per candidate, run lock, truthful abort counts | Reviewer truthfulness, reasoning quality, usefulness |
| `test_resolver`, `test_corpus_routing_guard`, `test_consumer_routing`, `test_metered_stages`, `test_cost`, `test_bedrock*`, `test_claude_code`, `test_codex`, `test_fable_denial_variants`, `test_evaluator_reservation_bound` | No corpus role on Kiro or a `fable` model, served-model check, billed failed responses, transport reservations, finite usage, input reconciliation, per-seat output budgets | Provider entitlement, live model behaviour, invoice reconciliation |
| `test_mcp_*`, `test_readonly_discovery`, `test_capture_api`, `test_express*` | Clamped arguments, vocabulary at the boundary, generic operational errors, read-only registry shape, bearer-token service, deterministic briefing | What a client actually did with the output |
| `test_job_runner`, `test_scheduling_plists`, `test_monitor_jobs`, `test_liveness_policy`, `test_backup_bundle`, `test_backup_job`, `test_job_scripts`, `test_repo_hygiene` | Lock, deadline floors, grace, service health, network preflight, plist table agreement, liveness predicates, bundle create/verify/restore into a disposable cluster, no secrets in tracked config, generated inventory and hazards table, no personal identifiers in live docs | Dark-wake behaviour of the real Mac, Drive availability, key custody |

Synthetic invokers verify orchestration and failure handling; they do not measure reasoning quality. The retrieval fixture uses independent synthetic embeddings through the actual full-text/vector/fusion path; it establishes regressions, not relevance. `scripts/eval/*` (golden/curated rank, cold/warm rerank, ablation, `recall_check.py` HNSW-versus-exact) measure index recall and ranking agreement, not usefulness; golden queries derived from memories must not be the only held-out set.

Legacy metric names remain in code and must be read accordingly: Tier 1 `cost_efficiency` estimates invocation count, not money; Tier 2 `insight_citation_rate` is an average access count; `user_rejection_rate` divides by accepted candidates without evidence the user saw them. Missing feedback is unknown, not approval.

## Measure whether Second Brain improves work

Freeze one current decision (for example Kiro and AI-DLC adoption planning) and a bounded source set from several projects. Record what is already known, the deliverable, source versions/roles/timestamps, exact providers and models, the spend threshold, the omitted stages and the ordinary-retrieval baseline. Preflight the actual installation (checkout, schema, profile, provider reachability) with synthetic content before any private content moves.

Then, in order: run real capture and ingestion for the chosen sources and assert the normalized content and chunks survive (including a mid-write failure and a re-run); run real distillation where the loop uses it and keep prompts and outputs; run native retrieval and discovery keeping every slice, candidate, audit, seat call, vote and persisted operation, recording unavailable stages as omissions rather than substitutes; deliver through the actual surface and save exactly what was shown with stable run/item/version identifiers; judge blind against the retrieval baseline; apply one useful discovery and record before/after evidence; close the loop through the feedback mechanisms the code supports and inspect what the next run retrieves, suppresses or revises.

| Dimension | Evidence |
|---|---|
| Grounding | Claims supported by exact sources; limitations and contradictions retained |
| Cross-project transfer | A prior outcome or mechanism applied to a different current problem with the required adaptation |
| Novelty | The user confirms the connection was not already known |
| Timeliness / usefulness | It changes the current decision or next action |
| Uptake | The exact suggestion is accepted, adapted, rejected or deferred with a reason |
| Outcome | Before/after evidence of reduced work, avoided rework, better readiness or decision quality |
| Cost | Retrieval, embeddings, every reasoning attempt including failures and reservations, human review time |

Keep novelty, uptake and outcome null until a human supplies them. The frozen-source trial runner of the refactor branch (`scripts/eval/discovery_trial.py`, `src/evaluation/`) was **not** ported into the release line; the eight-record trial it prepared was never authorized. A complete trial has to be re-prepared against the deployed release with a fresh manifest. The 35-record comparison of September 2026 bypassed capture and distillation and is not an end-to-end result.

## Known hazards with owners

The owner is responsible for evidence and closure. "Closed by" names the release whose code closes the hazard; closure is complete only when that release is **deployed** per [RELEASES.md](RELEASES.md). `tests/test_repo_hygiene.py::TestDocumentationContracts::test_hazards_have_owners_and_closure_evidence` checks that every row has an owner and closure evidence.

| ID | Owner | Hazard | Closure evidence |
|---|---|---|---|
| H-ISOLATION | Runtime maintainer | Direct tests or migrations can reach the installed database | Isolated runner with precollection guards, socket-only cluster, role allowlist and network denial (R1); liveness tests read `DB_HOST`/`DB_PORT` (R1, deployed) |
| H-ROUTING | Model maintainer | Corpus content escapes to Kiro or a `fable` model through a profile or override | `controlled` default, resolver and `invoke_metered` route checks, served-model check, fable id spellings pinned (R1, deployed) |
| H-INTEGRITY | Data maintainer | Partial ingestion, stale-target overwrite or unauthorized candidate mutation | Atomic source revision, ordered locks, deterministic authorization before spend, one transaction per candidate, migration 016 CHECKs (R3) |
| H-MAINTENANCE | Data maintainer | Dedup or rechunk destroys provenance or spends money unreviewed | Dry-run manifests bound to the database incarnation and row hashes, reference remap, `--allow-paid-embedding`, `corpus_maintenance` ledger (R3); application is R6, pending authorization |
| H-RECOVERY | Operations maintainer | Ciphertext without a usable independent key or a restorable payload | Bundle decrypt-verified and read back from Drive, weekly full restore into a disposable cluster with a 36 h freshness check (R5); OneDrive key copy verified byte-equal locally only — cloud sync from a second device still unverified |
| H-PRIVILEGE | Runtime maintainer | Every job and MCP server runs as a database superuser | Migration 017 owner/app split, owner gate in `migrate.sh`, app-role smoke script, suite runs every application path as NOSUPERUSER (R7); the live demotion is the CONFIRM-REQUIRED step in the R7 runbook |
| H-GRAPH-DATA | Data maintainer | Dropping the knowledge-graph tables loses data that exists nowhere else | Verified encrypted export with receipt and `corpus_maintenance` marker, 018 refuses on count mismatch, tested restore path (R7); the archive is local-only until the operator copies it off-site |
| H-CREDENTIALS | Account owner | Long-lived static job key and credential residue remain usable | Residue files restricted to 0600 (2026-09-26); replacement provider, least-privilege policy and revocation receipt still open |
| H-PLAINTEXT-REMOTE | Operations maintainer | Historical plaintext knowledge sources and config remain on Google Drive | Nightly plaintext sync stopped (R1, deployed); purge is a listing-first CONFIRM-REQUIRED step after the first verified bundle (R5 runbook) |
| H-DEPLOYMENT | Release owner | Jobs consume changed source before qualification, or a copied plist keeps an old definition | Fast-forward-only release line with hard gates on `HEAD` and the ledger, quiesce before backup, symlinked plists, rollback per release (R1 deployed; R3–R7 runbooks) |
| H-DEPENDENCIES | Runtime maintainer | A clean lock does not prove live provider compatibility | `requirements.lock` installed (R1b, deployed); yt-dlp and curl transport upgraded together; live provider behaviour is qualified per release, not by the suite |
| H-EFFECTIVENESS | Product owner and user | Plausible suggestions may not improve actual work | Retrieval baseline, blind judgment and human novelty/uptake/outcome evidence — trial not yet run |

## Documentation maintenance

The canonical entry point is [START-HERE.md](START-HERE.md); component contracts live in the guide linked there; one owner per contract. A change names the code revision, the affected contract, its tests and its limits. Runtime facts need host, timestamp and evidence and belong in the private release notes, not in these guides. Counted facts are generated (`scripts/docs/inventory.py`) and checked. Completed plans, tutorials and historical probes are archived with dated authority notices; archives retain history, not authority. Specification statuses link to real implementation and tests; a checked task is not deployment evidence. Live documents carry no personal e-mail addresses or home-directory paths (`tests/test_repo_hygiene.py`).
