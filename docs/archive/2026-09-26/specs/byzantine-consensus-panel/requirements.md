# Requirements Document: Byzantine Consensus Panel

## Introduction

The Dream Cycle's consensus panel currently uses 3 evaluators (Skeptic, User Advocate, Epistemologist) with a 3/3 unanimity threshold. The architecture is named "Four-Agent Byzantine Consensus Pipeline" and references Lamport's 1982 Byzantine Generals Problem, but the implementation does not satisfy Byzantine fault tolerance. Lamport, Shostak, and Pease (1982) proved that tolerating f faulty nodes requires 3f+1 total nodes. To tolerate 1 hallucinating evaluator (the "traitor general"), the panel needs 4 evaluators, not 3.

This feature adds a fourth evaluator — the Methodologist — and replaces the three-state consensus model (ACCEPTED/DEFERRED/REJECTED) with a binary BFT-correct model: ≥3/4 ACCEPT = ACCEPTED, ≤2/4 ACCEPT = REJECTED. The DEFERRED state is removed entirely.

### Design Rationale: Why Binary Consensus

The move from three-state to binary consensus follows from the BFT math:

- **3/4 ACCEPT (BFT quorum met)**: At least 2 loyal evaluators agreed. Even if the 4th accept is a hallucinator, a genuine majority of loyal evaluators said yes. → ACCEPTED
- **2/4 ACCEPT (ambiguous under BFT)**: In the worst case, the hallucinator is one of the 2 accepts, meaning only 1 loyal evaluator actually supports the insight. You cannot distinguish this from a genuine 2-1 split among loyal evaluators. Designing for the worst case → REJECTED
- **DEFERRED eliminated**: The old DEFERRED state (2/3 in the 3-evaluator model) represented "one dissenter — give it another shot." With 4 evaluators, the equivalent "one dissenter" case is 3/4, which now meets the BFT quorum and is ACCEPTED directly. The 2/4 case is not a "narrow miss" — it's an ambiguous result that cannot be trusted under BFT assumptions.

The Reflexion pattern (Shinn 2023) that DEFERRED enabled — feeding the dissenter's objection back to the Thinker — is valuable but requires a trustworthy trigger signal. 2/4 is not trustworthy. Instead, dissenting reasoning on accepted insights (3/4 cases) is preserved through two lightweight mechanisms: digest annotation and feedback injection. This provides the same learning loop without re-evaluation machinery.

### What Gets Removed

