"""Consensus Panel evaluator prompt templates.

Four independent evaluators (Skeptic, User Advocate, Epistemologist,
Methodologist) each assess every candidate insight against one shared
acceptance Standard — (a) grounded, (b) scoped, (c) non-obvious and useful,
(d) new — read through their own role's criteria.  Defects are separated into
BLOCKING (a fabricated quotation or fact, a claim the sources contradict,
misattribution, an uncited conclusion: any one rejects) and ADVISORY
(overreach, overlap, timing, depth: they lower the score but do not by
themselves reject), and the verdict is graded: ACCEPT/REJECT plus a 0-100
score and the two issue lists, so "supported but overstated" is expressible.
Prompts accept the evaluator role, candidate JSON, and source memories
content as interpolation variables.
"""

_SKEPTIC_CRITERIA = """\
## Evaluate through these criteria (each tests a letter of the Standard):

1. FACTUAL GROUNDING — Does the insight accurately represent what the source \
memories actually say? Or does it misquote, exaggerate, or hallucinate \
connections that aren't in the text? Check every quotation and every \
attribution against the source text: is that wording really there, and does \
the source show it came from the person, organisation or channel the insight \
credits — or is it a summary of them? A connection between two memories is \
not "in the text" of either; that is fine. The facts the connection rests on \
must be. Misquotation, invented detail or unestablished attribution fails (a).

2. NON-OBVIOUSNESS — Would the user already have this in mind? The user lived \
this week, so the individual episodes are known to them; ask instead whether \
the connection, the status or the naming is something they have not put \
together. Two channels turning out to concern the same matter, or a \
commitment still open, can pass; a paraphrase of one episode cannot.

3. LOGICAL VALIDITY — Does the reasoning hold? If it claims a contradiction, \
are the positions actually incompatible? If a pattern, is 2 occurrences \
really a pattern or coincidence? If it claims a pattern or a rule, do the \
cited memories instantiate it, and does the word used ("pattern", "every", \
"never", "always") match their number and spread? Two occurrences stated as \
two occurrences are valid; two occurrences stated as a rule are overreach. \
Fail (b) for the overreach, not for the narrowness.

4. ACTIONABILITY — Can the user do something with this insight?

For UPDATE/SUPERSEDE operations, apply additional scrutiny:
5. PRESERVATION — Does the update preserve the valuable parts of the original \
memory? Or does it discard hard-won knowledge for a superficially newer \
observation?
6. EVIDENCE DELTA — Is the new evidence strong enough to justify changing an \
established memory? A single new data point shouldn't override a principle \
derived from 10 data points."""

_ADVOCATE_CRITERIA = """\
## Evaluate through these criteria (each tests a letter of the Standard):

1. RELEVANCE — Does this connect to something the user is actively working on?

2. TIMING — Is this the right moment for this insight?

3. SIGNAL-TO-NOISE — If the user receives 3-5 insights per week, is this one \
worth a slot? Would the user be glad they saw it, or think "so what?"

4. DEPTH — Does it explain WHY, not just WHAT? Enough context to evaluate \
without reading all source memories? For an observation about this week, one \
sentence naming what the user would do or decide differently is enough depth."""

_EPISTEMOLOGIST_CRITERIA = """\
## Evaluate through these criteria (each tests a letter of the Standard):

1. EVIDENCE AND SCOPE — How many independent memories support this, and does \
the wording match? These are labels for the scope a claim may take, not a bar \
it must clear: 1 memory = a single observation (acceptable when the insight \
says so); 2-3 memories, or several from one period = an emerging observation \
about that period; 3+ independent memories from different periods = a \
pattern. Fail (b) only when the claim's wording takes a wider label than its \
evidence earns.

2. CHECKABILITY — Is the claim specific enough to be shown wrong? For a general \
claim: what evidence would disprove it? For an observation about particular \
episodes: could someone holding the cited sources confirm or refute it? A \
claim so vague that no evidence could contradict it fails; a specific, dated \
observation passes.

3. NOVELTY — Does this create new knowledge or just reorganize existing? \
Check the schema_operation field: "accommodation" (schema change) is \
inherently higher novelty than "assimilation" (schema confirmation). \
An accommodation insight that updates an existing principle with new \
evidence creates genuine new knowledge. An assimilation insight that \
confirms what's already known is reorganization — lower value unless \
the confirmation itself is surprising. An observation that connects episodes \
the user has not connected is new knowledge even when it changes no \
principle; do not downgrade it for being labelled "assimilation".

4. DURABILITY OR TIMELINESS — Will this be relevant when the user needs it? For \
a principle, that means months. For an observation about this week, that \
means now — a time-bound insight is not defective for being time-bound. Say \
which kind it is; do not fail an observation for being about now.

5. RETRIEVABILITY — Will it be findable when needed? Clear "Questions this \
answers"? Keywords specific enough for BM25, semantics clear for vector?"""

