# Jev and Second Brain: executed investigation and decision

September 26, 2026, Pacific time. The API receipts use September 27 UTC timestamps.

## Decision

The optional Jev client and synthetic comparison are implemented, tested, versioned and executed. **Keep automatic runtime screening off.** The proposed ranking improved this synthetic comparison, but the frozen injection flagging threshold failed its quality gate, and the ranking lost two important answers. The installed Second Brain application was not changed.

Jev is a plausible assistant for evidence triage. This experiment does not justify allowing it to decide which evidence Second Brain sees. Its judgments should remain inspectable annotations with the original candidates preserved.

## What the investigation established

Second Brain captures work context, retrieves evidence, generates cross-project hypotheses, reviews grounding and usefulness, and delivers selected suggestions. Jev's typed decisions fit a narrow passage-judgment stage between retrieval and discovery. It does not replace capture, embeddings, the generative Explorer/Thinker, independent reviewers, or human usefulness feedback.

The installed checkout is at `1071a81687aa71575ee4489adc9d9025fb552f0b` and contains substantial preexisting work. The separately qualified refactor at `74ba02df87b04ca3f21cd3781949b023afb856d5` repairs ingestion, source identity, retrieval and operating controls. This experiment branches from that refactor in `/Users/kerrfir/second-brain-worktrees/jev-evidence-screening`, on `codex/jev-evidence-screening`.

Current retrieval combines lexical and vector results, deduplicates them, and applies deterministic utility reranking. Jev can judge retrieved content, but it cannot recover candidates discarded upstream. A project filter can exclude other named projects, which matters for cross-project discovery. Public memory search also reinforces access counts and returns short previews, so it is unsuitable for extracting an unbiased experiment. A future integration should use explicit read-only full-passage snapshots before preview rendering.

The implementation follows the [investigation and execution plan](JEV-EVIDENCE-SCREENING.md). No database migration, schedule change, live-memory edit or existing provider-role change was made. The installed Git HEAD and SHA-256 hashes of all 128 preexisting modified/untracked files checked before the experiment matched afterward.

## Implemented and executed

- `src/evidence_screening.py`: an explicit, metered client for relevance, direct evidence, contradiction, cross-problem adaptation and injection judgments. Disabled mode makes zero calls. Shadow mode preserves every candidate, order and baseline score. Invalid or unscored responses remain unknown.
- `scripts/eval/jev_screening.py`: offline preparation and a frozen synthetic comparison against the actual deterministic reranker, on identical candidate pools with a fixed clock. Fixture, baseline, rubric, client and evaluator hashes are checked before live execution.
- `scripts/eval/continue_jev_screening.py`: explicit recovery after reviewed billing reconciliation, validating cached request hashes and successful responses. It reuses paid judgments, preserves the failed attempt, and subtracts both earlier spend and its outstanding reservation from the remaining budget.
- Durable request-start and response receipts, model/version, hashes, cost, latency, calibration and failure records.

The dataset contains 84 query formulations across 21 scenario families, six candidate passages per formulation, and 504 passage judgments. Calibration uses seven families; holdout uses 14 families and 56 formulations. The corpus includes direct answers, contradictory premises, cross-project mechanisms, plans versus completed actions, explicit uncertainty, long-tail evidence, real instruction attacks and harmless attack quotations. All passages are synthetic. No private Second Brain record was submitted to OpenRouter.

The semantic ordering was frozen as `max(evidence, conflict, transfer, 0.5 * relevance)`, with baseline order breaking ties. Injection flags do not remove evidence. Calibration selected the 0.95 injection threshold before holdout results were assessed. No prompt, label, ranking formula or threshold was changed after seeing holdout results.

## Held-out results

| Measure | Deterministic baseline | Jev proposal |
| --- | ---: | ---: |
| Queries with a target in the top three | 38/56 (67.9%) | 54/56 (96.4%) |
| Direct evidence retained in the top three | 33/56 (58.9%) | 54/56 (96.4%) |
| Contradicting evidence retained | 4/8 (50.0%) | 8/8 (100%) |
| Transferable evidence retained | 10/16 (62.5%) | 16/16 (100%) |
| Irrelevant passages among the top three | 51/168 (30.4%) | 6/168 (3.6%) |

Irrelevant top-three passages declined by 88.2% relative to this baseline. The proposed ranking passed the prespecified retention and noise targets.

**Injection flagging failed:** at the frozen 0.95 threshold, 12/16 attack judgments were flagged (75%, below the 95% target). False positives were 0/320 overall, 0/32 among injection-category negatives, and 0/8 for harmless attack quotations. The four misses are four query formulations involving one useful passage with malicious instructions. Its raw injection probabilities were 0.93, 0.94, 0.94 and 0.93. The model expressed strong suspicion; the selected threshold did not generalize. These are four dependent judgments, not four independent attack families.

The comparison uses the real reranker implementation but **authored candidate pools and RRF inputs**, including deliberately difficult distractors. The measured gain is not a measurement of the installed system's real retrieval performance. The 56 holdout formulations represent 14 families, not 56 independent tasks. Retention is not classifier accuracy or proof of human usefulness.