- `tally_consensus` DEFERRED branch
- `_is_second_deferral()` on orchestrator
- Deferred candidate handling in `invoke_thinker()`
- `get_deferred_candidates()` in dream_cycle_db (deprecate)
- `mark_deferred_twice_rejected()` in dream_cycle_db (deprecate)
- Two-strike rule logic
- "Deferred evidence gathering" Explorer strategy (#11)
- `candidates_deferred` field effectively becomes always-zero

## Glossary

- **Consensus_Panel**: The group of 4 independent evaluators (Agents 3a/3b/3c/3d) that assess each candidate insight. Binary verdict: ≥3/4 ACCEPT = ACCEPTED, ≤2/4 = REJECTED.
- **Evaluator_Verdict**: A binary ACCEPT or REJECT decision from a single evaluator, with reasoning text.
- **Skeptic**: Evaluator A — assesses factual grounding, non-obviousness, logical validity, and actionability.
- **User_Advocate**: Evaluator B — assesses relevance to current work, timing, signal-to-noise ratio, and depth.
- **Epistemologist**: Evaluator C — assesses evidence sufficiency, falsifiability, novelty, durability, and retrievability.
- **Methodologist**: Evaluator D — the new fourth evaluator. Assesses internal consistency, source independence, reasoning structure, and reproducibility of the insight's methodology.
- **Byzantine_Fault_Tolerance**: The property that the consensus protocol produces correct outcomes even when up to f of 3f+1 evaluators produce arbitrary (hallucinated) outputs. With 4 evaluators, the panel tolerates 1 faulty evaluator.
- **BFT_Quorum**: The minimum agreement threshold for safety: 2f+1 out of 3f+1 nodes. With f=1: 3 out of 4. Grounded in the property that any two quorums of size 2f+1 overlap in at least f+1 nodes, guaranteeing at least one honest node in the intersection (Castro & Liskov 1999, PBFT).
- **Tally_Consensus**: The pure function in `src/dream_cycle/consensus.py` that maps a list of 4 evaluator verdicts to ACCEPTED or REJECTED.
- **Orchestrator**: The central coordination module (`src/dream_cycle/orchestrator.py`) that manages the pipeline lifecycle, including invoking evaluators and recording verdicts.
- **Dream_Cycle_DB**: The database layer (`src/dream_cycle_db.py`) for dream-cycle-specific operations on `dream_cycle_runs` and `dream_cycle_candidates` tables.
- **Accepted_Dissent**: When a candidate is ACCEPTED with 3/4 (not unanimous), the dissenting evaluator's reasoning is preserved in the digest and feedback injection for systemic learning.

## Requirements

### Requirement 1: Fourth Evaluator Role — The Methodologist

**User Story:** As a knowledge system owner, I want a fourth evaluator that assesses the methodological rigor of candidate insights, so that the consensus panel covers a perspective orthogonal to the existing three evaluators and completes the Byzantine fault tolerance model.

#### Acceptance Criteria

1. THE Methodologist evaluator SHALL assess candidates on internal consistency: whether the insight's claims, evidence citations, and conclusions form a logically coherent argument without self-contradiction.
2. THE Methodologist evaluator SHALL assess candidates on source independence: whether the cited source memories represent genuinely independent data points rather than derivatives of the same original source.
3. THE Methodologist evaluator SHALL assess candidates on reasoning structure: whether the insight follows the depth framework (WHAT, EVIDENCE, WHY IT MATTERS) with each section substantively contributing rather than restating the others.
4. THE Methodologist evaluator SHALL assess candidates on reproducibility: whether another agent examining the same source memories would plausibly arrive at the same or a compatible conclusion.
5. WHEN evaluating UPDATE or SUPERSEDE operations, THE Methodologist SHALL verify that the proposed change follows from the cited evidence through a traceable chain of reasoning, not from unstated assumptions.
6. THE Methodologist evaluator SHALL return exactly one verdict per candidate: ACCEPT or REJECT with non-empty reasoning.

### Requirement 2: Binary BFT Consensus — ≥3/4 ACCEPTED, ≤2/4 REJECTED

**User Story:** As a knowledge system owner, I want the consensus threshold updated to a binary BFT-correct model, so that the panel satisfies the Byzantine fault tolerance quorum (2f+1 = 3 out of 3f+1 = 4 evaluators) and eliminates the DEFERRED state that conflated fault tolerance with quality gating.

#### Acceptance Criteria

1. WHEN a candidate receives 3 or more ACCEPT verdicts out of 4, THE Tally_Consensus function SHALL return ACCEPTED.
2. WHEN a candidate receives 2 or fewer ACCEPT verdicts out of 4, THE Tally_Consensus function SHALL return REJECTED.
3. THE Tally_Consensus function SHALL NOT return DEFERRED for any input.
4. THE Tally_Consensus function SHALL accept a list of exactly 4 EvaluatorVerdict objects as input.
5. THE Tally_Consensus function SHALL raise ValueError if the input list length is not exactly 4.
6. THE Tally_Consensus function SHALL remain a pure function with no side effects or external dependencies.

### Requirement 3: Orchestrator Integration — Four Evaluator Invocation

**User Story:** As a knowledge system owner, I want the orchestrator to invoke 4 evaluators per candidate instead of 3, so that every candidate is assessed by the full Byzantine-tolerant panel.

#### Acceptance Criteria

1. WHEN the Consensus Panel evaluates a candidate, THE Orchestrator SHALL invoke the Skeptic, User_Advocate, Epistemologist, and Methodologist evaluators independently.
2. THE Orchestrator SHALL apply the same crash/timeout handling to the Methodologist as to the existing three evaluators: retry on failure, then abort the run loudly if it persists — never fabricate a REJECT verdict.
3. THE Orchestrator SHALL build a verdicts dictionary containing evaluator_a through evaluator_d verdict and reasoning fields before storing the candidate record.
4. THE Consensus_Panel SHALL evaluate each candidate independently — no evaluator's prompt contains another evaluator's verdict or reasoning.

### Requirement 4: Evaluator Prompt Template — Methodologist Support

**User Story:** As a knowledge system owner, I want the prompt template system to support the Methodologist role, so that the fourth evaluator receives a role-specific prompt with the same interpolation interface as the existing three evaluators.

#### Acceptance Criteria

1. THE Prompt module SHALL include a Methodologist role description and criteria in the evaluator prompt template system alongside the existing Skeptic, User_Advocate, and Epistemologist roles.
2. WHEN `get_evaluator_prompt` is called with role "methodologist", THE Prompt module SHALL return a complete evaluator prompt containing the Methodologist's criteria, the candidate JSON, and the source memories content.
3. IF `get_evaluator_prompt` is called with an invalid role, THEN THE Prompt module SHALL raise a ValueError listing all four valid roles.

### Requirement 5: Database Schema Extension — Evaluator D Columns

**User Story:** As a knowledge system owner, I want the `dream_cycle_candidates` table to store the fourth evaluator's verdict and reasoning, so that all evaluator outputs are persisted for metrics, feedback injection, and auditability.

#### Acceptance Criteria

1. THE Dream_Cycle_DB SHALL add `evaluator_d_verdict TEXT` and `evaluator_d_reasoning TEXT` columns to the `dream_cycle_candidates` table.
2. WHEN storing a candidate record, THE `store_candidate` function SHALL accept and persist evaluator_d_verdict and evaluator_d_reasoning alongside the existing evaluator_a through evaluator_c fields.
3. WHEN querying recent rejections for feedback injection, THE `get_recent_rejections` function SHALL return evaluator_d_verdict and evaluator_d_reasoning in addition to the existing evaluator fields.
4. WHEN querying evaluator verdicts for digest generation, THE `get_evaluator_verdicts_for_run` function SHALL return the Methodologist's verdict and reasoning keyed as "methodologist" alongside the existing three evaluators.
5. THE migration SHALL be idempotent, using `ADD COLUMN IF NOT EXISTS` guards so that re-running the migration produces no errors.

### Requirement 6: DEFERRED State Removal — Orchestrator Simplification

**User Story:** As a knowledge system owner, I want the DEFERRED state and its associated machinery removed from the orchestrator, so that the consensus model is purely binary and the codebase is simplified.

#### Acceptance Criteria

1. THE Orchestrator SHALL NOT use the DEFERRED verdict for any candidate processing.
2. THE Orchestrator SHALL remove the `_is_second_deferral()` method and all two-strike rule logic.
3. THE Orchestrator SHALL remove deferred candidate handling from `invoke_thinker()` — the Thinker no longer receives deferred candidates with dissenting objections. The `deferred` parameter SHALL be removed from the `invoke_thinker()` method signature.
4. THE Orchestrator SHALL remove the call to `get_deferred_candidates()` and `get_previous_run_id()` for deferred candidate retrieval.
5. THE `DreamCycleResult` dataclass SHALL set `candidates_deferred` to 0 for all runs (field retained for schema compatibility).

### Requirement 7: Accepted Dissent — Digest Annotation

**User Story:** As a knowledge system owner, I want the digest to annotate non-unanimous accepted insights with the dissenter's role and reasoning, so that I can see which concerns were overridden by the BFT quorum.

#### Acceptance Criteria

1. WHEN an insight is ACCEPTED with 3/4 (not unanimous), THE digest SHALL display "Accepted (3/4)" and include the dissenting evaluator's role and reasoning.
2. WHEN an insight is ACCEPTED with 4/4 (unanimous), THE digest SHALL display "Accepted (4/4)" with no dissent annotation.
3. THE digest SHALL include "methodologist" in the evaluator role loop when rendering evaluator reasoning for all accepted insights.

### Requirement 8: Accepted Dissent — Feedback Injection

**User Story:** As a knowledge system owner, I want the feedback injection to include dissenting concerns from accepted insights, so that the Explorer learns from patterns of evaluator disagreement even on accepted candidates.

#### Acceptance Criteria

1. THE `build_feedback_injection()` function SHALL query accepted candidates where at least one evaluator rejected, in addition to the existing rejected candidate query.
2. THE feedback injection SHALL include a "Dissenting concerns on accepted insights" section that surfaces the dissenting evaluator's role and reasoning for non-unanimous accepted candidates.
3. THE `build_feedback_injection()` function SHALL include `"evaluator_d": "Methodologist"` in the evaluator role mapping for both rejected and accepted-dissent queries.

### Requirement 9: Cost Efficiency Metric Update

**User Story:** As a knowledge system owner, I want the cost efficiency metric to reflect 4 evaluator invocations per candidate instead of 3, so that the Tier 1 metrics remain accurate.

#### Acceptance Criteria

1. WHEN computing cost efficiency in `get_tier1_metrics`, THE Dream_Cycle_DB SHALL calculate evaluator invocations as 4 multiplied by candidates_generated, replacing the previous factor of 3.

### Requirement 10: Design Document Update — Byzantine Generals Explanation

**User Story:** As a knowledge system owner, I want the design document to accurately explain the Byzantine Generals connection with the corrected 4-evaluator binary consensus model, so that the architecture documentation matches the implementation and captures the design rationale.

#### Acceptance Criteria

1. THE design document SHALL explain that the panel uses 4 evaluators to satisfy Lamport's 3f+1 bound for tolerating f=1 faulty (hallucinating) evaluator.
2. THE design document SHALL describe the binary consensus model: ≥3/4 ACCEPT = ACCEPTED (BFT quorum met), ≤2/4 ACCEPT = REJECTED (insufficient consensus under worst-case BFT assumption).
3. THE design document SHALL explain why DEFERRED was removed: the BFT quorum (3/4) is the mathematically proven safe threshold, and requiring 4/4 for acceptance conflates fault tolerance with quality gating.
4. THE design document SHALL explain the worst-case analysis for 2/4: the hallucinator may be one of the 2 accepts, meaning only 1 loyal evaluator supports the insight.
5. THE design document SHALL reference Lamport, Shostak, and Pease 1982 ("The Byzantine Generals Problem", ACM TOPLAS) and Castro & Liskov 1999 (PBFT) as the theoretical foundations.
6. THE design document SHALL describe the Methodologist's role and how the four evaluator perspectives (factual grounding, user value, epistemic quality, methodological rigor) provide orthogonal coverage.
7. THE design document SHALL describe the accepted dissent mechanism (digest annotation + feedback injection) as the replacement for DEFERRED's Reflexion-based re-evaluation.
