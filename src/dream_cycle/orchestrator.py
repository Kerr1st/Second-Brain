"""Dream Cycle Orchestrator — four-agent pipeline coordination.

Coordinates Explorer → Thinker → Consensus Panel pipeline.
Delegates to consensus, storage, digest, and feedback modules.

Requirements: 1, 3, 9 (this file — task 6.1 skeleton)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone

import psycopg2

from src.backends.base import BACKEND_CAPABILITIES
from src.backends.cost import SpendCapExceeded, SpendTracker, kill_switch_engaged, invoke_metered, cap_from_env, UsageUnavailable
from src.backends.resolver import SKIM_ROLE, DREAM_ROLES, default_resolver
from src.models import (
    CandidateInsight,
    DreamCycleResult,
    EvaluatorVerdict,
    MemorySlice,
)
from src.prompts import get_evaluator_prompt, get_explorer_prompt, get_thinker_prompt
from src.prompts.revision import REVISION_PROMPT, REVISION_SCHEMA, build_revision_request
from src.prompts.skim import PROMPT_VERSION as SKIM_PROMPT_VERSION
import src.dream_cycle_db as dream_cycle_db
from src.dream_cycle.repository.runs import verdict_columns
from src.dream_cycle.consensus import tally_consensus
from src.dream_cycle.storage import store_accepted, check_duplicate, authorize_candidate, CandidateAuthorizationError, canonical_uuid
from src.dream_cycle.digest import generate_digest
from src.dream_cycle.feedback import build_feedback_injection
from src.dream_cycle import allocation, ledger
from src.dream_cycle.packet import (
    EVALUATOR_VERDICT_SCHEMA,
    TOOLED_NOTE,
    TOOLLESS_NOTE,
    build_packet,
    format_sources_for_evaluator,
    load_memories_by_ids,
)
from src.skim.runner import SkimResult

logger = logging.getLogger(__name__)

# Stats dict for aborted/failed runs — all counts zero.
_ZERO_STATS = {
    "candidates_generated": 0,
    "candidates_accepted": 0,
    "candidates_rejected": 0,
}


class RunInterrupted(BaseException):
    """SIGTERM/SIGINT arrived mid-run: stop paid work, close the run row as aborted.

    Derives from BaseException (like KeyboardInterrupt) so the per-stage
    ``except Exception`` fallbacks (Thinker skip, grounding, skim) cannot swallow
    it; only the dedicated arm in ``_run_locked`` handles it.
    """

    def __init__(self, signum: int):
        self.signum = signum
        try:
            self.signal_name = signal.Signals(signum).name
        except ValueError:
            self.signal_name = str(signum)
        super().__init__(f"run interrupted by {self.signal_name}")


_INTERRUPT_SIGNALS = (signal.SIGTERM, signal.SIGINT)

#: Cap on a storage failure message in the log and the candidate's block_reason.
MAX_FAILURE_MESSAGE_CHARS = 300


def _failure_message(exc: BaseException) -> str:
    """``Type: message`` on one line, truncated, for a candidate that failed storage."""
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    return text if len(text) <= MAX_FAILURE_MESSAGE_CHARS else text[:MAX_FAILURE_MESSAGE_CHARS - 1] + "…"

from src.dream_cycle.budget import *
from src.dream_cycle.budget import _skim_cap_usd
from src.dream_cycle.parsing import _as_int, _as_str_list, _unwrap_nested_candidate
from src.dream_cycle.agents.runner import AgentRunner
from src.dream_cycle.agents import explorer, thinker, evaluator
from src.dream_cycle.stages import grounding, skim_stage
from src.dream_cycle import run_result


class DreamCycleOrchestrator(AgentRunner):
    """Coordinates the four-agent dream cycle pipeline."""

    def __init__(self, database=None):
        self._database = database
        self.resolver = default_resolver()
        self._capture_dir: str | None = None
        self._current_run_id: str | None = None
        self._current_started_at: datetime | None = None
        # Metered-spend guard (reset per run) and the slice contents delivered to
        # a tool-less Thinker, kept so evaluators can ground against the same text.
        self._spend = SpendTracker.from_env()
        self._run_stats = dict(_ZERO_STATS)
        self._slice_memory_cache: dict[str, dict] = {}
        self._candidate_contexts: dict[int, dict[str, dict]] = {}
        # Skim records for the run's arrivals window (Step 3.5 → Explorer inventory
        # and the weighted guaranteed slice) and what the Explorer was actually given.
        self._skim_records: list[dict] = []
        self._explorer_input: dict = {}
        # Ledger rows (origin + metrics) aligned with the slices _allocate_attention
        # returned — the judging order (Phase 4) is derived from them.
        self._slice_rows: list[dict] = []








    def run(self, run_type: str, scope: dict | None = None, capture_dir: str | None = None) -> DreamCycleResult:
        """Hold the database-wide run lock before frequency checks or paid work."""
        with dream_cycle_db.run_lock() as acquired:
            if not acquired:
                self._run_stats = dict(_ZERO_STATS)
                self._spend = SpendTracker.from_env()
                return self._aborted_result("", run_type, datetime.now(timezone.utc))
            return self._run_locked(run_type, scope, capture_dir)

    def _run_locked(self, run_type: str, scope: dict | None = None, capture_dir: str | None = None) -> DreamCycleResult:
        """Execute a full dream cycle pipeline.

        Args:
            run_type: 'scheduled', 'post_learn', 'session_start', 'user_triggered'
            scope: Optional scoping for non-scheduled runs.
            capture_dir: Optional directory path. When set, raw agent inputs
                and outputs are written as JSON fixtures for replay testing.

        Returns:
            DreamCycleResult with stats and digest path.
        """
        self._capture_dir = capture_dir
        started_at = datetime.now(timezone.utc)
        logger.info("Starting dream cycle: run_type=%s, scope=%s", run_type, scope)

        self._run_stats = dict(_ZERO_STATS)
        # Kill switch: refuse to spend anything, before any record or LLM call.
        if kill_switch_engaged():
            logger.warning("SECOND_BRAIN_KILL_SWITCH is set — dream cycle run refused")
            return self._aborted_result("", run_type, started_at)

        # Fresh per-run spend accounting and slice-content cache.
        self._spend = SpendTracker.from_env()
        self._run_stats = dict(_ZERO_STATS)
        self._slice_memory_cache = {}
        self._candidate_contexts = {}
        self._skim_records = []
        self._explorer_input = {}
        self._slice_rows = []

        # Session-start frequency cap: check before creating a run record
        if run_type == "session_start" and not dream_cycle_db.should_run_briefing():
            logger.info("Session-start briefing skipped: frequency cap not met")
            return self._aborted_result("", run_type, started_at)

        # Step 1: Create run record (with backend provenance for cross-swap audit)
        try:
            run_id = dream_cycle_db.create_run(
                run_type, backend_provenance=self._backend_provenance()
            )
        except psycopg2.OperationalError:
            logger.error("Database unreachable when creating run record")
            return self._aborted_result("", run_type, started_at)
        logger.info("Created run record: %s", run_id)

        # Top-level try/except around the entire pipeline to catch unexpected
        # errors (including DB failures mid-run). The run record should always
        # be completed, even on error paths -- including a SIGTERM from the job
        # wrapper / launchd or a Ctrl-C, which must not leave completed_at NULL.
        previous_handlers = self._install_interrupt_handlers()
        try:
            return self._run_pipeline(run_id, run_type, scope, started_at)
        except RunInterrupted as exc:
            logger.error("Dream cycle interrupted by %s (run_id=%s); closing the run as aborted with real counts",
                         exc.signal_name, run_id)
            try:
                dream_cycle_db.complete_run(run_id, stats=self._run_stats | {
                    "aborted": True, "interrupted_by": exc.signal_name,
                    "cost_usd": self._spend.total_usd, "spend_reserved_usd": self._spend.reserved_usd,
                    "spend_unknown": bool(self._spend.blocked_reason),
                }, digest="")
            except Exception:
                logger.error("Failed to complete run record after %s", exc.signal_name)
            return self._aborted_result(run_id, run_type, started_at)
        except psycopg2.OperationalError:
            logger.error("Database unreachable during pipeline execution (run_id=%s)", run_id)
            try:
                dream_cycle_db.complete_run(run_id, stats=self._run_stats | {
                    "aborted": True, "cost_usd": self._spend.total_usd, "spend_reserved_usd": self._spend.reserved_usd,
                    "spend_unknown": bool(self._spend.blocked_reason),
                }, digest="")
            except Exception:
                logger.error("Could not persist aborted run while database unavailable")
            return self._aborted_result(run_id, run_type, started_at)
        except SpendCapExceeded as exc:
            # Hard stop: no more metered calls, no fabricated verdicts. Close the
            # run record so the abort is auditable, then report aborted_early.
            logger.error("Dream cycle aborted (run_id=%s): %s", run_id, exc)
            try:
                dream_cycle_db.complete_run(run_id, stats=self._run_stats | {"aborted": True, "cost_usd": self._spend.total_usd, "spend_reserved_usd": self._spend.reserved_usd, "spend_unknown": bool(self._spend.blocked_reason)}, digest="")
            except Exception:
                logger.error("Failed to complete run record after spend-cap abort")
            return self._aborted_result(run_id, run_type, started_at)
        except Exception:
            logger.exception(
                "Unexpected error during pipeline execution (run_id=%s)", run_id
            )
            completed_at = datetime.now(timezone.utc)
            try:
                dream_cycle_db.complete_run(
                    run_id,
                    stats=self._run_stats | {"aborted": True, "cost_usd": self._spend.total_usd, "spend_reserved_usd": self._spend.reserved_usd, "spend_unknown": bool(self._spend.blocked_reason)},
                    digest="",
                )
            except Exception:
                logger.error("Failed to complete run record after unexpected error")
            return self._aborted_result(run_id, run_type, started_at)
        finally:
            self._restore_interrupt_handlers(previous_handlers)

    def _install_interrupt_handlers(self) -> dict:
        """Turn SIGTERM/SIGINT into RunInterrupted (main thread only; no-op elsewhere)."""
        if threading.current_thread() is not threading.main_thread():
            return {}
        previous = {}

        def _raise(signum, frame):
            raise RunInterrupted(signum)
        for sig in _INTERRUPT_SIGNALS:
            try:
                previous[sig] = signal.signal(sig, _raise)
            except (ValueError, OSError):
                continue
        return previous

    @staticmethod
    def _restore_interrupt_handlers(previous: dict) -> None:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
            except (ValueError, OSError):
                continue

    def _run_pipeline(
        self,
        run_id: str,
        run_type: str,
        scope: dict | None,
        started_at: datetime,
    ) -> DreamCycleResult:
        """Execute the pipeline steps after the run record is created.

        Separated from ``run()`` so that the top-level error handler can
        always attempt to complete the run record on failure.
        """
        # Phase 0: attribute per-call LLM metrics to this run.
        self._current_run_id = run_id
        self._current_started_at = started_at

        # Step 2: Build feedback from historical rejections
        feedback = build_feedback_injection()
        logger.debug("Feedback injection length: %d chars", len(feedback))

        # Step 3: Get memory stats for Explorer context
        stats = dream_cycle_db.get_memory_stats()
        logger.info(
            "Memory stats: total=%d, recent_activity=%d",
            stats["total_count"],
            stats["recent_activity"],
        )

        # Step 3.5: Skim every arrival in the window (cheap Bedrock model, own sub-cap).
        # Never aborts the run on its own failure: None means the Explorer gets the
        # plain arrivals manifest, exactly as before. SpendCapExceeded propagates.
        skim = self._skim_arrivals(run_id, run_type, started_at)
        skim_stats = skim.to_stats() if skim is not None else {}
        self._run_stats.update(skim_stats)
        # The window's skim records (this run's and earlier runs' — idempotent per
        # document) feed the Explorer's inventory and the weighted guaranteed slice.
        self._skim_records = self._load_skim_records(skim, run_id, run_type, started_at)

        # Step 4: Invoke Explorer (with crash/timeout handling). On a scheduled run an
        # Explorer failure no longer aborts the night: the guaranteed arrivals slice
        # (built in _allocate_attention) lets the run still examine what arrived.
        try:
            slices = self.invoke_explorer(feedback, run_type, scope, stats)
            logger.info("Explorer returned %d slices", len(slices))
        except (TimeoutError, RuntimeError):
            if run_type not in ("scheduled", "session_start"):
                logger.error("Explorer agent crashed or timed out (run_id=%s)", run_id)
                dream_cycle_db.complete_run(run_id, stats=self._run_stats | {"aborted": True, "cost_usd": self._spend.total_usd, "spend_reserved_usd": self._spend.reserved_usd, "spend_unknown": bool(self._spend.blocked_reason)}, digest="")
                return self._aborted_result(run_id, run_type, started_at)
            logger.error("Explorer agent crashed or timed out (run_id=%s); continuing with the "
                         "guaranteed arrivals slice only", run_id)
            slices = []

        # Attention allocation + ledger: validate the Explorer's slices against the
        # breadth budget, add a guaranteed arrivals slice if needed, and persist what
        # this run will examine (with metrics) before anything downstream can fail.
        slices = self._allocate_attention(run_id, run_type, started_at, slices)

        # Circuit breaker: empty slices → abort early
        if not slices:
            logger.warning("Circuit breaker: no slices to examine (Explorer returned none and "
                           "nothing arrived), aborting early")
            dream_cycle_db.complete_run(run_id, stats=self._run_stats | self._explorer_input | {"aborted": True, "cost_usd": self._spend.total_usd, "spend_reserved_usd": self._spend.reserved_usd, "spend_unknown": bool(self._spend.blocked_reason)}, digest="")
            return self._aborted_result(run_id, run_type, started_at)

        # Judging order (Phase 4): the guaranteed arrivals slice first, then the
        # Explorer's slices by arrival share — so the spend cap cuts stale material,
        # not the most recent days. Stable when the ledger has no metrics.
        ordered = allocation.order_slices(slices, self._slice_rows)
        slices = [s for s, _ in ordered]
        logger.info("Slice order for the Thinker and the panel: %s", allocation.describe_slice_order(ordered))

        # Step 6: Invoke Thinker for each slice, collect candidates. Budget-aware:
        # a slice is skipped when the remaining budget cannot cover the Thinker call,
        # judging its candidates and judging what is already waiting.
        all_candidates: list[CandidateInsight] = []
        slices_skipped_for_budget = 0
        for i, s in enumerate(slices):
            remaining = self._budget_remaining()
            if remaining is not None:
                needed = EST_THINKER_CALL_USD + MAX_CANDIDATES_PER_SLICE * EST_JUDGE_CANDIDATE_USD + EST_JUDGE_CANDIDATE_USD * len(all_candidates)
                if remaining < needed:
                    slices_skipped_for_budget = len(slices) - i
                    logger.warning(
                        "Budget: $%.2f left of $%.2f, but the next slice needs ~$%.2f (Thinker $%.2f + judging "
                        "2 new and %d pending candidates at $%.2f each); skipping the Thinker for the remaining "
                        "%d slice(s): %s",
                        remaining, self._spend.cap_usd, needed, EST_THINKER_CALL_USD, len(all_candidates),
                        EST_JUDGE_CANDIDATE_USD, slices_skipped_for_budget,
                        "; ".join(sl.name for sl in slices[i:]),
                    )
                    break
            try:
                candidates = self.invoke_thinker(s)
            except SpendCapExceeded:
                raise
            except Exception:
                logger.error(
                    "Thinker failed for slice '%s', skipping", s.name, exc_info=True
                )
                continue
            # Per-slice cap: session-start keeps 2; every other run type keeps
            # MAX_CANDIDATES_PER_SLICE (the Thinker is asked for at most 2, best first).
            cap = MAX_CANDIDATES_PER_SLICE
            if len(candidates) > cap:
                logger.info(
                    "Truncating %d candidates to %d for slice '%s'", len(candidates), cap, s.name,
                )
                candidates = candidates[:cap]
            # Authorized context = the slice snapshots the Thinker actually read.
            # Slice ids come from the Explorer verbatim; the cache is keyed by the
            # canonical DB id, so canonicalise before the lookup (review C S3 2.5).
            authorized = {}
            for mid in s.memory_ids:
                key = canonical_uuid(mid)
                if key is not None and key in self._slice_memory_cache:
                    authorized[key] = self._slice_memory_cache[key]
            for candidate in candidates:
                self._candidate_contexts[id(candidate)] = authorized
            all_candidates.extend(candidates)
            self._run_stats["candidates_generated"] = len(all_candidates)
        logger.info("Total candidates from Thinker: %d%s", len(all_candidates),
                    f" ({slices_skipped_for_budget} slice(s) skipped for budget)" if slices_skipped_for_budget else "")

        accepted: list[CandidateInsight] = []
        rejected: list[CandidateInsight] = []
        candidates_unjudged_for_budget = 0

        # Step 7: Consensus Panel — invoke 4 evaluators per candidate.
        # If an evaluator is unrecoverable after retries (the panel is genuinely
        # down), abort the run loudly — but keep the candidates already evaluated
        # so the run record reflects real persisted work (no zeroed-stats lie /
        # orphaned accepted memories). An infra failure never fabricates a verdict.
        # Soft stop: when the remaining budget cannot cover judging one more
        # candidate, the rest are left unjudged — no verdict stored, none
        # fabricated — and the run completes normally instead of tripping the
        # spend stop threshold mid-candidate.
        panel_aborted = False
        for idx, candidate in enumerate(all_candidates):
            remaining = self._budget_remaining()
            if remaining is not None and remaining < EST_JUDGE_CANDIDATE_USD:
                candidates_unjudged_for_budget = len(all_candidates) - idx
                logger.warning(
                    "Budget: $%.2f left of $%.2f is below the ~$%.2f judging one candidate costs; leaving %d "
                    "candidate(s) unjudged: %s",
                    remaining, self._spend.cap_usd, EST_JUDGE_CANDIDATE_USD, candidates_unjudged_for_budget,
                    "; ".join(c.title for c in all_candidates[idx:]),
                )
                break
            # Authorization BEFORE any paid judging (review C S2 2.3): a candidate
            # that cites outside its slice, proposes a provenance/unknown relation
            # type, or targets a memory it may not mutate is BLOCKED here for $0.
            # store_accepted re-validates after the panel as defence in depth.
            authorized_memories = self._candidate_contexts.get(id(candidate), {})
            try:
                authorize_candidate(candidate, authorized_memories)
            except CandidateAuthorizationError as exc:
                logger.warning("Candidate '%s' blocked before judging: %s", candidate.title, exc)
                self._run_stats["candidates_blocked_unjudged"] = self._run_stats.get("candidates_blocked_unjudged", 0) + 1
                dream_cycle_db.store_candidate(run_id, asdict(candidate) | {"storage_error": type(exc).__name__,
                                               "block_reason": str(exc)}, {}, "BLOCKED")
                continue
            # Grounding audit + one revision before the panel votes (never aborts).
            candidate = self._ground_candidate(candidate)
            if not candidate.content.strip():
                logger.info("Candidate '%s' had nothing supportable after revision; recording as rejected",
                            candidate.title)
                dream_cycle_db.store_candidate(run_id,
                    asdict(candidate) | {"withdrawal_reason": "nothing supportable remained after revision"},
                    {}, "WITHDRAWN")
                self._run_stats["candidates_withdrawn"] = self._run_stats.get("candidates_withdrawn", 0) + 1
                continue
            try:
                verdict_a = self._invoke_evaluator_safe(candidate, "skeptic")
                verdict_b = self._invoke_evaluator_safe(candidate, "advocate")
                verdict_c = self._invoke_evaluator_safe(candidate, "epistemologist")
                verdict_d = self._invoke_evaluator_safe(candidate, "methodologist")
            except RuntimeError as exc:
                logger.error(
                    "Evaluator unrecoverable for candidate '%s' after retries; "
                    "aborting run after %d/%d candidates (accepted=%d, rejected=%d): %s",
                    candidate.title, len(accepted) + len(rejected),
                    len(all_candidates), len(accepted), len(rejected), exc,
                )
                panel_aborted = True
                break

            evaluator_verdicts = [verdict_a, verdict_b, verdict_c, verdict_d]
            final = tally_consensus(evaluator_verdicts)

            verdicts_dict = verdict_columns(evaluator_verdicts)

            candidate_dict = asdict(candidate)

            try:
                if final == "ACCEPTED":
                    store_accepted(candidate, run_id=run_id, verdicts=verdicts_dict,
                                   authorized_memories=authorized_memories)
                    accepted.append(candidate)
                    self._run_stats["candidates_accepted"] = len(accepted)
                else:
                    dream_cycle_db.store_candidate(run_id, candidate_dict, verdicts_dict, "REJECTED")
                    rejected.append(candidate)
                    self._run_stats["candidates_rejected"] = len(rejected)
            except (CandidateAuthorizationError, ValueError, psycopg2.IntegrityError) as exc:
                # The failed candidate transaction was rolled back in full. Keep
                # prior successes, retain real votes, and distinguish authorization
                # or storage failure from a quality rejection by the panel.
                logger.warning("Candidate storage blocked for %s: %s", candidate.title, _failure_message(exc))
                self._run_stats["candidates_storage_failed"] = self._run_stats.get("candidates_storage_failed", 0) + 1
                dream_cycle_db.store_candidate(run_id, candidate_dict | {"storage_error": type(exc).__name__,
                                               "block_reason": _failure_message(exc)},
                                               verdicts_dict, "BLOCKED")

            logger.info(
                "Candidate '%s': %s", candidate.title, final
            )

        # Step 8: Generate digest and complete run. The digest is a convenience
        # artifact: if writing it fails (permissions, disk, rendering) the run
        # still completes with its real counts and digest="" (review C S2).
        completed_at = datetime.now(timezone.utc)
        try:
            digest = generate_digest(run_id, accepted, rejected,
                generated=len(all_candidates), feedback_injected=bool(feedback),
                cost_usd=self._spend.total_usd, aborted=panel_aborted,
                storage_failed=self._run_stats.get("candidates_storage_failed", 0))
        except Exception:
            logger.error("Digest could not be written for run %s; completing the run without it", run_id,
                         exc_info=True)
            digest = ""

        # Step 9: Post-run maintenance — expire stale relationships
        try:
            expired_count = dream_cycle_db.expire_stale_relationships(days_threshold=90)
            if expired_count and expired_count > 0:
                logger.info("Expired %d stale relationships (90+ days without access)", expired_count)
        except Exception:
            logger.debug("Relationship decay skipped (non-critical)", exc_info=True)

        budget_stats = {}
        if slices_skipped_for_budget:
            budget_stats["slices_skipped_for_budget"] = slices_skipped_for_budget
        if candidates_unjudged_for_budget:
            budget_stats["candidates_unjudged_for_budget"] = candidates_unjudged_for_budget
        dream_cycle_db.complete_run(
            run_id,
            stats={
                **self._run_stats,
                "aborted": panel_aborted,
                "candidates_unjudged": max(0, len(all_candidates) - len(accepted) - len(rejected) - self._run_stats.get("candidates_storage_failed", 0) - self._run_stats.get("candidates_withdrawn", 0) - self._run_stats.get("candidates_blocked_unjudged", 0)),
                "cost_usd": self._spend.total_usd, "spend_reserved_usd": self._spend.reserved_usd,
                "spend_unknown": bool(self._spend.blocked_reason),
                "candidates_generated": len(all_candidates),
                "candidates_accepted": len(accepted),
                "candidates_rejected": len(rejected),
                **skim_stats,
                **self._explorer_input,
                **budget_stats,
            },
            digest=digest,
            explorer_output=json.dumps([{"name": s.name, "strategy": s.strategy, "memory_count": len(s.memory_ids)} for s in slices]),
            explorer_feedback_injected=feedback,
        )

        return DreamCycleResult(
            run_id=run_id,
            run_type=run_type,
            started_at=started_at,
            completed_at=completed_at,
            candidates_generated=len(all_candidates),
            candidates_accepted=len(accepted),
            candidates_rejected=len(rejected),
            digest_path=digest,
            aborted_early=panel_aborted,
            cost_usd=self._spend.total_usd,
            spend_unknown=bool(self._spend.blocked_reason),
            candidates_unjudged=max(0, len(all_candidates) - len(accepted) - len(rejected) - self._run_stats.get("candidates_storage_failed", 0) - self._run_stats.get("candidates_withdrawn", 0) - self._run_stats.get("candidates_blocked_unjudged", 0)),
        )














    @property
    def db(self):
        return self._database if self._database is not None else dream_cycle_db

    def invoke_explorer(self, feedback: str, run_type: str, scope: dict | None=None, stats: dict | None=None):
        return explorer.invoke_explorer(self, feedback, run_type, scope, stats)

    def _allocate_attention(self, run_id: str, run_type: str, started_at: datetime, slices: list[MemorySlice]):
        return explorer._allocate_attention(self, run_id, run_type, started_at, slices)

    def invoke_thinker(self, slice: MemorySlice):
        return thinker.invoke_thinker(self, slice)

    def invoke_evaluator(self, candidate: CandidateInsight, role: str, stage: str | None=None):
        return evaluator.invoke_evaluator(self, candidate, role, stage)

    def _invoke_evaluator_safe(self, candidate: CandidateInsight, role: str):
        return evaluator._invoke_evaluator_safe(self, candidate, role)

    def _ground_candidate(self, candidate: CandidateInsight):
        return grounding._ground_candidate(self, candidate)

    def _revise_candidate(self, candidate: CandidateInsight, audit: EvaluatorVerdict):
        return grounding._revise_candidate(self, candidate, audit)

    def _load_skim_records(self, skim: SkimResult | None, run_id: str, run_type: str, started_at: datetime):
        return skim_stage._load_skim_records(self, skim, run_id, run_type, started_at)

    def _skim_arrivals(self, run_id: str, run_type: str, started_at: datetime):
        return skim_stage._skim_arrivals(self, run_id, run_type, started_at)

    def _aborted_result(self, run_id: str, run_type: str, started_at: datetime):
        return run_result._aborted_result(self, run_id, run_type, started_at)
