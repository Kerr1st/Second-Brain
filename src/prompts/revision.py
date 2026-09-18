"""Revision prompt — repair a candidate insight from the grounding audit's findings.

Before the consensus panel votes, one evaluator seat audits the candidate against its
cited sources with the graded verdict schema (blocking / advisory issues). If it finds
defects, the Thinker's backend is asked — tool-less, once — to repair the candidate:
remove or correct what the sources do not support, narrow the claim to what they do,
fix attribution, keep the evidence. The panel then votes on the repaired candidate.

Why this exists (docs/PLAN-panel-acceptance.md, 2026-09-18): the GPT-6 Astra seats
rejected 69 of 70 candidates, and on inspection their objections were correct — the
Thinker's candidates carried fabricated details, misattributed quotations and claims
the sources contradicted. Repairing from the audit's own list and re-voting turned
3 of 4 Astra votes to ACCEPT in the offline probe.
"""

REVISION_PROMPT = """\
You are repairing a candidate insight for a personal knowledge system so that it is \
grounded in its cited sources. You are given the candidate (JSON), the full text of its \
cited source memories, and the issues an evaluator raised.

Rules:
- Remove or correct every BLOCKING issue: delete any statement, number, name, channel or \
quotation that does not appear in the cited sources; fix any claim the sources contradict; \
attribute wording to whoever the source shows produced it (a person, a system, an agent's \
summary of an email is not the email's author; an assistant's words are not the user's); \
drop conclusions that rest on uncited material.
- Address ADVISORY issues by narrowing: state each claim at the scope the sources support and \
quantify ("in the two captured notices") instead of generalising ("every", "never").
- Keep what is supported. Do not add new claims, new details or new sources. Keep the \
WHAT / EVIDENCE / WHY IT MATTERS structure; end EVIDENCE with one sentence beginning \
"claim_scope:".
- Set confidence by the evidence: 1 source = low; 2 sources or several from one day/conversation \
= medium; 3+ independent sources = high.
- Rewrite the title so it makes no claim the body does not support.
- If nothing supportable remains, return the candidate with content set to an empty string.

Return ONLY the revised candidate as a JSON object with the fields: title, content, \
confidence, confidence_reasoning, source_memories (a subset of the original ids)."""

REVISION_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "content": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "confidence_reasoning": {"type": "string"},
        "source_memories": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "content", "confidence", "source_memories"],
}


def build_revision_request(candidate_json: str, sources_text: str,
                           blocking: list[str], advisory: list[str]) -> str:
    """User message for the revision call (JSON, so quotes survive intact)."""
    import json
    return json.dumps({
        "candidate": json.loads(candidate_json) if candidate_json.startswith("{") else candidate_json,
        "cited_sources": sources_text,
        "blocking_issues": list(blocking),
        "advisory_issues": list(advisory),
    }, default=str)
