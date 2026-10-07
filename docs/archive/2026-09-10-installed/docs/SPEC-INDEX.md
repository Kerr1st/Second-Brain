# Specification index — Original implementation

Reviewed September 10, 2026 against local source. This is the current interpretation of historical implementation specifications; use [START-HERE](START-HERE.md) for the complete system.

## Precedence and scope

The `.kiro/specs/` requirements, designs and tasks preserve useful decisions and regression intent. They are not current runbooks. Their historical counts, present tense, proposed APIs and checked tasks can predate both code revisions. Every tracked specification now links here. Consult source for actual behavior, the current component docs for contracts, and a dated machine inventory for operational state.

The review reconciles all 36 tracked specification documents across 12 families with the corresponding current source entrypoints and active documentation. It does not re-prove every historical mathematical claim or repeat all historical tests.

| Specification | Purpose | Current implementation and limits |
|---|---|---|
| [byzantine-consensus-panel](../.kiro/specs/byzantine-consensus-panel/requirements.md) | Four reviewer roles and binary voting | [`src/dream_cycle/consensus.py`](../src/dream_cycle/consensus.py): Original uses at least three ACCEPT votes out of four. Repaired code additionally vetoes material objections. The historical BFT analogy is not a proof of independent reviewers or factual correctness. |
| [capture-api](../.kiro/specs/capture-api/requirements.md) | HTTP capture adapters | [`src/capture_api.py`](../src/capture_api.py): Endpoint code exists in both versions. Original code includes service templates; experimental scheduling deprecates this route. Consult a dated installation inventory for actual activation; endpoint availability does not establish successful capture or client coverage. |
| [claude-code-stream-json-probe-fix](../.kiro/specs/claude-code-stream-json-probe-fix/bugfix.md) | Claude Code output and tool-reachability parsing | [`src/backends/claude_code.py`](../src/backends/claude_code.py): Stream JSON handling and startup probes exist. Historical CLI build results do not establish present authentication, configured tool isolation, or end-to-end usefulness. |
| [data-layer-decomposition](../.kiro/specs/data-layer-decomposition/requirements.md) | Storage/search separation | [`src/search.py`](../src/search.py): Search and reranking are separate from db.py. Historical function sizes, test counts, old import paths and structural-only promises describe that refactor, not all subsequent changes. |
| [db-layer-hardening](../.kiro/specs/db-layer-hardening/requirements.md) | Connection pooling and SQL construction | [`src/db.py`](../src/db.py): Pooled connections and composable search queries exist. Per-function commits do not make a complete synthesis/application transaction atomic. |
| [dream-cycle](../.kiro/specs/dream-cycle/requirements.md) | Explorer, Thinker, review, storage and feedback | [`src/dream_cycle/orchestrator.py`](../src/dream_cycle/orchestrator.py): The three-evaluator/DEFERRED design is superseded. Current flow uses four reviewers; original and repaired grounding, rejection and delivery behavior differ. Completion alone does not prove useful discoveries. |
| [evaluation-framework](../.kiro/specs/evaluation-framework/requirements.md) | Retrieval diagnostics and reports | [`scripts/eval/run_evaluation.py`](../scripts/eval/run_evaluation.py): Diagnostic code exists. Generated golden questions, placeholder curated targets and metric names need the current validation caveats. No complete source-to-user-benefit result is established by task checkboxes. |
| [model-backend-adapters](../.kiro/specs/model-backend-adapters/requirements.md) | Kiro, Claude Code and Codex invocation | [`src/backends/resolver.py`](../src/backends/resolver.py): These three adapters are registered. Default laptop roles use Kiro; mini roles use Claude Code/Bedrock. The profile does not route legacy distillation, weekly digest or the default Express editor. |
| [project-auto-tagging](../.kiro/specs/project-auto-tagging/requirements.md) | Project normalization and capture context | [`src/project.py`](../src/project.py): Original legacy paths infer project labels from native workspace data. Newer canonical policy separates workspace provenance from semantic project identity; legacy paths still require care. Directory names alone are not outcome or semantic-project evidence. |
| [question-aware-search](../.kiro/specs/question-aware-search/requirements.md) | Weighted full-text indexing | [`migrations/005_question_weighted_search.sql`](../migrations/005_question_weighted_search.sql): Weighted PostgreSQL tsvector and ts_rank are implemented. Historical BM25 wording does not identify the actual scoring implementation. Generated questions within indexed content can leak answers into a benchmark. |
| [rerank-weights-single-source-of-truth](../.kiro/specs/rerank-weights-single-source-of-truth/requirements.md) | Shared reranking constants | [`src/rerank_weights.py`](../src/rerank_weights.py): Shared constants exist. Formula agreement is regression evidence, not proof that rankings help real tasks. Historical Bayesian wording does not establish posterior belief updates. |
| [retrieval-quality](../.kiro/specs/retrieval-quality/requirements.md) | Classification, depth, ranking and relationships | [`src/search.py`](../src/search.py): Heuristic classification, scoring and access reinforcement exist. Scores and exposure counts do not measure human novelty or benefit; newer retrieval/context paths extend this older design. |

## Unfinished local work

The untracked `direct-api-backends` specification and untracked `src/backends/direct_api.py` / `bedrock.py` remain preserved. Their registration/live verification is incomplete: the active resolver does not register a direct Bedrock adapter. A docstring claiming registration is ahead of implementation. This work must be reconciled before any update or deployment; it was not completed by this documentation pass.

## Terminology corrections

- **Bayesian confidence** in the integration history describes Quick Desktop memory state. The importer copies scalar confidence information; it does not implement the upstream alpha/beta update loop.
- **Byzantine/BFT** is the historical voting analogy. Role separation and a three-of-four threshold do not demonstrate calibrated correctness, reviewer independence, or learning.
- **Full-text search** here is PostgreSQL weighted `tsvector` / `ts_rank`, combined with vector results and reciprocal-rank fusion. Historical BM25 labels are imprecise.
- **End-to-end** must name its boundaries. A mocked orchestration test, source capture replay, model-call probe and demonstrated work improvement establish different things.