## Failures that prevent automatic adoption

For “Which completed check demonstrated recovery from the backup?”, Jev ranked the actual successful-restore evidence fourth. Notes about an upload, a runbook review and a future proposal received conflict scores of 0.73–0.77, above the real restore's evidence score of 0.63. The composite score promoted apparent contradictions that did not establish the absence of a separate completed restore.

For “On which exact date will the archive service launch?”, the passage explicitly recording that no calendar date was assigned also fell to fourth. Other notes received larger conflict scores, including one about a different service. The model and composite ranking did not reliably preserve evidence that the requested fact was still undecided.

These are material failures even though aggregate direct-evidence retention passed. Lowering the injection threshold or changing the composite score after examining this holdout would require a new, independent evaluation. This experiment's failed gate is preserved.

## Cost, reliability and recovery

The served model was `typesafe/jev-1.13-20260917` for all 504 successful judgments. Returned inference receipts total **$0.019834416**, about two cents, against the original $0.10 experiment budget. The dedicated account key retains its separate $1 total limit. The earlier connectivity probe cost $0.000020916. After the account usage counter caught up, the final read-only check at 03:19:18 UTC matched both amounts exactly: total key usage $0.019855332, with $0.980144668 remaining under its cap.

There were **505 network attempts: 504 successful judgments and one transport timeout**. The initial run stopped after 377 successes when request 378 timed out after about 10.1 seconds. Two account-usage checks matched the known receipts with no additional observed charge. The reviewed continuation reused all 377 successful results and sent only the remaining 127 requests. The original failed run remains unchanged. Its $0.000819 reservation remains recorded as a conservative buffer against delayed billing.

Successful requests had a median latency of 332 ms and a 95th percentile of 450 ms. These are per-passage measurements. The client is sequential, so a 20-passage screen would add roughly 6.6 seconds at the observed median request latency, before other search work. This is an illustrative extrapolation, not a measured full-search latency.

## Validation and versioning

The full isolated Second Brain suite passed **1,793 tests** in 126.75 seconds. The added continuation subsequently passed **six isolated recovery tests**, including cache integrity, billing mismatch, corrupt receipts, original-file preservation, calibration preservation and stopping on another unknown charge. That is 1,799 distinct tests passed across the two runs. The only full-suite warning was an existing Starlette TestClient deprecation.

An independent reviewer found and verified fixes for rubric/label ambiguity, repeated underlying scenarios across splits, missing long-tail evidence, incomplete hash binding, absent pre-call receipts and hidden hard-negative error rates. The final implementation and continuation had no unresolved blocking review findings. Review also independently checked the final failures and recommended keeping activation off.

Local implementation history:

- `be13436`: optional client, failure tests and investigation plan.
- `81cc14e`: frozen synthetic fixture, evaluator and comparison tests, committed before live calls.
- `68be583`: preserved interrupted run and reviewed continuation with its tests.

The final results and this report are committed afterward on the same branch. The original installed checkout remains at its initial HEAD with the 128 checked preexisting files preserved. No push, merge or installed-runtime replacement was performed.

Rollback requires no database or service action because no runtime hook was activated. The integration commits can be reverted on `codex/jev-evidence-screening`, or the qualified base `74ba02d` can be checked out separately. The existing installed checkout needs no rollback. Git preserves code and receipts; it does not reverse completed API charges.

## Recommended next stage

Keep the existing search behavior and retain Jev as an optional annotation experiment. A new qualification should expand calibration around embedded instructions, explicit uncertainty, scoped negation and conflicting claims. Treating conflict as a separate review flag instead of an unconditional ranking boost is a hypothesis to test, not a proven fix. Use fresh holdout families after any change.

After that, a separately scoped real-evidence shadow comparison should preserve the baseline pool and use an exact source/destination manifest. It should assess blind human usefulness, novelty and evidence retention. Synthetic scores do not establish whether a proposed cross-project insight improves real work. The current failed gate does not authorize automatic filtering or adoption.

## Evidence

- [Completed machine-readable report](../evaluations/jev-screening/results/2026-09-26-complete/report.json), [504 judgment receipts](../evaluations/jev-screening/results/2026-09-26-complete/results.jsonl), and [continuation record](../evaluations/jev-screening/results/2026-09-26-complete/continuation.json).
- [Original interrupted run](../evaluations/jev-screening/results/2026-09-26/report.json), [billing review](../evaluations/jev-screening/results/2026-09-26/billing-review.json), and [offline validation record](../evaluations/jev-screening/results/2026-09-26/validation.json).
- [Final account observation](../evaluations/jev-screening/results/2026-09-26-complete/account-after.json) and [installed source preservation](../evaluations/jev-screening/results/2026-09-26-complete/source-preservation.json).
- [Frozen protocol and limitations](../evaluations/jev-screening/README.md) and [manifest](../evaluations/jev-screening/manifest.json).
