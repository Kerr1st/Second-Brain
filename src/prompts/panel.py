"""Consensus Panel evaluator prompt templates.

Four independent evaluators (Skeptic, User Advocate, Epistemologist,
Methodologist) each assess every candidate insight.  Prompts accept the
evaluator role, candidate JSON, and source memories content as interpolation
variables.
"""

_SKEPTIC_CRITERIA = """\
## Evaluate on these criteria:

1. FACTUAL GROUNDING — Does the insight accurately represent what the source \
memories actually say? Or does it misquote, exaggerate, or hallucinate \
connections that aren't in the text?

2. NON-OBVIOUSNESS — Would the user likely already know this? The bar: would \
this make the user stop and think?

3. LOGICAL VALIDITY — Does the reasoning hold? If it claims a contradiction, \
are the positions actually incompatible? If a pattern, is 2 occurrences \
really a pattern or coincidence?

4. ACTIONABILITY — Can the user do something with this insight?

For UPDATE/SUPERSEDE operations, apply additional scrutiny:
5. PRESERVATION — Does the update preserve the valuable parts of the original \
memory? Or does it discard hard-won knowledge for a superficially newer \
observation?
6. EVIDENCE DELTA — Is the new evidence strong enough to justify changing an \
established memory? A single new data point shouldn't override a principle \
derived from 10 data points."""

_ADVOCATE_CRITERIA = """\
## Evaluate on these criteria:

1. RELEVANCE — Does this connect to something the user is actively working on?

2. TIMING — Is this the right moment for this insight?

3. SIGNAL-TO-NOISE — If the user receives 3-5 insights per week, is this one \
worth a slot? Would the user be glad they saw it, or think "so what?"

4. DEPTH — Does it explain WHY, not just WHAT? Enough context to evaluate \
without reading all source memories?"""

_EPISTEMOLOGIST_CRITERIA = """\
## Evaluate on these criteria:

1. EVIDENCE SUFFICIENCY — How many independent memories support this? A claim \
based on 1 memory is an observation. Based on 3+ from different periods \
is a pattern.

2. FALSIFIABILITY — Could this be wrong? What would disprove it? Unfalsifiable \
claims are too vague to be useful.

3. NOVELTY — Does this create new knowledge or just reorganize existing? \
Check the schema_operation field: "accommodation" (schema change) is \
inherently higher novelty than "assimilation" (schema confirmation). \
An accommodation insight that updates an existing principle with new \
evidence creates genuine new knowledge. An assimilation insight that \
confirms what's already known is reorganization — lower value unless \
the confirmation itself is surprising.

4. DURABILITY — Will this still be relevant in 6 months?

5. RETRIEVABILITY — Will it be findable when needed? Clear "Questions this \
answers"? Keywords specific enough for BM25, semantics clear for vector?"""

_METHODOLOGIST_CRITERIA = """\
## Evaluate on these criteria:

1. INTERNAL CONSISTENCY — Do the insight's claims, evidence citations, and \
conclusions form a logically coherent argument? Are there self-contradictions \
between what the EVIDENCE section says and what the WHAT section claims?

2. SOURCE INDEPENDENCE — Do the cited source memories represent genuinely \
independent data points? Or are they derivatives of the same original source \
(e.g., a memory and its chunk, or two memories from the same conversation)?

3. REASONING STRUCTURE — Does the insight follow the depth framework \
(WHAT, EVIDENCE, WHY IT MATTERS) with each section substantively contributing? \
Or does WHY IT MATTERS merely restate WHAT in different words?

4. REPRODUCIBILITY — Would another agent examining the same source memories \
plausibly arrive at the same or a compatible conclusion? Or does the insight \
depend on unstated assumptions or creative leaps not grounded in the sources?

For UPDATE/SUPERSEDE operations, apply additional scrutiny:
5. TRACEABLE REASONING — Does the proposed change follow from the cited \
evidence through a traceable chain of reasoning? Or does it introduce \
conclusions that require unstated assumptions not present in the sources?"""


_ROLE_DESCRIPTIONS = {
    "skeptic": "THE SKEPTIC — you look for reasons the insight might be wrong, shallow, or misleading.",
    "advocate": "THE USER ADVOCATE — you evaluate whether this insight is genuinely valuable to the human who owns this knowledge base.",
    "epistemologist": "THE EPISTEMOLOGIST — you evaluate the quality of the knowledge claim itself.",
    "methodologist": "THE METHODOLOGIST — you evaluate the methodological rigor of the insight: internal consistency, source independence, reasoning structure, and reproducibility.",
}

_ROLE_CRITERIA = {
    "skeptic": _SKEPTIC_CRITERIA,
    "advocate": _ADVOCATE_CRITERIA,
    "epistemologist": _EPISTEMOLOGIST_CRITERIA,
    "methodologist": _METHODOLOGIST_CRITERIA,
}


_ADVOCATE_CONTEXT = """\

## Context about the user:
Developer/technologist who values research-grounded decisions, understanding \
WHY before building WHAT, proactive collaboration, documentation quality, \
and strategic thinking across projects."""


def get_evaluator_prompt(
    role: str,
    candidate_json: str,
    source_memories_content: str,
) -> str:
    """Build evaluator prompt for the given role.

    Args:
        role: One of "skeptic", "advocate", "epistemologist", "methodologist".
        candidate_json: JSON string of the candidate insight.
        source_memories_content: Formatted content of the source memories
            referenced by the candidate.

    Returns:
        Complete evaluator system prompt string.

    Raises:
        ValueError: If role is not one of the four valid roles.
    """
    if role not in _ROLE_DESCRIPTIONS:
        raise ValueError(
            f"Invalid evaluator role: {role!r}. "
            f"Must be one of: skeptic, advocate, epistemologist, methodologist"
        )

    role_description = _ROLE_DESCRIPTIONS[role]
    criteria = _ROLE_CRITERIA[role]

    # Advocate gets extra user context section
    extra_context = _ADVOCATE_CONTEXT if role == "advocate" else ""

    prompt = f"""\
You are evaluating a candidate insight. Your role is {role_description}

## The candidate insight:
{candidate_json}

## The source memories it references:
{source_memories_content}
{extra_context}
{criteria}

## Your verdict

Return a JSON object with exactly two fields:
- "verdict": "ACCEPT" or "REJECT" (no "maybe")
- "reasoning": Your reasoning (must be non-empty). Provide reasoning before stating your verdict.

Example: {{"verdict": "ACCEPT", "reasoning": "The insight is well-grounded in 4 independent memories..."}}"""

    return prompt
