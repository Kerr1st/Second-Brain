"""Dream Cycle Orchestrator — four-agent pipeline coordination.

Coordinates Explorer → Thinker → Consensus Panel pipeline.
Manages run lifecycle, feedback injection, inter-agent data flow,
consensus tallying, deduplication, memory storage, and digest generation.

Requirements: 1, 3, 9 (this file — task 6.1 skeleton)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone

import psycopg2

from src.agent_invoker import AgentInvoker
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

DEFAULT_MCP_CONFIG = os.path.expanduser("~/.kiro/settings/mcp.json")


class DreamCycleOrchestrator:
    """Coordinates the four-agent dream cycle pipeline."""

    def __init__(self, mcp_config: str | None = None):
        self.invoker = AgentInvoker()
        self.mcp_config = mcp_config or DEFAULT_MCP_CONFIG

    def run(self, run_type: str, scope: dict | None = None) -> DreamCycleResult:
        """Execute a full dream cycle pipeline.

        Args:
            run_type: 'scheduled', 'post_learn', 'session_start', 'user_triggered'
            scope: Optional scoping for non-scheduled runs.

        Returns:
            DreamCycleResult with stats and digest path.
        """
        started_at = datetime.now(timezone.utc)
        logger.info("Starting dream cycle: run_type=%s, scope=%s", run_type, scope)

        # Session-start frequency cap: check before creating a run record
        if run_type == "session_start" and not dream_cycle_db.should_run_briefing():
            logger.info("Session-start briefing skipped: frequency cap not met")
            completed_at = datetime.now(timezone.utc)
            return DreamCycleResult(
                run_id="",
                run_type=run_type,
                started_at=started_at,
                completed_at=completed_at,
                candidates_generated=0,
                candidates_accepted=0,
                candidates_deferred=0,
                candidates_rejected=0,
                digest_path=None,
                aborted_early=True,
            )

        # Step 1: Create run record
        try:
            run_id = dream_cycle_db.create_run(run_type)
        except psycopg2.OperationalError:
            logger.error("Database unreachable when creating run record")
            completed_at = datetime.now(timezone.utc)
            return DreamCycleResult(
                run_id="",
                run_type=run_type,
                started_at=started_at,
                completed_at=completed_at,
                candidates_generated=0,
                candidates_accepted=0,
                candidates_deferred=0,
                candidates_rejected=0,
                digest_path=None,
                aborted_early=True,
            )
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
            completed_at = datetime.now(timezone.utc)
            return DreamCycleResult(
                run_id=run_id,
                run_type=run_type,
                started_at=started_at,
                completed_at=completed_at,
                candidates_generated=0,
                candidates_accepted=0,
                candidates_deferred=0,
                candidates_rejected=0,
                digest_path=None,
                aborted_early=True,
            )
        except Exception:
            logger.exception(
                "Unexpected error during pipeline execution (run_id=%s)", run_id
            )
            completed_at = datetime.now(timezone.utc)
            try:
                dream_cycle_db.complete_run(
                    run_id,
                    stats={
                        "candidates_generated": 0,
                        "candidates_accepted": 0,
                        "candidates_deferred": 0,
                        "candidates_rejected": 0,
                    },
                    digest="",
                )
            except Exception:
                logger.error("Failed to complete run record after unexpected error")
            return DreamCycleResult(
                run_id=run_id,
                run_type=run_type,
                started_at=started_at,
                completed_at=completed_at,
                candidates_generated=0,
                candidates_accepted=0,
                candidates_deferred=0,
                candidates_rejected=0,
                digest_path=None,
                aborted_early=True,
            )

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
            slices = self.invoke_explorer(feedback, run_type, scope)
        except (TimeoutError, RuntimeError):
            logger.error("Explorer agent crashed or timed out (run_id=%s)", run_id)
            completed_at = datetime.now(timezone.utc)
            dream_cycle_db.complete_run(
                run_id,
                stats={
                    "candidates_generated": 0,
                    "candidates_accepted": 0,
                    "candidates_deferred": 0,
                    "candidates_rejected": 0,
                },
                digest="",
            )
            return DreamCycleResult(
                run_id=run_id,
                run_type=run_type,
                started_at=started_at,
                completed_at=completed_at,
                candidates_generated=0,
                candidates_accepted=0,
                candidates_deferred=0,
                candidates_rejected=0,
                digest_path=None,
                aborted_early=True,
            )
        logger.info("Explorer returned %d slices", len(slices))

        # Circuit breaker: empty slices → abort early
        if not slices:
            logger.warning("Circuit breaker: Explorer returned 0 slices, aborting early")
            completed_at = datetime.now(timezone.utc)
            dream_cycle_db.complete_run(
                run_id,
                stats={
                    "candidates_generated": 0,
                    "candidates_accepted": 0,
                    "candidates_deferred": 0,
                    "candidates_rejected": 0,
                },
                digest="",
            )
            return DreamCycleResult(
                run_id=run_id,
                run_type=run_type,
                started_at=started_at,
                completed_at=completed_at,
                candidates_generated=0,
                candidates_accepted=0,
                candidates_deferred=0,
                candidates_rejected=0,
                digest_path=None,
                aborted_early=True,
            )

        # Step 5: Get deferred candidates from previous cycle (if any)
        previous_run_id = dream_cycle_db.get_previous_run_id(run_type)
        deferred = (
            dream_cycle_db.get_deferred_candidates(previous_run_id)
            if previous_run_id
            else []
        )
        logger.info(
            "Deferred candidates from previous run: %d", len(deferred)
        )

        # Step 6: Invoke Thinker for each slice, collect candidates
        all_candidates: list[CandidateInsight] = []
        for s in slices:
            try:
                candidates = self.invoke_thinker(s, deferred)
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
        deferred_new: list[CandidateInsight] = []
        rejected: list[CandidateInsight] = []

        # Step 7: Consensus Panel for each candidate
        for candidate in all_candidates:
            verdict_a = self._invoke_evaluator_safe(candidate, "skeptic")
            verdict_b = self._invoke_evaluator_safe(candidate, "advocate")
            verdict_c = self._invoke_evaluator_safe(candidate, "epistemologist")

            evaluator_verdicts = [verdict_a, verdict_b, verdict_c]
            final = tally_consensus(evaluator_verdicts)

            # Build verdicts dict for store_candidate
            verdicts_dict = {
                "evaluator_a_verdict": verdict_a.verdict,
                "evaluator_a_reasoning": verdict_a.reasoning,
                "evaluator_b_verdict": verdict_b.verdict,
                "evaluator_b_reasoning": verdict_b.reasoning,
                "evaluator_c_verdict": verdict_c.verdict,
                "evaluator_c_reasoning": verdict_c.reasoning,
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

            elif final == "DEFERRED":
                # Two-strike rule: if already deferred once, reject instead
                if self._is_second_deferral(candidate, deferred):
                    candidate_id = dream_cycle_db.store_candidate(
                        run_id, candidate_dict, verdicts_dict, "REJECTED"
                    )
                    dream_cycle_db.mark_deferred_twice_rejected(candidate_id)
                    rejected.append(candidate)
                    logger.info(
                        "Two-strike rule: candidate '%s' deferred twice, now REJECTED",
                        candidate.title,
                    )
                else:
                    dream_cycle_db.store_candidate(
                        run_id, candidate_dict, verdicts_dict, "DEFERRED"
                    )
                    deferred_new.append(candidate)

            else:  # REJECTED
                dream_cycle_db.store_candidate(
                    run_id, candidate_dict, verdicts_dict, "REJECTED"
                )
                rejected.append(candidate)

            logger.info(
                "Candidate '%s': %s", candidate.title, final
            )

        # Step 8: Generate digest and complete run
        completed_at = datetime.now(timezone.utc)
        digest = generate_digest(run_id, accepted, deferred_new, rejected)

        dream_cycle_db.complete_run(
            run_id,
            stats={
                "candidates_generated": len(all_candidates),
                "candidates_accepted": len(accepted),
                "candidates_deferred": len(deferred_new),
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
            candidates_deferred=len(deferred_new),
            candidates_rejected=len(rejected),
            digest_path=digest,
            aborted_early=False,
        )


    def _invoke_evaluator_safe(self, candidate: CandidateInsight, role: str) -> EvaluatorVerdict:
        """Invoke evaluator with crash/timeout handling.

        On TimeoutError or RuntimeError, returns a default REJECT verdict
        instead of propagating the exception.

        Args:
            candidate: The CandidateInsight to evaluate.
            role: One of 'skeptic', 'advocate', 'epistemologist'.

        Returns:
            EvaluatorVerdict — real verdict on success, default REJECT on failure.
        """
        try:
            return self.invoke_evaluator(candidate, role)
        except (TimeoutError, RuntimeError):
            logger.error(
                "Evaluator '%s' crashed/timed out for '%s', treating as REJECT",
                role, candidate.title,
            )
            return EvaluatorVerdict(
                role=role,
                verdict="REJECT",
                reasoning="Evaluator crashed or timed out",
            )

    def _is_second_deferral(self, candidate: CandidateInsight, deferred_from_previous: list[dict]) -> bool:
        """Check if a candidate was already deferred in the previous cycle.

        Compares the candidate's title against the titles of deferred candidates
        from the previous run. A title match indicates this is the second
        consecutive deferral, triggering the two-strike rule (Requirement 10.4).

        Args:
            candidate: The current candidate that received a DEFERRED verdict.
            deferred_from_previous: Deferred candidates from the previous cycle,
                each a dict with candidate_json containing the original candidate data.

        Returns:
            True if the candidate title matches a previously deferred candidate.
        """
        for prev in deferred_from_previous:
            prev_json = prev.get("candidate_json") or {}
            if isinstance(prev_json, str):
                try:
                    prev_json = json.loads(prev_json)
                except (json.JSONDecodeError, ValueError):
                    continue
            if prev_json.get("title") == candidate.title:
                return True
        return False



    def invoke_explorer(
        self, feedback: str, run_type: str, scope: dict | None = None
    ) -> list[MemorySlice]:
        """Invoke Explorer agent and parse memory slices.

        Builds the Explorer prompt, invokes via AgentInvoker with MCP config,
        and parses the JSON output into MemorySlice objects.

        Args:
            feedback: Formatted feedback injection text (may be empty).
            run_type: One of scheduled, post_learn, session_start, user_triggered.
            scope: Optional scoping dict for non-scheduled runs.

        Returns:
            List of MemorySlice objects (may be empty for circuit breaker).
        """
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
        )

        user_message = (
            f"Run type: {run_type}. "
            f"Memory count: {stats['total_count']}. "
            f"Recent activity (7d): {stats['recent_activity']} new memories. "
            "Please assemble 0-5 memory slices using diverse strategies."
        )

        logger.info("Invoking Explorer agent (run_type=%s)", run_type)
        result = self.invoker.invoke(
            system_prompt=prompt,
            user_message=user_message,
            mcp_config=self.mcp_config,
        )

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

        return slices

    def invoke_thinker(
        self, slice: MemorySlice, deferred: list[dict]
    ) -> list[CandidateInsight]:
        """Invoke Thinker agent with a memory slice + deferred candidates.

        Builds a JSON user message containing the memory slice data and any
        deferred candidates with their dissenting evaluator's objection, then
        invokes the Thinker via AgentInvoker with MCP config.

        Args:
            slice: A MemorySlice assembled by the Explorer.
            deferred: Deferred candidates from the previous cycle, each a dict
                with candidate_json and evaluator verdict/reasoning fields.

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

        # Add deferred candidates with dissenting objections
        if deferred:
            deferred_entries = []
            for d in deferred:
                candidate_json = d.get("candidate_json") or {}
                # If candidate_json is a string, parse it
                if isinstance(candidate_json, str):
                    try:
                        candidate_json = json.loads(candidate_json)
                    except (json.JSONDecodeError, ValueError):
                        candidate_json = {}

                # Find the dissenting evaluator (the one who voted REJECT)
                dissenting_objection = ""
                evaluator_roles = {
                    "evaluator_a": "Skeptic",
                    "evaluator_b": "User Advocate",
                    "evaluator_c": "Epistemologist",
                }
                for key_prefix, role_name in evaluator_roles.items():
                    verdict = d.get(f"{key_prefix}_verdict", "")
                    reasoning = d.get(f"{key_prefix}_reasoning", "")
                    if verdict == "REJECT" and reasoning:
                        dissenting_objection = (
                            f"{role_name}: {reasoning}"
                        )
                        break

                deferred_entries.append({
                    "candidate": candidate_json,
                    "dissenting_objection": dissenting_objection,
                })

            payload["deferred_candidates"] = deferred_entries

        user_message = json.dumps(payload)

        # Get the static Thinker prompt
        prompt = get_thinker_prompt()

        logger.info(
            "Invoking Thinker agent (slice=%s, deferred=%d)",
            slice.name,
            len(deferred),
        )
        result = self.invoker.invoke(
            system_prompt=prompt,
            user_message=user_message,
            mcp_config=self.mcp_config,
        )

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
        """Invoke a single evaluator (skeptic/advocate/epistemologist).

        Builds the evaluator prompt via template, invokes via AgentInvoker
        (no MCP config for evaluators), and parses the output into an
        EvaluatorVerdict.

        Args:
            candidate: The CandidateInsight to evaluate.
            role: One of 'skeptic', 'advocate', 'epistemologist'.

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
        result = self.invoker.invoke(
            system_prompt=prompt,
            user_message=user_message,
        )

        output = result["output"]
        if isinstance(output, list):
            output = output[0] if output else {}

        return EvaluatorVerdict(
            role=role,
            verdict=output.get("verdict", "REJECT"),
            reasoning=output.get("reasoning", ""),
        )

