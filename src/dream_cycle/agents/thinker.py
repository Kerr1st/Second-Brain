"""Agents Thinker responsibilities."""
from __future__ import annotations
import json
import logging
from src.models import CandidateInsight, MemorySlice
from src.prompts import get_thinker_prompt
from src.dream_cycle.packet import (
    TOOLED_NOTE, TOOLLESS_NOTE, assign_refs, build_packet, load_memories_by_ids, resolve_candidate_refs,
)
from src.dream_cycle.storage import canonical_uuid, is_active_memory
from src.dream_cycle.budget import *
logger = logging.getLogger(__name__)

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
    loaded = load_memories_by_ids(slice.memory_ids)
    # Only active memories may be cited, targeted or related to (the rule
    # authorize_candidate and store_accepted enforce), so a superseded, expired
    # or archived memory the Explorer picked is never shown to the Thinker: a
    # candidate built on it can only be blocked. The slice itself and its
    # attention-ledger row are unchanged.
    memories = [m for m in loaded if is_active_memory(m)]
    excluded = {canonical_uuid(m.get("id")) for m in loaded} - {canonical_uuid(m.get("id")) for m in memories}
    if excluded:
        logger.info("Thinker packet: excluded %d non-active memor%s from slice %s",
                    len(excluded), "y" if len(excluded) == 1 else "ies", slice.name)
        logger.debug("Thinker packet: non-active ids excluded: %s", sorted(excluded))
        keep = [canonical_uuid(mid) not in excluded for mid in slice.memory_ids]
        payload["memory_slice"]["memory_ids"] = [mid for mid, k in zip(slice.memory_ids, keep) if k]
        if len(slice.memory_titles) == len(slice.memory_ids):
            payload["memory_slice"]["memory_titles"] = [t for t, k in zip(slice.memory_titles, keep) if k]
    for memory in memories:
        self._slice_memory_cache[str(memory.get("id"))] = memory
    # Each packet entry carries a short ref ("m1".."mN") the Thinker cites
    # instead of copying 36-char UUIDs (I3, 2026-10-16: a corrupted copy got two
    # valid candidates blocked). ref_map resolves them back before authorization.
    payload["memory_slice"]["memories"], ref_map = assign_refs(build_packet(memories))
    payload["note"] = TOOLED_NOTE if thinker_has_tools else TOOLLESS_NOTE
    if len(loaded) < len(slice.memory_ids):
        logger.warning(
            "Thinker packet: loaded %d of %d slice memories for %s",
            len(loaded), len(slice.memory_ids), slice.name,
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
        # Refs -> UUIDs. Unknown refs and non-packet/corrupted UUIDs pass through
        # unchanged, so authorize_candidate still blocks them.
        raw = resolve_candidate_refs(raw, ref_map)
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
