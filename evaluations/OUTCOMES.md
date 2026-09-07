# Layer 3: real AI outcome evaluations

This is an evidence-review protocol, separate from deterministic pytest and retrieval metrics. Start with Job Search through Codex, following the approved requirement to prove a Reference Integration vertically before generalizing.

## Freeze before running

1. Choose a real question and source cutoff. Record exact task/turn references, authoritative project documents, and hashes of the frozen inputs. Distinguish source observation dates from processing dates.
2. Write the expected current state, appropriate next action, prohibited unsupported conclusions, and what is genuinely unknown. Derive these from evidence, not from the answer being graded.
3. Assign the case to development or holdout. A case used to diagnose or tune a change is no longer an untouched holdout. Retain previous answers and failed receipts.
4. Record the baseline answer, code commit, model/profile, prompt, source corpus, context budget, and retrieval receipt. Keep the baseline and assisted runs otherwise comparable. If a baseline is unavailable, do not claim time saved or causal improvement.

## Run the actual consumer

Capture the ordinary source-to-memory path and the actual context delivered to a fresh consumer. Save the consumer's answer before scoring it. Do not inject the expected answer, hand-selected memories, role-specific exceptions, or widened ownership eligibility to make a case pass. Use the normal configuration and record any deviations as diagnostic runs.

The initial goal is useful recall and a correct next action. Sending an application, email, or referral request is not required for this evaluation. A recommendation is not evidence that the user acted on it.

## Score the outcome

Use pass / fail / unknown for each criterion and attach evidence:

| Criterion | Pass requires |
|---|---|
| Current state | Latest supported correction/status is reflected; obsolete history is identified |
| Exact provenance | Consequential claims trace to the correct source task/turn/document |
| Scope and authority | Correct Semantic Project and role; evidence is not promoted to approved guidance |
| Next action | Proposed action follows the actual state and does not repeat completed work |
| Uncertainty | Missing capture, ambiguous ownership, and unverified application/message state are acknowledged |
| Human usefulness | Independent user feedback or observed work supports reduced rework / improved decision quality |

A demonstrated failure remains a failure even if other criteria pass. Missing evidence or an unreviewed usefulness judgment remains unknown. A “followed” receipt is an observation to inspect, not an independent quality score. Record reviewer identity and whether the assessment came from an agent or a human; only explicit human approval can govern steering rules.

Report per-case outcomes and the number of evaluated cases, failures, and unknowns. Do not turn one successful repair replay into a statistical claim about the system. Capture cost/latency and human rework when available; do not fabricate measurements.

## Job Search baseline and next evaluation

The prior September 6 evidence is preserved in the ignored `evaluations/results/current-state-diagnosis/` directory and summarized in [the recall report](../docs/research/CURRENT-STATE-RECALL-2026-09-06.md).

- The corrected outreach-state case is development/repair evidence. A fresh consumer improved with the repaired pack, but independent human usefulness and hiring impact were not established.
- The completed-package handoff case failed to recover the latest state and suggested repeated work. It was an untouched holdout at that run. It is now known diagnostic evidence and must not be described as an unseen future holdout.
- Both are retained in the private dated baseline prepared for this implementation. No fresh live-model quality result is claimed by reorganizing tests.

For the next run, use the corrected-state case as a regression and select a new, unexamined job-search task as a holdout before changing the implementation. Freeze its expected state independently and run the normal source-to-consumer process. If source ownership blocks capture, report that coverage failure; do not silently enroll the task or manufacture a passing answer.

Use [OUTCOME-TEMPLATE.md](OUTCOME-TEMPLATE.md) for every case. Keep completed scorecards and private input/output artifacts under ignored `evaluations/results/<date>/`. The existing retrieval evaluation command supplements this review and now returns nonzero for failed or empty required tiers; it does not grade the semantic correctness of an answer.
