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




# --------------------------------------------------------------------------- #
# Variant "standard" (experimental; docs/PLAN-panel-acceptance.md Phase 1/2)
# --------------------------------------------------------------------------- #
# The current prompts list criteria but state no decision rule, and demand a binary
# verdict with "no maybe". Replay experiments (2026-09-18) showed GPT-6 Astra then
# rejects ~95% of candidates in any seat — including carefully scoped ones — while
# conceding "the practical core is supported". This variant (a) states an explicit
# acceptance standard, (b) separates BLOCKING defects (fabrication, contradiction by
# the sources, misattribution, uncited claims) from ADVISORY ones (overreach, novelty,
# timing, depth), and (c) asks for a 0-100 score alongside the verdict so a "supported
# but overstated" judgement is expressible. The tally can then use scores and blocking
# flags across both model families instead of counting binary vetoes.

_STANDARD = """\
## Standard

The criteria below are lenses. This is the decision rule.

ACCEPT when all four hold:
(a) Grounded — every statement and quotation appears in the cited source text, attributed \
to the right author, channel and date.
(b) Scoped — the claim is stated at the scope the sources support. Three memories from one \
week support a claim about that week, not "every", "never" or "always". A specific, scoped \
observation about the user's current week qualifies on its own. Universality, a multi-period \
pattern and six-month durability are NOT required.
(c) Non-obvious and useful — it connects, names or flags something the user has not already \
articulated, and the user could act on it or would want to know it now.
(d) New — not a restatement of the sources or of any existing memory shown to you.

BLOCKING defects (any one means REJECT): a quotation or fact not present in the cited \
sources; a claim the cited sources contradict; wording credited to a person or organisation \
the source does not show produced it; a conclusion that rests on a memory that is not cited.

ADVISORY defects (lower the score, do not by themselves reject): a generalisation wider \
than the sources carry; partial overlap with an existing memory; unverified timing; thin \
WHY IT MATTERS. Name them; the user and the other evaluators will see them.

Do not reject a correctly scoped claim for being narrow. Judge the claim as written, not \
the claim it could have been. Your reasoning must name the letter(s) that fail, or state \
that all four hold."""

_VERDICT_V2 = """\
## Your verdict

Return a JSON object with these fields:
- "verdict": "ACCEPT" or "REJECT"
- "score": 0-100 — how far the claim, as written, meets the Standard (100 = all four letters \
hold cleanly; 60 = holds with advisory defects; below 40 = a blocking defect or (c)/(d) fail)
- "blocking_issues": list of strings, each one blocking defect found (empty if none)
- "advisory_issues": list of strings, each one advisory defect found (empty if none)
- "reasoning": name the letter(s) that fail or state that all four hold, then explain. \
Provide reasoning before stating your verdict.

Examples:
{{"verdict": "ACCEPT", "score": 78, "blocking_issues": [], "advisory_issues": ["'each said no action was needed' is true of two of the three notices, not all three"], "reasoning": "(a) the two quotations appear in the cited notices; (b) the claim is limited to the captured notices and says so; (c) the user has not connected the state letter to the silence of the Amazon channel; (d) the cited DLS memory states the 5-day rule but not that no captured notice mentions it. All four hold, with one advisory overstatement."}}
{{"verdict": "REJECT", "score": 22, "blocking_issues": ["the quoted phrase 'decision letter will follow' does not appear in any cited source"], "advisory_issues": [], "reasoning": "(a) fails: the central quotation is not in the sources. (b), (c), (d) not reached."}}"""


def get_evaluator_prompt_standard(role: str, candidate_json: str, source_memories_content: str) -> str:
    """Experimental variant of :func:`get_evaluator_prompt` with an explicit acceptance
    standard, blocking/advisory separation and a graded verdict. Same role criteria."""
    if role not in _ROLE_DESCRIPTIONS:
        raise ValueError(
            f"Invalid evaluator role: {role!r}. "
            f"Must be one of: skeptic, advocate, epistemologist, methodologist"
        )
    role_description = _ROLE_DESCRIPTIONS[role]
    criteria = _ROLE_CRITERIA[role]
    extra_context = _ADVOCATE_CONTEXT if role == "advocate" else ""
    return f"""\
You are evaluating a candidate insight. Your role is {role_description} You are one of four \
evaluators from two model families; your verdict, score and issue lists are combined with theirs.

## The candidate insight:
{candidate_json}

## The source memories it references:
{source_memories_content}
{extra_context}
{_STANDARD}

{criteria}

{_VERDICT_V2}"""
