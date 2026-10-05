"""Agents Evaluator responsibilities."""
from __future__ import annotations
import json
import logging
import time
from dataclasses import asdict
from src.models import CandidateInsight, EvaluatorVerdict
from src.prompts import get_evaluator_prompt
from src.dream_cycle.packet import EVALUATOR_VERDICT_SCHEMA, format_sources_for_evaluator
from src.dream_cycle.budget import *
from src.dream_cycle.parsing import _as_int, _as_str_list
logger = logging.getLogger(__name__)

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

    if stage == "grounding_audit":
        from src.prompts.grounding import get_grounding_prompt
        prompt = get_grounding_prompt(candidate_json_str, source_memories_content)
    else:
        prompt = get_evaluator_prompt(role=role, candidate_json=candidate_json_str,
                                      source_memories_content=source_memories_content)

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
        verdict=output.get("verdict"),
        reasoning=output.get("reasoning", ""),
        score=_as_int(output.get("score")),
        blocking_issues=_as_str_list(output.get("blocking_issues")),
        advisory_issues=_as_str_list(output.get("advisory_issues")),
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
