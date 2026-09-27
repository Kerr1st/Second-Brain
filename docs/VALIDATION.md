# Validation and effectiveness

A successful test proves its stated contract. It does not automatically prove capture completeness, provider availability, safe deployment or improved work. [The execution ledger](REFACTOR-COMPLETION-2026-09-26.md) records the exact boundary reached.

## Reliability evidence

The isolated suite exercises actual disposable-database transactions/migrations alongside unit/property tests. It blocks external providers and source stores. Synthetic invokers verify orchestration/failure handling; they do not measure reasoning quality. The retrieval regression fixture uses independent synthetic query embeddings with the actual full-text/vector/fusion path, checking hit@k, result uniqueness, parent reachability and rank stability. These checks must pass before retrieval refactors.

Correctness regressions cover atomic source revisions, intra-batch identity, bounded chunks, explicit oversize rejection, relationship grace/provenance, incoming graph neighbors, finite spend usage, paid failed responses, unavailable reviewer handling, deterministic mutation authorization, transactional candidate storage, concurrent run/dedup locks and truthful job exits/status.

Run only `SECOND_BRAIN_PROFILE=controlled .venv/bin/python scripts/test_isolated.py -- -q`. The runner validates a disposable manifest before collecting tests. Actual external embeddings, inference, source credentials and cloud backup synchronization need separate qualification.

For the optional Jev component, see [the evidence-screening plan](JEV-EVIDENCE-SCREENING.md). Its offline failure/accounting tests run inside this same isolated suite. Its explicitly requested live evaluation uses only the versioned synthetic fixture through OpenRouter, outside the offline suite, and records calibration/holdout separation, baseline comparison, unknown spend and actual usage. A successful synthetic comparison is not authorization for a private source transfer or evidence of human usefulness.

## Measure whether Second Brain improves work

Freeze one current decision, such as Kiro and AI-DLC adoption planning, and a bounded source set from multiple projects. Record what is already known, the deliverable, source versions/roles/timestamps, exact providers, cost threshold, missing stages and baseline ordinary retrieval.

Compare retrieval-only evidence with discovery candidates under blind labels. Judge each material claim against its cited full source. Assess:

| Dimension | Evidence |
|---|---|
| Grounding | Claims supported by exact sources; limitations and contradictions retained |
| Cross-project transfer | A prior outcome/mechanism is applied to a different current problem with required adaptations |
| Novelty | The user confirms the connection was not already known |
| Timeliness/usefulness | It changes the current decision or next action |
| Uptake | The exact suggestion is accepted, adapted, rejected or deferred with a reason |
| Outcome | Before/after evidence of reduced work, avoided rework, better readiness or decision quality |
| Cost | Retrieval, embeddings, all reasoning attempts, human review time and failure/missing usage |

Keep novelty, uptake and outcome null until a human supplies evidence. Missing feedback is unknown. Model acceptance rates, access counts and notification delivery are not substitutes. Maintain a held-out task/source set; do not derive every test question from the system's own accepted memories.

`scripts/eval/discovery_trial.py prepare` writes a frozen record/task/provider/cost manifest without calling models. `run` requires its exact reviewed hash and verified Explorer Bedrock routing, creates a fresh socket-only database, runs shared normalized-source ingestion, ordinary retrieval, native discovery and a local briefing, then writes blind comparison packets and an evidence ledger. It explicitly omits native source acquisition and conversation distillation. `src/evaluation/trial.py:record_review` records supplied human ratings without inferring approval from silence. This is a bounded product trial, not a claim to test every capture surface.

A complete trial records raw capture and append/retry behavior, real distillation where appropriate, native retrieval, skim/Explorer/Thinker/audit/four reviewers, persisted candidate/evidence, exact delivered briefing and the next feedback-informed run. A frozen-source comparison that bypasses capture/distillation must name those omissions. The prior 35-record exercise was such a comparison, not a complete end-to-end success.

## Known hazards with owners

The owner is responsible for evidence and closure; status is qualified by the execution ledger. This table is checked by `tests/test_repo_hygiene.py`.

| ID | Owner | Hazard | Closure evidence |
|---|---|---|---|
| H-ISOLATION | Runtime maintainer | Direct tests/migrations can reach an installed database | Precollection guards, socket-only cluster, network denial and failure-injection migration checks |
| H-ROUTING | Model maintainer | Corpus escape through a CLI/model override | Global routing guard, controlled profile, no unmetered application invokes, read-only Explorer |
| H-INTEGRITY | Data maintainer | Partial ingestion or unauthorized candidate overwrite | Actual transactional rollback, stale-target and concurrent-writer regressions |
| H-MAINTENANCE | Data maintainer | Dedup/rechunk can destroy provenance or cost money | Current dry-run manifest, reference remap, price, verified restore and explicit apply receipt |
| H-RECOVERY | Operations maintainer | Ciphertext without usable independent key or restorable payload | Matching OneDrive key plus verified cloud custody and isolated full restore |
| H-CREDENTIALS | Account owner | Long-lived job key and historical residue remain usable | Verified replacement provider, least-privilege policy and identified IAM revocation receipt |
| H-DEPLOYMENT | Release owner | Jobs consume changed source before qualification | Preserved dirty tree, reviewed release manifest, restore and staged service checks |
| H-DEPENDENCIES | Runtime maintainer | A clean lock/audit does not prove live provider compatibility | Current audit plus isolated suite and bounded synthetic provider probes |
| H-EFFECTIVENESS | Product owner and user | Plausible suggestions may not improve actual work | Retrieval baseline, blinded source judgment, human novelty/uptake/outcome evidence |

## Documentation maintenance

The canonical entrypoint is START-HERE. Component contracts live in the guide linked there. Generated counted facts terminate on source and plists; the doc regression compares them. Completed plans, tutorials and historical probes are archived with dated authority notices. Specification statuses link to real implementation/tests; a checked task is not deployment evidence.
