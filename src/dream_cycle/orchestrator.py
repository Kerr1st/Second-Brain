"""Dream Cycle Orchestrator — four-agent pipeline coordination.

Coordinates Explorer → Thinker → Consensus Panel pipeline.
Delegates to consensus, storage, digest, and feedback modules.

Requirements: 1, 3, 9 (this file — task 6.1 skeleton)
"""

from __future__ import annotations

import json
import logging
import os
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
from src.dream_cycle.consensus import tally_consensus
from src.dream_cycle.storage import store_accepted, check_duplicate
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
from src.skim import store as skim_store
from src.skim.cluster import cluster_window
from src.skim.runner import SkimResult, skim_window

logger = logging.getLogger(__name__)

# Stats dict for aborted/failed runs — all counts zero.
_ZERO_STATS = {
    "candidates_generated": 0,
    "candidates_accepted": 0,
    "candidates_rejected": 0,
}

# Evaluator resilience: retry transient evaluator failures before treating the
# panel as broken. An infra failure (timeout/crash/unparseable output) must never
# be silently recorded as a quality REJECT vote — retry, then abort loudly.
EVALUATOR_MAX_ATTEMPTS = 3


def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _unwrap_nested_candidate(out: dict) -> dict:
    """A revision sometimes puts the whole candidate JSON into the ``content`` string
    (observed 2026-09-18: an accepted memory was stored as a JSON blob). If ``content``
    parses as a dict that itself has a ``content`` key, use the inner object's fields."""
    content = out.get("content")
    if isinstance(content, str) and content.lstrip().startswith("{"):
        try:
            inner = json.loads(content)
        except ValueError:
            return out
        if isinstance(inner, dict) and isinstance(inner.get("content"), str):
            merged = dict(out)
            merged.update({k: v for k, v in inner.items() if k in REVISION_SCHEMA["properties"]})
            return merged
    return out


