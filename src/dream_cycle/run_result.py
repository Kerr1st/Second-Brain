"""Run Result responsibilities."""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from src.models import DreamCycleResult
from src.dream_cycle.budget import *
logger = logging.getLogger(__name__)

def _aborted_result(
    self,
    run_id: str,
    run_type: str,
    started_at: datetime,
) -> DreamCycleResult:
    """Build an aborted result preserving work already produced/persisted."""
    return DreamCycleResult(
        run_id=run_id,
        run_type=run_type,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc),
        candidates_generated=self._run_stats["candidates_generated"],
        candidates_accepted=self._run_stats["candidates_accepted"],
        candidates_rejected=self._run_stats["candidates_rejected"],
        digest_path=None,
        aborted_early=True,
        cost_usd=self._spend.total_usd,
        spend_unknown=bool(self._spend.blocked_reason),
        candidates_unjudged=max(0, self._run_stats["candidates_generated"] - self._run_stats["candidates_accepted"] - self._run_stats["candidates_rejected"] - self._run_stats.get("candidates_storage_failed", 0) - self._run_stats.get("candidates_withdrawn", 0) - self._run_stats.get("candidates_blocked_unjudged", 0)),
    )
