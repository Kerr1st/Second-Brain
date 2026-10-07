# Effectiveness evaluation: design

Present in the release line: `scripts/eval/run_evaluation.py` and its `eval_*` siblings (golden and curated queries, cold/warm reranking, ablation, consolidation, depth), `scripts/eval/recall_check.py` (HNSW versus exact vector search) and `scripts/eval/refactor_probe.py`. They run read-only against a configured database and report ranking metrics.

Not present: the frozen-manifest trial launcher (`prepare` / `run` / `record_review`) that created a private cluster, ingested normalized records, recorded a retrieval baseline, invoked the dream cycle and wrote blinded packets. Re-implementing it means: a manifest with record hashes, task, providers, cost threshold and omitted stages; a launcher that refuses any connection other than its own disposable cluster; shuffled packets that hide the retrieval/discovery arm; a review recorder that appends 0–4/null judgments. Native acquisition, distillation, external delivery and human benefit remain separate evidence.

Code authority: [scripts/eval/run_evaluation.py](../../../scripts/eval/run_evaluation.py), [scripts/eval/recall_check.py](../../../scripts/eval/recall_check.py), [scripts/eval/golden_queries.py](../../../scripts/eval/golden_queries.py), [src/dream_cycle/orchestrator.py](../../../src/dream_cycle/orchestrator.py).

Regression authority: [tests/test_eval_properties.py](../../../tests/test_eval_properties.py), [tests/test_retrieval_quality_gate.py](../../../tests/test_retrieval_quality_gate.py). Test presence is not a passing result.
