"""Stages Grounding responsibilities."""
from __future__ import annotations
import json
import logging
from dataclasses import asdict
from src.backends.cost import SpendCapExceeded
from src.models import CandidateInsight, EvaluatorVerdict
from src.prompts.revision import REVISION_PROMPT, REVISION_SCHEMA, build_revision_request
from src.dream_cycle.packet import format_sources_for_evaluator
from src.dream_cycle.budget import *
from src.dream_cycle.budget import GROUNDING_AUDIT_ROLE  # "grounding_auditor"
from src.dream_cycle.parsing import _as_str_list, _unwrap_nested_candidate
logger = logging.getLogger(__name__)

#: Bound on the raw reply kept in a failed-revision capture.
FAILED_REVISION_CAPTURE_CHARS = 4000


class RevisionNoCandidate(ValueError):
    """The revision reply held no candidate; the panel votes on the original.

    An expected model outcome (seen 10-09 and 10-26), not a code fault: it is
    logged as one WARNING line and the reply is captured for diagnosis.
    """

def _ground_candidate(self, candidate: CandidateInsight) -> CandidateInsight:
    """Audit a candidate against its sources and repair it once before the panel.

    One graded, metered call on the dedicated ``grounding_auditor`` role (routed
    like the skeptic seat in every committed profile) lists blocking
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
    except RevisionNoCandidate as exc:
        logger.warning("Revision failed for '%s' (%s); panel votes on the original", candidate.title, exc)
        return candidate
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
    capture_name = f"revision_{candidate.title.replace(' ', '_').replace('/', '_')[:40]}"
    out = result.get("output") or {}
    if isinstance(out, list):
        out = out[0] if out else {}
    if not isinstance(out, dict) or "content" not in out:
        shape = (f"output {type(out).__name__}"
                 + (f", keys {sorted(map(str, out))}" if isinstance(out, dict) else ""))
        raw = result.get("raw")
        raw = raw if isinstance(raw, str) else json.dumps(result.get("output"), default=str)
        self._capture(f"{capture_name}_failed",
                      inputs={"system_prompt": REVISION_PROMPT, "user_message": user_message},
                      output={"failed": True, "reason": "revision returned no candidate", "shape": shape,
                              "stop_reason": result.get("stop_reason"), "raw_chars": len(raw),
                              "raw": raw[:FAILED_REVISION_CAPTURE_CHARS]})
        raise RevisionNoCandidate(f"revision returned no candidate; {shape}")
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
    self._capture(capture_name,
                  inputs={"system_prompt": REVISION_PROMPT, "user_message": user_message}, output=out)
    logger.info("Revised '%s' -> '%s' (confidence %s, %d/%d sources kept)", candidate.title[:60],
                revised.title[:60], revised.confidence, len(kept), len(original_sources))
    return revised



def audit_and_revise(runner, candidate: CandidateInsight) -> CandidateInsight:
    """Supported grounding service for replay and the live coordinator."""
    return _ground_candidate(runner, candidate)


class GroundingService:
    """Supported read-only audit/revision/panel service for offline replays.

    Owns a shared metered runner but never persists a memory or ledger row.
    """
    def __init__(self, resolver, tracker, *, run_id=None):
        from src.dream_cycle.orchestrator import DreamCycleOrchestrator
        self._runner = DreamCycleOrchestrator()
        self._runner.resolver = resolver
        self._runner._spend = tracker
        self._runner._current_run_id = run_id
        self.resolver = resolver
        self.tracker = tracker

    def audit_and_revise(self, candidate, sources):
        self._runner._slice_memory_cache = {str(source["id"]): source for source in sources}
        return audit_and_revise(self._runner, candidate)

    def evaluate(self, candidate, role):
        return self._runner.invoke_evaluator(candidate, role)
