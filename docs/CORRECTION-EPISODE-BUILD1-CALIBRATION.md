# Correction Episode Build 1 calibration

**Task:** `019f516e-8399-79a2-b22e-ec1ca23353f0`
**Date:** 2026-07-23
**Policy:** Real adjacent Agent Turns, manually labeled before the configured semantic backend runs.

This is a bounded review of Build 1 detection behavior, not a permanent evaluation framework or a
numerical quality gate. A positive case requires the user to reject, replace, or materially narrow
a prior visible agent outcome and to state a specific improved expectation. Questions, answers to
agent questions, new requirements, decisions made after discussion, changed circumstances, and
scope changes should abstain.

| Case | Prior turn | Current turn | Expected | Reason |
|---|---|---|---|---|
| Migration terminology question | `e74498f0-ad80-49fb-b4dc-09fb1d62e3f5` | `eec8f056-d580-46de-9836-15bb58234afb` | Abstain | The user asks whether two terms mean the same thing; the improved terminology is not yet settled. |
| Topic Segment follow-up | `6d2cc495-6b2c-4c3d-b9a5-6b4d57bb494f` | `b3dc790d-46bf-418d-9055-7ebedaa60015` | Abstain | Ordinary comprehension question. |
| Amazon products conflated | `ee442a21-8919-4145-b3df-b454fd987cd7` | `b0a21672-118f-433e-a1e7-3b206baed324` | Correction | The user identifies the conflation and states that the products are distinct. |
| Quick Desktop identity answer | `b0a21672-118f-433e-a1e7-3b206baed324` | `35d0a242-b2a4-4dee-944c-c9d3bad80f68` | Abstain | The agent explicitly asked whether the sources were separate; the user answers that question. |
| Reject historical-mutation handling | `fa389019-0c3c-4f10-a0da-442baa55360c` | `b7739d7f-4502-48dd-80e5-0b4dc6c64ec8` | Correction | The user rejects the proposed behavior and supplies the monotonic alternative. |
| Ask about current retry behavior | `28e75ff4-934e-4908-8b38-0f98e793c567` | `2ed03ad8-7a52-4f25-8be1-f570e5a4e887` | Abstain | Information-seeking question. |
| Defer source-state capability | `2ed03ad8-7a52-4f25-8be1-f570e5a4e887` | `666a78af-2947-445c-8958-b3f96c2320d2` | Abstain | A decision made after receiving the requested information, not a correction. |
| Reject four-tier test policy | `dd2a2d57-b2f2-4538-bd24-b844d571145b` | `b1360b40-aebf-4cbd-9190-d181fd7c8fdb` | Correction | The user rejects the elaborate policy and states that real data can be used with less rigid separation. |
| Reject synthetic-fixture requirement | `b1360b40-aebf-4cbd-9190-d181fd7c8fdb` | `7f59f7c9-a29f-438a-b1bd-d734940cff27` | Correction | The user rejects synthetic fixtures and specifies real data throughout, including committed raw data. |
| Reject synthetic harness | `7f59f7c9-a29f-438a-b1bd-d734940cff27` | `155048b0-8b3c-4372-9c3d-a2bf85855542` | Correction | The user says the harness is still too complex and directs the plan back toward simplification. |
| Ask whether distillation is summarization | `4bcf1f3f-e939-4542-aa0f-b28a92de800f` | `6993224e-d510-46fa-8d70-1660609dc066` | Abstain | Clarifying question. |
| Request correction capture | `447b7e21-604e-450b-b4f2-89fcaf09ca16` | `73d1d3f8-e958-432c-95f4-aa8f867687e3` | Abstain | New capability request unrelated to the prior outcome. |
| Confirm three-of-four consensus | `ab7d2803-d436-49c2-93e7-a3f5090a8594` | `97f8ced3-4762-45ef-8408-76f6bf88d48c` | Abstain | The prior outcome already said three of four; the user asks for confirmation. |
| Changed-rule circumstances | `85db460a-f03a-4e3a-803d-22b33b400f28` | `14785312-ed29-4b4f-8eb8-2d46d2dff890` | Abstain | The user agrees with the proposal and discusses later changed circumstances. |
| Expand to all agents | `aee4c832-942f-4d1b-9440-2b6f814bcf5d` | `1d84c063-0390-45f5-9f62-f4fdb04bd1a9` | Abstain | New applicability requirement, not a correction of the prior outcome. |

## Observed results

The first configured-backend pass matched 14 of 15 predeclared labels.

| Case | Initial observation | Final observation | Correction title |
|---|---|---|---|
| Migration terminology question | Abstain | Abstain | — |
| Topic Segment follow-up | Abstain | Abstain | — |
| Amazon products conflated | Correction | Correction | Amazon Quick, Kiro, and Amazon Q Developer are distinct |
| Quick Desktop identity answer | Abstain | Abstain | — |
| Reject historical-mutation handling | Correction | Correction | Ignore historical source changes while processing new turns |
| Ask about current retry behavior | Abstain | Abstain | — |
| Defer source-state capability | Abstain | Abstain | — |
| Reject four-tier test policy | Correction | Correction | Simplify real Codex history evaluation policy |
| Reject synthetic-fixture requirement | Correction | Correction | Use real Codex data throughout the pipeline |
| Reject synthetic harness | Correction | Correction | Reevaluate and simplify the capture plan |
| Ask whether distillation is summarization | Abstain | Abstain | — |
| Request correction capture | Abstain | Abstain | — |
| Confirm three-of-four consensus | **Correction** | **Abstain** | Initial false positive: Preserve the existing Dream Cycle consensus standard |
| Changed-rule circumstances | Abstain | Abstain | — |
| Expand to all agents | Abstain | Abstain | — |

The false positive was a conditional clarification: preserve three-of-four consensus if it is
already the current standard, but otherwise explain the discrepancy. The first model pass treated
that conditional request as a correction even though the prior outcome already specified three of
four. The semantic contract now explicitly requires conditional clarification requests to abstain
unless the user also unambiguously states that the prior outcome was wrong and supplies its
replacement. A bounded rerun of that same real pair abstained.

The five expected Correction Episodes all cited exactly the declared prior and current Agent Turn
IDs. Their corrected expectations stayed within the correcting prompts:

- Amazon Quick, Kiro, and Amazon Q Developer are different agent frameworks.
- Previously distilled history remains intact; historical source changes are ignored while new
  turns continue through capture and distillation.
- Real Codex data may be used without the proposed rigid four-tier separation.
- Real data may be used throughout the pipeline and committed to Git; synthetic fixtures are not
  required.
- The synthetic harness was too complex and the plan should be reevaluated and simplified.

This review is evidence that the selected boundaries behaved as intended in one configured-backend
run after one refinement. It is not a statistical accuracy claim, fixed score threshold, or
substitute for reviewing later real captures.
