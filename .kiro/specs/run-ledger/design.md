# Discovery authorization and truthful completion: design

The orchestrator coordinates allocation, skim, Explorer, Thinker, grounding and four-seat voting through focused agents/stages. Consensus decides the vote; storage independently authorizes operations against locked evidence.

Candidate transactions retain earlier successes when later work fails. Repositories persist counts/provenance, run_result preserves abort progress and the pure digest renderer presents recorded outcomes. The run advisory lock excludes overlapping orchestration.

Code authority: [src/dream_cycle/orchestrator.py](../../../src/dream_cycle/orchestrator.py), [src/dream_cycle/storage.py](../../../src/dream_cycle/storage.py), [src/dream_cycle/consensus.py](../../../src/dream_cycle/consensus.py), [src/dream_cycle/repository/runs.py](../../../src/dream_cycle/repository/runs.py), [src/dream_cycle/run_result.py](../../../src/dream_cycle/run_result.py), [src/models.py](../../../src/models.py).

Regression authority: [tests/test_discovery_integrity.py](../../../tests/test_discovery_integrity.py), [tests/test_dream_cycle.py](../../../tests/test_dream_cycle.py), [tests/test_dream_cycle_db.py](../../../tests/test_dream_cycle_db.py), [tests/test_budget_order.py](../../../tests/test_budget_order.py), [tests/test_verdict_validation.py](../../../tests/test_verdict_validation.py). Test presence is not a passing result.