_METHODOLOGIST_CRITERIA = """\
## Evaluate through these criteria (each tests a letter of the Standard):

1. INTERNAL CONSISTENCY — Do the insight's claims, evidence citations, and \
conclusions form a logically coherent argument? Are there self-contradictions \
between what the EVIDENCE section says and what the WHAT section claims?

2. SOURCE INDEPENDENCE — Do the cited source memories represent independent \
data points to the extent the claim needs? A claim about a pattern across \
projects or periods needs sources from different contexts; a claim about one \
thread, one case or one week needs only the sources from that thread, case or \
week — two memories from the same conversation are the right evidence for a \
claim about that conversation. Fail (b) when the claim's breadth outruns the \
independence of its sources, or when the "several sources" are a memory and \
its own chunk counted twice.

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
    "skeptic": (
        "THE SKEPTIC — you look for reasons the insight might be wrong, shallow, or misleading. "
        "You are hard to fool, not hard to satisfy: when a claim survives your checks at the "
        "scope it is actually stated, accept it."
    ),
    "advocate": "THE USER ADVOCATE — you evaluate whether this insight is genuinely valuable to the human who owns this knowledge base.",
    "epistemologist": (
        "THE EPISTEMOLOGIST — you evaluate the quality of the knowledge claim itself. "
        "Does it claim exactly what its evidence supports, no more and no less?"
    ),
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


# --------------------------------------------------------------------------- #
# The acceptance Standard and the graded verdict format shared by all four seats
# (docs/PLAN-panel-acceptance.md, Decisions 2026-09-18). The role criteria above
# are lenses onto the Standard; the Standard is the decision rule.
# --------------------------------------------------------------------------- #

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
{"verdict": "ACCEPT", "score": 78, "blocking_issues": [], "advisory_issues": ["'each said no action was needed' is true of two of the three notices, not all three"], "reasoning": "(a) the two quotations appear in the cited notices; (b) the claim is limited to the captured notices and says so; (c) the user has not connected the state letter to the silence of the Amazon channel; (d) the cited DLS memory states the 5-day rule but not that no captured notice mentions it. All four hold, with one advisory overstatement."}
{"verdict": "REJECT", "score": 22, "blocking_issues": ["the quoted phrase 'decision letter will follow' does not appear in any cited source"], "advisory_issues": [], "reasoning": "(a) fails: the central quotation is not in the sources. (b), (c), (d) not reached."}"""


def get_evaluator_prompt(
    role: str,
    candidate_json: str,
    source_memories_content: str,
) -> str:
    """Build evaluator prompt for the given role.

    Sections, in order: role line, the candidate, its source memories, the user
    context (advocate only), the shared Standard, the role's criteria, and the
    graded verdict format.

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

    sections = [
        f"You are evaluating a candidate insight. Your role is {_ROLE_DESCRIPTIONS[role]} "
        "You are one of four evaluators from two model families; your verdict, score and "
        "issue lists are combined with theirs.",
        f"## The candidate insight:\n{candidate_json}",
        f"## The source memories it references:\n{source_memories_content}",
    ]
    if role == "advocate":
        sections.append(_ADVOCATE_CONTEXT)
    sections += [_STANDARD, _ROLE_CRITERIA[role], _VERDICT_V2]
    return "\n\n".join(sections)


def get_evaluator_prompt_standard(
    role: str,
    candidate_json: str,
    source_memories_content: str,
) -> str:
    """Alias of :func:`get_evaluator_prompt` returning the identical text.

    Kept because the replay harness (``scripts/panel_replay.py``) resolves
    ``--prompt-variant standard`` to ``get_evaluator_prompt_standard``.
    """
    return get_evaluator_prompt(role, candidate_json, source_memories_content)