def _as_str_list(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return []
# The Explorer now enumerates channels and searches for cross-channel echoes;
# with ~15-20s of kiro-cli overhead per tool turn a thorough pass takes 10-15
# minutes (a 600s ceiling timed out at 30 tool calls on 2026-09-17).
EXPLORER_TIMEOUT_S = int(os.environ.get("EXPLORER_TIMEOUT", "1200"))
THINKER_TIMEOUT_S = int(os.environ.get("THINKER_TIMEOUT", "1200"))
# Grounding audit + revision (docs/PLAN-panel-acceptance.md). Skipped once the run has
# spent this share of its cap, so the panel itself always gets to vote.
GROUNDING_SPEND_SHARE = float(os.environ.get("GROUNDING_SPEND_SHARE", "0.8"))
GROUNDING_AUDIT_ROLE = "skeptic"
# Hard cap on candidates per slice (the Thinker is asked for at most 2).
MAX_CANDIDATES_PER_SLICE = int(os.environ.get("MAX_CANDIDATES_PER_SLICE", "3"))
EVALUATOR_BACKOFF_S = 2.0
# Skim stage (docs/PLAN-skim-stage.md, Step 3.5): every parent-level arrival in the
# window is read once by the `skim` role's cheap Bedrock model. Its own sub-cap is
# SKIM_SPEND_CAP_USD (env SECOND_BRAIN_SKIM_CAP_USD) and never more than
# SKIM_RUN_CAP_SHARE of the run cap; the spend still counts toward the run cap.
SKIM_SPEND_CAP_USD = 1.00
SKIM_RUN_CAP_SHARE = 0.15
#: Operational off-switch: set to 1/true/yes to skip Step 3.5 (manifest as before).
SKIM_DISABLED_ENV = "SECOND_BRAIN_SKIM_DISABLED"
# Budget-aware Thinker and panel (docs/PLAN-skim-stage.md, Phase 4). Per-unit
# estimates from run 3a1263a6 (2026-09-18, Bedrock profile): the Thinker cost
# $0.21–0.30 per slice; judging one candidate — grounding audit + revision + four
# votes — cost ~$0.66. Rounded up so the rule errs toward finishing what was
# started rather than starting more. Before invoking the Thinker for a slice the
# run must be able to afford the call, judging its (up to two) candidates and
# judging every candidate already generated; before judging a candidate it must
# be able to afford that candidate. Otherwise the slice / the remaining
# candidates are skipped, counted (slices_skipped_for_budget,
# candidates_unjudged_for_budget) and the run completes normally — the hard
# cap in SpendTracker stays as the backstop.
EST_THINKER_CALL_USD = 0.30
EST_JUDGE_CANDIDATE_USD = 0.70


def _skim_cap_usd(run_cap_usd: float) -> float:
    """The skim's sub-cap: SECOND_BRAIN_SKIM_CAP_USD (default SKIM_SPEND_CAP_USD),
    bounded to SKIM_RUN_CAP_SHARE of the run cap. A bad env value falls back to the
    default with a warning, like SpendTracker.from_env."""
    cap = SKIM_SPEND_CAP_USD
    cap = cap_from_env("SECOND_BRAIN_SKIM_CAP_USD", cap)
    if run_cap_usd and run_cap_usd > 0:
        cap = min(cap, SKIM_RUN_CAP_SHARE * float(run_cap_usd))
    return cap


class DreamCycleOrchestrator:
    """Coordinates the four-agent dream cycle pipeline."""

    def __init__(self):
        self.resolver = default_resolver()
        self._capture_dir: str | None = None
        self._current_run_id: str | None = None
        self._current_started_at: datetime | None = None
        # Metered-spend guard (reset per run) and the slice contents delivered to
        # a tool-less Thinker, kept so evaluators can ground against the same text.
        self._spend = SpendTracker.from_env()
        self._run_stats = dict(_ZERO_STATS)
        self._slice_memory_cache: dict[str, dict] = {}
        # Skim records for the run's arrivals window (Step 3.5 → Explorer inventory
        # and the weighted guaranteed slice) and what the Explorer was actually given.
        self._skim_records: list[dict] = []
        self._explorer_input: dict = {}
        # Ledger rows (origin + metrics) aligned with the slices _allocate_attention
        # returned — the judging order (Phase 4) is derived from them.
        self._slice_rows: list[dict] = []

    def _invoker_for(self, role: str):
        """Resolve the Invoker for a role from the active backend profile."""
        return self.resolver.invoker_for(role)

    def _role_has_tools(self, role: str) -> bool:
        """Whether the role's backend can attach MCP tools (agentic CLIs can;
        the Direct-API family cannot and must be fed a context packet)."""
        backend = self.resolver.spec_for(role).backend
        caps = BACKEND_CAPABILITIES.get(backend)
        return caps.supports_mcp if caps is not None else True

    def _invoke_metered(self, role: str, **kwargs):
        """All Dream Cycle requests share strict accounting, including failures."""
        try:
            return invoke_metered(
                self._invoker_for(role), self.resolver.spec_for(role), self._spend, **kwargs
            )
        except (TimeoutError, RuntimeError, ValueError) as exc:
            if self._spend.blocked_reason:
                raise UsageUnavailable(self._spend.blocked_reason) from exc
            raise

    def _budget_remaining(self) -> float | None:
        """Dollars left under the run cap, from the SpendTracker's real figures
        (never estimates), or None when no cap applies. With an all-Kiro profile
        nothing is metered, the total stays at zero and the budget rules never
        fire — the cap binds only where usage is real."""
        cap = getattr(self._spend, "cap_usd", None)
        if not isinstance(cap, (int, float)) or cap <= 0:
            return None
        return float(cap) - float(self._spend.total_usd)

    def _backend_provenance(self) -> dict:
        """Snapshot the active profile's (backend, model, effort) per role.

        Persisted on the run record so a run's candidates/verdicts stay auditable
        across a backend swap or the Mac Mini cutover (Kiro-Opus era vs
        Claude-Code-on-Bedrock era). Built here from the resolver (read-only)
        rather than on the Resolver itself, to keep this change out of the
        backends lane. See docs/MODEL-BACKENDS.md.
        """
        provenance: dict[str, dict] = {}
        for role in DREAM_ROLES:
            try:
                spec = self.resolver.spec_for(role)
            except KeyError:
                # Optional stage not configured (e.g. no skim role in a hand-built
                # profile): the run proceeds without it, so provenance omits it.
                continue
            provenance[role] = {
                "backend": spec.backend,
                "model": spec.model,
                "effort": spec.effort,
            }
        return provenance

    def _capture(self, name: str, *, inputs: dict | None = None, output: object = None) -> None:
        """Write agent call inputs/outputs to capture_dir as JSON fixtures.

        No-op when capture_dir is not set. Failures are logged but never
        propagate — fixture capture must not break the pipeline.
        """
        if not self._capture_dir:
            return
        try:
            os.makedirs(self._capture_dir, exist_ok=True)
            data: dict = {}
            if inputs is not None:
                data["inputs"] = inputs
            if output is not None:
                data["output"] = output
            path = os.path.join(self._capture_dir, f"{name}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            logger.debug("Captured fixture: %s", path)
        except Exception:
            logger.warning("Failed to capture fixture %s", name, exc_info=True)

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
        )

    def run(self, run_type: str, scope: dict | None = None, capture_dir: str | None = None) -> DreamCycleResult:
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
        # be completed, even on error paths.
        try:
            return self._run_pipeline(run_id, run_type, scope, started_at)
        except psycopg2.OperationalError:
            logger.error("Database unreachable during pipeline execution (run_id=%s)", run_id)
            try:
                dream_cycle_db.complete_run(run_id, stats=self._run_stats | {
                    "aborted": True, "cost_usd": self._spend.total_usd,
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
                dream_cycle_db.complete_run(run_id, stats=self._run_stats | {"aborted": True, "cost_usd": self._spend.total_usd, "spend_unknown": bool(self._spend.blocked_reason)}, digest="")
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
                    stats=self._run_stats | {"aborted": True, "cost_usd": self._spend.total_usd, "spend_unknown": bool(self._spend.blocked_reason)},
                    digest="",
                )
            except Exception:
                logger.error("Failed to complete run record after unexpected error")
            return self._aborted_result(run_id, run_type, started_at)

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
                dream_cycle_db.complete_run(run_id, stats=_ZERO_STATS | skim_stats, digest="")
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
            dream_cycle_db.complete_run(run_id, stats=_ZERO_STATS | skim_stats | self._explorer_input, digest="")
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
                needed = EST_THINKER_CALL_USD + 2 * EST_JUDGE_CANDIDATE_USD + EST_JUDGE_CANDIDATE_USD * len(all_candidates)
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
            cap = 2 if run_type == "session_start" else MAX_CANDIDATES_PER_SLICE
            if len(candidates) > cap:
                logger.info(
                    "Truncating %d candidates to %d for slice '%s'", len(candidates), cap, s.name,
                )
                candidates = candidates[:cap]
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
        # hard cap mid-candidate.
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
            # Grounding audit + one revision before the panel votes (never aborts).
            candidate = self._ground_candidate(candidate)
            if not candidate.content.strip():
                logger.info("Candidate '%s' had nothing supportable after revision; recording as rejected",
                            candidate.title)
                dream_cycle_db.store_candidate(
                    run_id, asdict(candidate),
                    {f"evaluator_{k}_verdict": "REJECT" for k in "abcd"}
                    | {f"evaluator_{k}_reasoning": "grounding audit: nothing supportable remained after revision"
                       for k in "abcd"},
                    "REJECTED",
                )
                rejected.append(candidate)
                self._run_stats["candidates_rejected"] = len(rejected)
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

            verdicts_dict = {
                "evaluator_a_verdict": verdict_a.verdict,
                "evaluator_a_reasoning": verdict_a.reasoning,
                "evaluator_b_verdict": verdict_b.verdict,
                "evaluator_b_reasoning": verdict_b.reasoning,
                "evaluator_c_verdict": verdict_c.verdict,
                "evaluator_c_reasoning": verdict_c.reasoning,
                "evaluator_d_verdict": verdict_d.verdict,
                "evaluator_d_reasoning": verdict_d.reasoning,
                # Graded detail (migration 013): score + issue lists per seat.
                "evaluator_details": {
                    v.role: {"score": v.score, "blocking_issues": v.blocking_issues,
                             "advisory_issues": v.advisory_issues}
                    for v in evaluator_verdicts
                },
            }

            candidate_dict = asdict(candidate)

            if final == "ACCEPTED":
                existing = check_duplicate(candidate.content, threshold=0.85)
                memory_id = None
                if existing is None:
                    memory_id = store_accepted(candidate)
                else:
                    logger.info(
                        "Duplicate detected for '%s', matched memory %s",
                        candidate.title,
                        existing,
                    )

                dream_cycle_db.store_candidate(
                    run_id, candidate_dict, verdicts_dict, "ACCEPTED", memory_id
                )
                accepted.append(candidate)
                self._run_stats["candidates_accepted"] = len(accepted)
            else:  # REJECTED — no DEFERRED branch
                dream_cycle_db.store_candidate(
                    run_id, candidate_dict, verdicts_dict, "REJECTED"
                )
                rejected.append(candidate)
                self._run_stats["candidates_rejected"] = len(rejected)

            logger.info(
                "Candidate '%s': %s", candidate.title, final
            )

        # Step 8: Generate digest and complete run
        completed_at = datetime.now(timezone.utc)
        digest = generate_digest(run_id, accepted, rejected)

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
                "cost_usd": self._spend.total_usd,
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
        )


    def _invoke_evaluator_safe(self, candidate: CandidateInsight, role: str) -> EvaluatorVerdict:
        """Invoke an evaluator, retrying transient failures before giving up.

        Transient infra failures (timeout / subprocess error / unparseable output)
        are retried up to ``EVALUATOR_MAX_ATTEMPTS`` times with linear backoff. If
        the evaluator still fails, the exception is raised so the run aborts loudly
        (the top-level handler sets ``aborted_early`` -> exit 2 + notification)
        instead of silently recording a REJECT vote.

        An infrastructure failure must never masquerade as a quality verdict: a
        crashed evaluator is the judging machinery breaking, not a verdict on the
        insight. (This mirrors how ``invoke_explorer`` already aborts on crash.)

        Args:
            candidate: The CandidateInsight to evaluate.
            role: One of 'skeptic', 'advocate', 'epistemologist', 'methodologist'.

        Returns:
            EvaluatorVerdict — the real verdict once the evaluator succeeds.

        Raises:
            RuntimeError: If the evaluator fails every attempt (unrecoverable).
        """
        last_exc: Exception | None = None
        for attempt in range(1, EVALUATOR_MAX_ATTEMPTS + 1):
            try:
                return self.invoke_evaluator(candidate, role)
            except (TimeoutError, RuntimeError, ValueError) as exc:
                last_exc = exc
                logger.warning(
                    "Evaluator '%s' failed for '%s' (attempt %d/%d): %s",
                    role, candidate.title, attempt, EVALUATOR_MAX_ATTEMPTS, exc,
                )
                if attempt < EVALUATOR_MAX_ATTEMPTS:
                    time.sleep(EVALUATOR_BACKOFF_S * attempt)
        logger.error(
            "Evaluator '%s' unrecoverable after %d attempts for '%s'; aborting run",
            role, EVALUATOR_MAX_ATTEMPTS, candidate.title,
        )
        raise RuntimeError(
            f"Evaluator '{role}' unrecoverable after {EVALUATOR_MAX_ATTEMPTS} attempts"
        ) from last_exc



    def invoke_explorer(
        self, feedback: str, run_type: str, scope: dict | None = None,
        stats: dict | None = None,
    ) -> list[MemorySlice]:
        """Invoke Explorer agent and parse memory slices.

        Builds the Explorer prompt, invokes via AgentInvoker with MCP config,
        and parses the JSON output into MemorySlice objects.

        Args:
            feedback: Formatted feedback injection text (may be empty).
            run_type: One of scheduled, post_learn, session_start, user_triggered.
            scope: Optional scoping dict for non-scheduled runs.
            stats: Optional pre-fetched memory stats (avoids redundant DB call).

        Returns:
            List of MemorySlice objects (may be empty for circuit breaker).
        """
        if stats is None:
            stats = dream_cycle_db.get_memory_stats()
        date_range = stats["date_range"]

        # Build human-readable date range string
        date_min = date_range.get("min")
        date_max = date_range.get("max")
        if date_min and date_max:
            date_range_str = f"{date_min:%B %Y} to {date_max:%B %Y}"
        else:
            date_range_str = "unknown"

        # Build Explorer prompt
        prompt = get_explorer_prompt(
            memory_count=stats["total_count"],
            date_range=date_range_str,
            feedback_injection=feedback,
            run_type=run_type,
            scope=scope,
            strategy_usage=dream_cycle_db.get_strategy_usage(n_cycles=10),
        )

        # Arrivals block: what was captured since the previous run, by channel, with
        # ids. Without this the Explorer knew only "N new memories" and had no reason
        # to look at any of them. With skim records for the window (Step 3.5) the
        # block is the full inventory — one line per document — instead of the
        # 4-samples-per-channel manifest (docs/PLAN-skim-stage.md, Phase 3).
        manifest = ""
        self._explorer_input = {}
        try:
            window_start = ledger.arrival_window_start(self._current_run_id, run_type,
                                                       self._current_started_at, dcdb=dream_cycle_db)
            arrivals = dream_cycle_db.get_arrivals_summary(window_start, allocation.MANIFEST_SAMPLES_PER_SOURCE)
            arrivals = arrivals if isinstance(arrivals, list) else []
            if self._skim_records:
                counts = {a["source_type"]: int(a["documents"]) for a in arrivals
                          if isinstance(a, dict) and a.get("source_type") and a.get("documents")}
                render = allocation.render_arrivals_inventory(window_start, self._skim_records,
                                                              counts_by_channel=counts)
                manifest = render.text
                self._explorer_input = {"explorer_input": "inventory", "inventory_listed": render.listed,
                                        "inventory_docs": render.total, "inventory_chars": len(manifest)}
                logger.info("Explorer arrivals input: inventory — %d of %d skimmed documents listed "
                            "(%d lines, %d chars)", render.listed, render.total, manifest.count("\n") + 1, len(manifest))
            else:
                manifest = allocation.build_arrivals_manifest(window_start, arrivals)
                self._explorer_input = {"explorer_input": "manifest"}
                logger.info("Explorer arrivals input: manifest — %d channels, %d sample ids per channel "
                            "(%d lines, %d chars); no skim records for this window",
                            len(arrivals), allocation.MANIFEST_SAMPLES_PER_SOURCE, manifest.count("\n") + 1,
                            len(manifest))
        except Exception:
            logger.warning("Arrivals manifest unavailable for this run", exc_info=True)

        user_message = (
            f"Run type: {run_type}. "
            f"Memory count: {stats['total_count']}. "
            f"Recent activity (7d): {stats['recent_activity']} new memories. "
            "Please assemble 0-5 memory slices using diverse strategies and meet the slice budget."
            + (f"\n\n{manifest}" if manifest else "")
        )

        logger.info("Invoking Explorer agent (run_type=%s)", run_type)
        result = self._invoke_metered("explorer",
            system_prompt=prompt,
            user_message=user_message,
            tools=True,
            timeout=EXPLORER_TIMEOUT_S,
            effort=self.resolver.spec_for("explorer").effort,
            stage="explorer",
            run_id=self._current_run_id,
        )

        self._capture("explorer", inputs={"system_prompt": prompt, "user_message": user_message}, output=result.get("output"))

        # Parse output into MemorySlice objects
        raw_slices = result["output"]
        if not isinstance(raw_slices, list):
            logger.warning("Explorer output is not a list, wrapping: %s", type(raw_slices))
            raw_slices = [raw_slices] if raw_slices else []

        slices = []
        for raw in raw_slices:
            try:
                slice_obj = MemorySlice(
                    name=raw.get("name", ""),
                    strategy=raw.get("strategy", ""),
                    memory_ids=raw.get("memory_ids", []),
                    memory_titles=raw.get("memory_titles", []),
                    hypothesis=raw.get("hypothesis", ""),
                )
                slices.append(slice_obj)
                logger.debug(
                    "Parsed slice: name=%s, strategy=%s, memories=%d",
                    slice_obj.name,
                    slice_obj.strategy,
                    len(slice_obj.memory_ids),
                )
            except (KeyError, TypeError) as exc:
                logger.warning("Failed to parse Explorer slice: %s — %s", raw, exc)

        if not slices:
            # Surface the raw agent output so a scheduled 0-slice abort is diagnosable
            # (distinguishes a genuine "nothing to do" from a degraded/failed invocation,
            # e.g. a model call that returned empty when the host woke for the 4am job).
            raw_head = (result.get("raw") or "")[:1000]
            logger.warning(
                "Explorer produced no usable slices (raw_candidates=%s). Raw output head: %r",
                len(raw_slices) if isinstance(raw_slices, list) else "n/a", raw_head,
            )
        return slices

    def _allocate_attention(self, run_id: str, run_type: str, started_at: datetime,
                            slices: list[MemorySlice]) -> list[MemorySlice]:
        """Enforce the slice budget, add the guaranteed arrivals slice, persist the ledger.

        Never aborts a run: on any failure the Explorer's slices are returned as-is.
        Scoped runs (post_learn, user_triggered) are exempt from the budget — their
        attention is directed by the scope — but are still recorded. The ledger rows
        of the returned slices are kept on ``self._slice_rows`` (same order) for the
        judging order; they are empty when the ledger could not be computed.
        """
        self._slice_rows = []
        try:
            window_start = ledger.arrival_window_start(run_id, run_type, started_at, dcdb=dream_cycle_db)
            provenance = ledger.fetch_provenance(slices, dcdb=dream_cycle_db)
            if provenance is None:
                logger.info("Attention: provenance lookup unavailable; slices used as-is, ledger skipped")
                return slices
            rows = ledger.compute_slice_rows(run_id, run_type, slices, started_at, dcdb=dream_cycle_db,
                                             window_start=window_start, provenance=provenance)
            metrics = [r["metrics"] for r in rows]
            origins: dict[int, str] = {}
            extra_metrics: dict[int, dict] = {}

            if run_type in ("scheduled", "session_start"):
                kept, kept_metrics, needs_arrivals = allocation.enforce_budget(slices, metrics)
                if needs_arrivals:
                    # With skim records half the slice goes to the most salient / novel
                    # arrivals (docs/PLAN-skim-stage.md, Phase 3); without them this is
                    # the stratified sample it has always been.
                    extra, selection = allocation.build_arrivals_selection(
                        window_start, dcdb=dream_cycle_db, skim_records=self._skim_records or None)
                    if extra is not None:
                        kept.append(extra)
                        origins[len(kept) - 1] = allocation.ARRIVALS_ORIGIN
                        extra_metrics[len(kept) - 1] = selection.metrics
                        logger.info("Added guaranteed arrivals slice with %d memories (%s): %s",
                                    len(extra.memory_ids),
                                    f"{selection.skim_picked} picked for salience/novelty from the skim, "
                                    f"{len(extra.memory_ids) - selection.skim_picked} stratified"
                                    if selection.skim_weighted else "stratified, no skim records",
                                    extra.name)
                    else:
                        logger.info("No arrivals to build a guaranteed slice from")
                slices = kept
                # Re-resolve: the arrivals slice brings ids the first lookup did not cover.
                provenance = ledger.fetch_provenance(slices, dcdb=dream_cycle_db) or provenance
                rows = ledger.compute_slice_rows(run_id, run_type, slices, started_at, origins,
                                                 dcdb=dream_cycle_db, window_start=window_start,
                                                 provenance=provenance)
                for i, m in extra_metrics.items():
                    rows[i]["metrics"].update(m)

            dream_cycle_db.record_slices(run_id, rows)
            ledger.log_run_metrics(ledger.run_metrics(rows))
            self._slice_rows = rows
        except Exception:
            logger.warning("Attention allocation failed (non-fatal); using Explorer slices as-is", exc_info=True)
            self._slice_rows = []
        return slices

    def _load_skim_records(self, skim: SkimResult | None, run_id: str, run_type: str,
                           started_at: datetime) -> list[dict]:
        """The window's skim records for the Explorer inventory and the weighted
        guaranteed slice (docs/PLAN-skim-stage.md, Phase 3).

        Fetched only when Step 3.5 ran and something is stored for the window
        (``skimmed + reused > 0``). The records are then clustered by their
        existing embeddings (``[c<k>]`` tags; no model calls). Never raises:
        any failure logs a WARNING and returns ``[]``, which makes the Explorer
        fall back to the plain manifest.
        """
        if skim is None or (skim.skimmed + skim.reused) <= 0:
            return []
        try:
            window_start = ledger.arrival_window_start(run_id, run_type, started_at, dcdb=dream_cycle_db)
            records = skim_store.fetch_window_records(window_start, skim.model, skim.prompt_version)
        except Exception:
            logger.warning("Skim records unavailable for the window (non-fatal); the Explorer gets the manifest",
                           exc_info=True)
            return []
        if not isinstance(records, list) or not records:
            logger.warning("Skim ran (%d skimmed, %d reused) but no records came back for the window",
                           skim.skimmed, skim.reused)
            return []
        try:
            cluster_window(records, model=skim.model, prompt_version=skim.prompt_version)
        except Exception:
            logger.warning("Skim clustering failed (non-fatal); inventory without cluster tags", exc_info=True)
        logger.info("Skim records loaded for the window: %d (of %d arrivals)", len(records), skim.docs_total)
        return records

    def _skim_arrivals(self, run_id: str, run_type: str, started_at: datetime) -> SkimResult | None:
        """Step 3.5 — skim every arrival in the run's window (docs/PLAN-skim-stage.md).

        Resolves the ``skim`` role through the active profile and runs
        :func:`~src.skim.runner.skim_window` over ``ledger.arrival_window_start``,
        metering every call through the run's SpendTracker under the skim's own
        sub-cap (:func:`_skim_cap_usd`). Returns the counts, or ``None`` — with one
        WARNING — when the role is not configured, resolves to a non-Bedrock /
        agentic backend, or anything at all fails: the run then continues exactly
        as before (the Explorer gets the plain arrivals manifest). Only
        :class:`SpendCapExceeded` propagates, like in every other stage.

        Operational off-switch: ``SECOND_BRAIN_SKIM_DISABLED=1`` (also ``true``/``yes``)
        skips the stage with one WARNING and no model calls.
        """
        if os.environ.get(SKIM_DISABLED_ENV, "").strip().lower() in {"1", "true", "yes"}:
            logger.warning("Skim skipped: %s is set; the Explorer gets the arrivals manifest", SKIM_DISABLED_ENV)
            return None
        try:
            spec = self.resolver.spec_for(SKIM_ROLE)
        except KeyError:
            logger.warning("Skim skipped: role %r is not configured in the active backend profile", SKIM_ROLE)
            return None
        try:
            backend = getattr(spec, "backend", None)
            caps = BACKEND_CAPABILITIES.get(backend)
            if caps is None or caps.supports_mcp:
                logger.warning("Skim skipped: role %r must run on the direct Bedrock backend, not %r",
                               SKIM_ROLE, backend)
                return None
            invoker = self._invoker_for(SKIM_ROLE)
            window_start = ledger.arrival_window_start(run_id, run_type, started_at, dcdb=dream_cycle_db)
            cap = _skim_cap_usd(self._spend.cap_usd)
            result = skim_window(
                window_start,
                invoker=invoker,
                model=spec.model,
                prompt_version=SKIM_PROMPT_VERSION,
                tracker=self._spend,
                cap_usd=cap,
                run_id=run_id,
                effort=spec.effort or "low",
            )
        except SpendCapExceeded:
            raise
        except Exception:
            logger.warning("Skim failed (non-fatal); the Explorer gets the arrivals manifest as before",
                           exc_info=True)
            return None
        logger.info(
            "Skim (%s): %d arrival(s) since %s — %d skimmed, %d reused, %d failed, %d invalid, %d coerced, "
            "%d truncated; quotes %d/%d verbatim; $%.4f of $%.2f sub-cap in %.0fs%s",
            spec.model, result.docs_total, window_start.isoformat(), result.skimmed, result.reused,
            result.failed, result.invalid, result.coerced, result.truncated, result.quotes_verbatim,
            result.quotes_emitted, result.cost_usd, cap, result.duration_s,
            " (stopped at sub-cap)" if result.aborted_on_cap else "",
        )
        return result

    def invoke_thinker(
        self, slice: MemorySlice,
    ) -> list[CandidateInsight]:
        """Invoke Thinker agent with a memory slice.

        Builds a JSON user message containing the memory slice data,
        then invokes the Thinker via AgentInvoker with MCP config.

        Args:
            slice: A MemorySlice assembled by the Explorer.

        Returns:
            List of CandidateInsight objects (may be empty).
        """
        # Build the user message payload
        payload: dict = {
            "memory_slice": {
                "name": slice.name,
                "strategy": slice.strategy,
                "memory_ids": slice.memory_ids,
                "memory_titles": slice.memory_titles,
                "hypothesis": slice.hypothesis,
            },
        }

        # The evaluators are always tool-less, so cache the slice contents for
        # their grounding checks regardless of how the Thinker reads memories.
        # Every Thinker gets the slice contents inline (the "B2 context packet"):
        # a tool-capable one (agentic CLI) used to re-read 10-20 memories over MCP
        # at ~20s per turn and timed out on episodic slices (2026-09-17); it now
        # keeps its tools for CLS-interleaving searches only. A tool-less one
        # (Direct-API / Bedrock) reasons from the packet alone.
        thinker_has_tools = self._role_has_tools("thinker")
        memories = load_memories_by_ids(slice.memory_ids)
        for memory in memories:
            self._slice_memory_cache[str(memory.get("id"))] = memory
        payload["memory_slice"]["memories"] = build_packet(memories)
        payload["note"] = TOOLED_NOTE if thinker_has_tools else TOOLLESS_NOTE
        if len(memories) < len(slice.memory_ids):
            logger.warning(
                "Thinker packet: loaded %d of %d slice memories for %s",
                len(memories), len(slice.memory_ids), slice.name,
            )

        user_message = json.dumps(payload, default=str)

        # Get the static Thinker prompt
        prompt = get_thinker_prompt()

        logger.info(
            "Invoking Thinker agent (slice=%s, tools=%s)",
            slice.name, thinker_has_tools,
        )
        result = self._invoke_metered("thinker",
            system_prompt=prompt,
            user_message=user_message,
            tools=thinker_has_tools,
            timeout=THINKER_TIMEOUT_S,
            effort=self.resolver.spec_for("thinker").effort,
            stage="thinker",
            run_id=self._current_run_id,
        )

        safe_name = slice.name.replace(" ", "_").replace("/", "_")[:50]
        self._capture(f"thinker_{safe_name}", inputs={"system_prompt": prompt, "user_message": user_message}, output=result.get("output"))

        # Parse output into CandidateInsight objects
        raw_candidates = result["output"]
        if not isinstance(raw_candidates, list):
            logger.warning(
                "Thinker output is not a list, wrapping: %s", type(raw_candidates)
            )
            raw_candidates = [raw_candidates] if raw_candidates else []

        candidates = []
        for raw in raw_candidates:
            try:
                candidate = CandidateInsight(
                    title=raw.get("title", ""),
                    type=raw.get("type", "insight"),
                    operation=raw.get("operation", "CREATE"),
                    target_memory_id=raw.get("target_memory_id"),
                    supersedes_reason=raw.get("supersedes_reason"),
                    schema_operation=raw.get("schema_operation", "assimilation"),
                    schema_note=raw.get("schema_note", ""),
                    confidence=raw.get("confidence", "medium"),
                    confidence_reasoning=raw.get("confidence_reasoning", ""),
                    content=raw.get("content", ""),
                    source_memories=raw.get("source_memories", []),
                    relationships=raw.get("relationships", []),
                    strategy_that_found_it=raw.get(
                        "strategy_that_found_it", slice.strategy
                    ),
                )
                candidates.append(candidate)
                logger.debug(
                    "Parsed candidate: title=%s, operation=%s, confidence=%s",
                    candidate.title,
                    candidate.operation,
                    candidate.confidence,
                )
            except (KeyError, TypeError) as exc:
                logger.warning(
                    "Failed to parse Thinker candidate: %s — %s", raw, exc
                )

        return candidates

    def invoke_evaluator(
        self, candidate: CandidateInsight, role: str, stage: str | None = None,
    ) -> EvaluatorVerdict:
        """Invoke a single evaluator (skeptic/advocate/epistemologist/methodologist).

        Builds the evaluator prompt via template, invokes via AgentInvoker
        (no MCP config for evaluators), and parses the output into an
        EvaluatorVerdict.

        Args:
            candidate: The CandidateInsight to evaluate.
            role: One of 'skeptic', 'advocate', 'epistemologist', 'methodologist'.

        Returns:
            EvaluatorVerdict with role, verdict, and reasoning.
        """
        projection = asdict(candidate)
        if stage != "grounding_audit":
            projection.pop("revision", None)
        candidate_json_str = json.dumps(projection, default=str)
        source_memories_content = (
            f"Source memories: {candidate.source_memories}"
        )
        # When this run's Thinker was fed a context packet, the same texts are
        # cached — give the evaluator the cited sources' actual content so
        # "factual grounding" can be checked against the text, not UUIDs.
        sources_text = format_sources_for_evaluator(
            self._slice_memory_cache, candidate.source_memories
        )
        if sources_text:
            source_memories_content += "\n" + sources_text

        prompt = get_evaluator_prompt(
            role=role,
            candidate_json=candidate_json_str,
            source_memories_content=source_memories_content,
        )

        user_message = (
            f"Please evaluate this candidate insight as the {role}. "
            "Return your verdict as JSON with 'verdict', 'score', 'blocking_issues', "
            "'advisory_issues' and 'reasoning' fields."
        )

        logger.info("Invoking %s: role=%s, candidate=%s", stage or "evaluator", role, candidate.title)
        # Direct-API backends accept a JSON schema and return the verdict as a
        # tool call — immune to the quote-escaping failures of JSON-in-prose.
        # Agentic CLIs (Kiro's invoke() has a fixed signature) get no extra option.
        extra: dict = {} if self._role_has_tools(role) else {"schema": EVALUATOR_VERDICT_SCHEMA}
        result = self._invoke_metered(role,
            system_prompt=prompt,
            user_message=user_message,
            effort=self.resolver.spec_for(role).effort,
            stage=stage or f"evaluator:{role}",
            run_id=self._current_run_id,
            **extra,
        )

        safe_title = candidate.title.replace(" ", "_").replace("/", "_")[:40]
        self._capture(f"{(stage or 'evaluator').replace(':', '_')}_{role}_{safe_title}",
                      inputs={"system_prompt": prompt, "user_message": user_message}, output=result.get("output"))

        output = result["output"]
        if isinstance(output, list):
            output = output[0] if output else {}
        if not isinstance(output, dict):
            output = {}

        return EvaluatorVerdict(
            role=role,
            verdict=output.get("verdict", "REJECT"),
            reasoning=output.get("reasoning", ""),
            score=_as_int(output.get("score")),
            blocking_issues=_as_str_list(output.get("blocking_issues")),
            advisory_issues=_as_str_list(output.get("advisory_issues")),
        )

    def _ground_candidate(self, candidate: CandidateInsight) -> CandidateInsight:
        """Audit a candidate against its sources and repair it once before the panel.

        One graded call by the ``GROUNDING_AUDIT_ROLE`` seat's backend lists blocking
        and advisory defects; if there are any, one tool-less call by the Thinker's
        backend repairs the candidate (remove/correct unsupported details, narrow the
        claim, fix attribution, keep evidence). The panel then votes on the result.
        Skipped when the run has spent >= GROUNDING_SPEND_SHARE of its cap. Never
        raises except SpendCapExceeded: any other failure returns the candidate unchanged.
        """
        if self._spend.cap_usd and self._spend.total_usd >= GROUNDING_SPEND_SHARE * self._spend.cap_usd:
            logger.info("Grounding audit skipped for '%s': run spend $%.2f >= %.0f%% of cap",
                        candidate.title, self._spend.total_usd, 100 * GROUNDING_SPEND_SHARE)
            return candidate
        try:
            audit = self.invoke_evaluator(candidate, GROUNDING_AUDIT_ROLE, stage="grounding_audit")
        except SpendCapExceeded:
            raise
        except Exception:
            logger.warning("Grounding audit failed for '%s'; panel votes on the original", candidate.title,
                           exc_info=True)
            return candidate
        if not audit.blocking_issues and not audit.advisory_issues:
            logger.info("Grounding audit: '%s' clean (score %s)", candidate.title, audit.score)
            return candidate
        logger.info("Grounding audit: '%s' score %s, %d blocking, %d advisory — revising",
                    candidate.title, audit.score, len(audit.blocking_issues), len(audit.advisory_issues))
        try:
            return self._revise_candidate(candidate, audit)
        except SpendCapExceeded:
            raise
        except Exception:
            logger.warning("Revision failed for '%s'; panel votes on the original", candidate.title, exc_info=True)
            return candidate

    def _revise_candidate(self, candidate: CandidateInsight, audit: EvaluatorVerdict) -> CandidateInsight:
        """One tool-less repair call on the Thinker's backend; returns the revised candidate."""
        candidate_json = json.dumps(asdict(candidate), default=str)
        sources_text = format_sources_for_evaluator(self._slice_memory_cache, candidate.source_memories)
        user_message = build_revision_request(candidate_json, sources_text, audit.blocking_issues,
                                              audit.advisory_issues)
        result = self._invoke_metered("thinker",
            system_prompt=REVISION_PROMPT,
            user_message=user_message,
            tools=False,
            timeout=THINKER_TIMEOUT_S,
            effort="medium",
            stage="revision",
            run_id=self._current_run_id,
            **({} if self._role_has_tools("thinker") else {"schema": REVISION_SCHEMA}),
        )
        out = result.get("output") or {}
        if isinstance(out, list):
            out = out[0] if out else {}
        if not isinstance(out, dict) or "content" not in out:
            raise ValueError("revision returned no candidate")
        out = _unwrap_nested_candidate(out)
        # Sources may only shrink: the revision must not introduce new citations.
        original_sources = [str(m) for m in candidate.source_memories]
        kept = [m for m in _as_str_list(out.get("source_memories")) if m in original_sources] or original_sources
        # Relationships may be re-noted or dropped, never added: keep only targets the
        # Thinker originally proposed. schema_note is revised too — evaluators read both.
        original_targets = {str(r.get("target_id")) for r in candidate.relationships if isinstance(r, dict)}
        relationships = candidate.relationships
        if isinstance(out.get("relationships"), list):
            relationships = [r for r in out["relationships"]
                             if isinstance(r, dict) and str(r.get("target_id")) in original_targets]
        revised = CandidateInsight(
            **{**asdict(candidate),
               "title": str(out.get("title") or candidate.title)[:300],
               "content": str(out.get("content") or ""),
               "confidence": out.get("confidence") if out.get("confidence") in ("high", "medium", "low") else candidate.confidence,
               "confidence_reasoning": str(out.get("confidence_reasoning") or candidate.confidence_reasoning),
               "schema_note": str(out["schema_note"]) if isinstance(out.get("schema_note"), str) else candidate.schema_note,
               "relationships": relationships,
               "source_memories": kept,
               "revision": {"original_title": candidate.title, "audit_score": audit.score,
                            "blocking_issues": audit.blocking_issues, "advisory_issues": audit.advisory_issues}}
        )
        self._capture(f"revision_{candidate.title.replace(' ', '_').replace('/', '_')[:40]}",
                      inputs={"system_prompt": REVISION_PROMPT, "user_message": user_message}, output=out)
        logger.info("Revised '%s' -> '%s' (confidence %s, %d/%d sources kept)", candidate.title[:60],
                    revised.title[:60], revised.confidence, len(kept), len(original_sources))
        return revised

