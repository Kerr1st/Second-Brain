# Specification index

Twelve active families below replace drifted historical snapshots: four updated contracts and eight missing families. Code and tests are authoritative for implementation; the completion ledger records qualification. No active specification depends on a non-existent operational inventory.

| Family | Status | Contract |
|---|---|---|
| [db-layer-hardening](../.kiro/specs/db-layer-hardening/requirements.md) | Updated load-bearing specification | Database lifecycle and SQL ownership |
| [project-auto-tagging](../.kiro/specs/project-auto-tagging/requirements.md) | Updated load-bearing specification | Project attribution |
| [evaluation-framework](../.kiro/specs/evaluation-framework/requirements.md) | Updated load-bearing specification | Effectiveness evaluation |
| [direct-api-backends](../.kiro/specs/direct-api-backends/requirements.md) | Updated load-bearing specification | Provider request and accounting |
| [routing-enforcement](../.kiro/specs/routing-enforcement/requirements.md) | New canonical specification | Corpus routing |
| [corpus-dedup](../.kiro/specs/corpus-dedup/requirements.md) | New canonical specification | Source identity and resumable repair |
| [relationship-lifecycle](../.kiro/specs/relationship-lifecycle/requirements.md) | New canonical specification | Relationship provenance and expiry |
| [run-ledger](../.kiro/specs/run-ledger/requirements.md) | New canonical specification | Discovery authorization and truthful completion |
| [capture-framework](../.kiro/specs/capture-framework/requirements.md) | New canonical specification | Capture channels and vocabulary |
| [observability](../.kiro/specs/observability/requirements.md) | New canonical specification | Scheduled operation evidence |
| [retrieval-regression-gate](../.kiro/specs/retrieval-regression-gate/requirements.md) | New canonical specification | Retrieval regression and ranking ownership |
| [data-locality-policy](../.kiro/specs/data-locality-policy/requirements.md) | New canonical specification | Private source boundaries |

## Historical families

The original families are preserved under [the dated archive](archive/2026-09-26/specs/). Dream-cycle, old panel terminology, model-adapter, decomposition, capture-API and retrieval-quality specs had materially drifted and are superseded. The question-aware-search and rerank-weight records retain research/implementation history but are not duplicate current authority. The Claude stream-JSON probe record is historical evidence; current transport behavior lives in the adapter and Models guide.

Direct Bedrock code was already implemented before its earlier unchecked specification. The current spec describes that implementation and its newer safety repairs; unchecked historical tasks do not mean the adapter was absent.
