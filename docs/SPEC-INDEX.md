# Specification index

Twelve active families, each four short files (`requirements.md`, `design.md`, `tasks.md`, `tasks.meta.json`) that name the source and test files that are authoritative for the contract. Code and tests decide implementation; [RELEASES.md](RELEASES.md) decides qualification and deployment. A checked task is a statement about source, never about the installed system. No active specification depends on an operational inventory that does not exist in this repository; counted facts come from [the generated inventory](generated/runtime-inventory.md).

| Family | Contract | Release that carries it |
|---|---|---|
| [routing-enforcement](../.kiro/specs/routing-enforcement/requirements.md) | Corpus routing: controlled default, Kiro/Fable denial, served-model check | R1 (deployed) |
| [direct-api-backends](../.kiro/specs/direct-api-backends/requirements.md) | Provider request and accounting: Converse, usage, billed failures, reservations | R1 (deployed) |
| [db-layer-hardening](../.kiro/specs/db-layer-hardening/requirements.md) | Database lifecycle, SQL ownership, migrations, runtime roles | R3, R7 |
| [relationship-lifecycle](../.kiro/specs/relationship-lifecycle/requirements.md) | Relationship provenance and expiry | R1 (expiry rule), R3 (016 backfill) |
| [run-ledger](../.kiro/specs/run-ledger/requirements.md) | Discovery authorization and truthful completion | R3 |
| [corpus-dedup](../.kiro/specs/corpus-dedup/requirements.md) | Source identity and resumable repair | R3 (planners), R6 (application, applied 2026-10-09) |
| [project-auto-tagging](../.kiro/specs/project-auto-tagging/requirements.md) | Project attribution | R3, R4 |
| [capture-framework](../.kiro/specs/capture-framework/requirements.md) | Capture channels and vocabulary | R4 |
| [retrieval-regression-gate](../.kiro/specs/retrieval-regression-gate/requirements.md) | Retrieval regression and ranking ownership | R3 |
| [observability](../.kiro/specs/observability/requirements.md) | Scheduled operation evidence | R5 |
| [data-locality-policy](../.kiro/specs/data-locality-policy/requirements.md) | Private source boundaries | R1, R5 |
| [evaluation-framework](../.kiro/specs/evaluation-framework/requirements.md) | Effectiveness evaluation | not ported; see the family's status |

## Historical families

The original families are preserved under [the dated archive](archive/2026-09-26/specs/) exactly as they were committed before the refactor. The dream-cycle, panel (its archived directory name still carries the old fault-tolerance analogy), model-adapter, data-layer-decomposition, capture-API and retrieval-quality specs had drifted from the code and are superseded by the families above. The question-aware-search and rerank-weight records keep research and implementation history but are not current authority. The Claude stream-JSON probe record is historical evidence; current transport behaviour lives in `src/backends/claude_code.py` and [MODEL-BACKENDS.md](MODEL-BACKENDS.md). The 2026-09-10 versions of `db-layer-hardening`, `evaluation-framework` and `project-auto-tagging` (one-paragraph redirect stubs) and the 2026-09-10 `direct-api-backends` spec (the pre-implementation design of the Bedrock adapter, 190-line task plan with execution history) are in [the installed-snapshot archive](archive/2026-09-10-installed/README-ARCHIVE.md).

## Terminology

- **Panel / seat / consensus.** The panel is the four reviewer seats (skeptic, advocate, epistemologist, methodologist); consensus is the three-of-four rule in `src/dream_cycle/consensus.py`. The grounding audit is a separate graded call made through the skeptic seat's backend before the vote (`GROUNDING_AUDIT_ROLE`), and its record is excluded from the voting projection. Older documents borrowed a distributed-systems fault-tolerance term for the vote; that analogy is retired — correlated model judgments do not inherit such guarantees.
- **Channel ≠ arrival ≠ slice.** A channel is a capture source; an arrival is one parent document new in the run's window; a slice is an Explorer-selected group handed to a Thinker.
- **Bayesian confidence** in the Quick Desktop import history describes upstream state; the importer copies scalar confidence, it does not implement an update loop.
- **Full-text search** is PostgreSQL weighted `tsvector`/`ts_rank` fused with vector results by reciprocal rank; "BM25" in historical documents is imprecise.
- **End-to-end** must name its boundaries: a mocked orchestration test, a restored-dump rehearsal, a synthetic provider probe and a demonstrated work improvement establish different things.
