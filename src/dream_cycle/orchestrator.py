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

from src.backends.resolver import VALID_ROLES, default_resolver
from src.models import (
    CandidateInsight,
    DreamCycleResult,
    EvaluatorVerdict,
    MemorySlice,
)
from src.prompts import get_evaluator_prompt, get_explorer_prompt, get_thinker_prompt
import src.dream_cycle_db as dream_cycle_db
from src.dream_cycle.consensus import tally_consensus
from src.dream_cycle.storage import store_accepted, check_duplicate
from src.dream_cycle.digest import generate_digest
from src.dream_cycle.feedback import build_feedback_injection

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
EVALUATOR_BACKOFF_S = 2.0


class DreamCycleOrchestrator:
    """Coordinates the four-agent dream cycle pipeline."""

    def __init__(self):
        self.resolver = default_resolver()
        self._capture_dir: str | None = None
        self._current_run_id: str | None = None

    def _invoker_for(self, role: str):
        """Resolve the Invoker for a role from the active backend profile."""
        return self.resolver.invoker_for(role)

    def _backend_provenance(self) -> dict:
        """Snapshot the active profile's (backend, model, effort) per role.

        Persisted on the run record so a run's candidates/verdicts stay auditable
        across a backend swap or the Mac Mini cutover (Kiro-Opus era vs
        Claude-Code-on-Bedrock era). Built here from the resolver (read-only)
        rather than on the Resolver itself, to keep this change out of the
        backends lane. See docs/MODEL-BACKENDS.md.
        """
        provenance: dict[str, dict] = {}
        for role in VALID_ROLES:
            spec = self.resolver.spec_for(role)
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
        """Build a DreamCycleResult for aborted/failed runs (all counts zero)."""
        return DreamCycleResult(
            run_id=run_id,
            run_type=run_type,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            candidates_generated=0,
            candidates_accepted=0,
            candidates_rejected=0,
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
            logger.error(
                "Database unreachable during pipeline execution (run_id=%s)", run_id
            )
            return self._aborted_result(run_id, run_type, started_at)
        except Exception:
            logger.exception(
                "Unexpected error during pipeline execution (run_id=%s)", run_id
            )
            completed_at = datetime.now(timezone.utc)
            try:
                dream_cycle_db.complete_run(
                    run_id,
                    stats=_ZERO_STATS,
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

        # Step 4: Invoke Explorer (with crash/timeout handling)
        try:
            slices = self.invoke_explorer(feedback, run_type, scope, stats)
        except (TimeoutError, RuntimeError):
            logger.error("Explorer agent crashed or timed out (run_id=%s)", run_id)
            dream_cycle_db.complete_run(run_id, stats=_ZERO_STATS, digest="")
            return self._aborted_result(run_id, run_type, started_at)
        logger.info("Explorer returned %d slices", len(slices))

        # Circuit breaker: empty slices → abort early
        if not slices:
            logger.warning("Circuit breaker: Explorer returned 0 slices, aborting early")
            dream_cycle_db.complete_run(run_id, stats=_ZERO_STATS, digest="")
            return self._aborted_result(run_id, run_type, started_at)

        # Step 6: Invoke Thinker for each slice, collect candidates
        all_candidates: list[CandidateInsight] = []
        for s in slices:
            try:
                candidates = self.invoke_thinker(s)
            except Exception:
                logger.error(
                    "Thinker failed for slice '%s', skipping", s.name, exc_info=True
                )
                continue
            # Session-start mode: limit to 1-2 candidates per slice
            if run_type == "session_start" and len(candidates) > 2:
                logger.info(
                    "Session-start mode: truncating %d candidates to 2 for slice '%s'",
                    len(candidates),
                    s.name,
                )
                candidates = candidates[:2]
            all_candidates.extend(candidates)
        logger.info("Total candidates from Thinker: %d", len(all_candidates))

        accepted: list[CandidateInsight] = []
        rejected: list[CandidateInsight] = []

        # Step 7: Consensus Panel — invoke 4 evaluators per candidate.
        # If an evaluator is unrecoverable after retries (the panel is genuinely
        # down), abort the run loudly — but keep the candidates already evaluated
        # so the run record reflects real persisted work (no zeroed-stats lie /
        # orphaned accepted memories). An infra failure never fabricates a verdict.
        panel_aborted = False
        for candidate in all_candidates:
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
            else:  # REJECTED — no DEFERRED branch
                dream_cycle_db.store_candidate(
                    run_id, candidate_dict, verdicts_dict, "REJECTED"
                )
                rejected.append(candidate)

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

        dream_cycle_db.complete_run(
            run_id,
            stats={
                "candidates_generated": len(all_candidates),
                "candidates_accepted": len(accepted),
                "candidates_rejected": len(rejected),
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

        user_message = (
            f"Run type: {run_type}. "
            f"Memory count: {stats['total_count']}. "
            f"Recent activity (7d): {stats['recent_activity']} new memories. "
            "Please assemble 0-5 memory slices using diverse strategies."
        )

        logger.info("Invoking Explorer agent (run_type=%s)", run_type)
        result = self._invoker_for("explorer").invoke(
            system_prompt=prompt,
            user_message=user_message,
            tools=True,
            timeout=600,
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

        user_message = json.dumps(payload)

        # Get the static Thinker prompt
        prompt = get_thinker_prompt()

        logger.info(
            "Invoking Thinker agent (slice=%s)",
            slice.name,
        )
        result = self._invoker_for("thinker").invoke(
            system_prompt=prompt,
            user_message=user_message,
            tools=True,
            timeout=600,
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
        self, candidate: CandidateInsight, role: str
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
        candidate_json_str = json.dumps(asdict(candidate), default=str)
        source_memories_content = (
            f"Source memories: {candidate.source_memories}"
        )

        prompt = get_evaluator_prompt(
            role=role,
            candidate_json=candidate_json_str,
            source_memories_content=source_memories_content,
        )

        user_message = (
            f"Please evaluate this candidate insight as the {role}. "
            "Return your verdict as JSON with 'verdict' and 'reasoning' fields."
        )

        logger.info("Invoking evaluator: role=%s, candidate=%s", role, candidate.title)
        result = self._invoker_for(role).invoke(
            system_prompt=prompt,
            user_message=user_message,
            effort=self.resolver.spec_for(role).effort,
            stage=f"evaluator:{role}",
            run_id=self._current_run_id,
        )

        safe_title = candidate.title.replace(" ", "_").replace("/", "_")[:40]
        self._capture(f"evaluator_{role}_{safe_title}", inputs={"system_prompt": prompt, "user_message": user_message}, output=result.get("output"))

        output = result["output"]
        if isinstance(output, list):
            output = output[0] if output else {}

        return EvaluatorVerdict(
            role=role,
            verdict=output.get("verdict", "REJECT"),
            reasoning=output.get("reasoning", ""),
        )

