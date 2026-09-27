"""Dream Cycle database facade. SQL lives in focused, typed repositories."""
import sys
from datetime import datetime
from src.db import get_connection
from src.dream_cycle.repository import runs, feedback, briefing, metrics, attention
from src.dream_cycle.repository.runs import verdict_columns
from src.dream_cycle.golden_queries import extract_golden_queries


def create_run(run_type: str, backend_provenance: dict | None=None) -> str:
    return runs.create_run(run_type, backend_provenance, db=sys.modules[__name__])

def complete_run(run_id: str, stats: dict, digest: str, explorer_output: str | None=None, explorer_feedback_injected: str | None=None) -> None:
    return runs.complete_run(run_id, stats, digest, explorer_output, explorer_feedback_injected, db=sys.modules[__name__])

def store_candidate(run_id: str, candidate: dict, verdicts: dict, final_verdict: str, created_memory_id: str | None=None, *, conn=None) -> str:
    return runs.store_candidate(run_id, candidate, verdicts, final_verdict, created_memory_id, conn=conn, db=sys.modules[__name__])

def get_evaluator_verdicts_for_run(run_id: str) -> dict[str, dict]:
    return runs.get_evaluator_verdicts_for_run(run_id, db=sys.modules[__name__])

def was_feedback_injected(run_id: str) -> bool:
    return runs.was_feedback_injected(run_id, db=sys.modules[__name__])


def run_lock():
    return runs.run_lock(db=sys.modules[__name__])

def get_recent_rejections(n_cycles: int=3) -> list[dict]:
    return feedback.get_recent_rejections(n_cycles, db=sys.modules[__name__])

def get_user_rejections(n_cycles: int=3) -> list[dict]:
    return feedback.get_user_rejections(n_cycles, db=sys.modules[__name__])

def get_accepted_dissents(n_cycles: int=3) -> list[dict]:
    return feedback.get_accepted_dissents(n_cycles, db=sys.modules[__name__])

def mark_user_rejected(candidate_id: str, reason: str) -> None:
    return feedback.mark_user_rejected(candidate_id, reason, db=sys.modules[__name__])

def get_last_briefing_time() -> datetime | None:
    return briefing.get_last_briefing_time(db=sys.modules[__name__])

def get_memory_stats() -> dict:
    return briefing.get_memory_stats(db=sys.modules[__name__])

def get_golden_queries() -> list[dict]:
    return metrics.get_golden_queries(db=sys.modules[__name__])

def get_tier1_metrics(n_cycles: int=10) -> dict:
    return metrics.get_tier1_metrics(n_cycles, db=sys.modules[__name__])

def get_tier2_metrics(n_cycles: int=10) -> dict:
    return metrics.get_tier2_metrics(n_cycles, db=sys.modules[__name__])

def get_strategy_usage(n_cycles: int=10) -> dict[str, int]:
    return metrics.get_strategy_usage(n_cycles, db=sys.modules[__name__])

def record_slices(run_id: str, slices: list[dict]) -> None:
    return attention.record_slices(run_id, slices, db=sys.modules[__name__])

def get_previous_run_started_at(run_id: str, run_type: str | None=None):
    return attention.get_previous_run_started_at(run_id, run_type, db=sys.modules[__name__])

def get_recent_slice_memory_ids(before_run_id: str, n_runs: int=3) -> set[str]:
    return attention.get_recent_slice_memory_ids(before_run_id, n_runs, db=sys.modules[__name__])

def get_slices_for_run(run_id: str) -> list[dict]:
    return attention.get_slices_for_run(run_id, db=sys.modules[__name__])

def get_recent_parent_memories(since, limit: int=2000) -> list[dict]:
    return attention.get_recent_parent_memories(since, limit, db=sys.modules[__name__])

def should_run_briefing() -> bool:
    from src.dream_cycle.briefing import should_run_briefing as policy
    return policy(sys.modules[__name__])


def has_activity_since(since):
    return briefing.has_activity_since(since, db=sys.modules[__name__])

def expire_stale_relationships(days_threshold: int=90) -> int:
    """Compatibility facade for the canonical relationship lifecycle service."""
    from src.db.relationships import expire_stale_relationships as expire
    return expire(days_threshold)


def get_arrivals_summary(since, samples_per_source: int=5) -> list[dict]:
    from src.db import get_arrivals_summary as _impl
    return _impl(since, samples_per_source=samples_per_source)
