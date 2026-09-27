"""The skim record: JSON schema, dataclass and validation.

One record per document (docs/SKIM-STAGE.md §2):

* ``summary`` — one neutral sentence, ≤ 200 characters
* ``people[]``, ``entities[]`` — names exactly as written in the text
* ``topics[]`` — at most 5 short tags
* ``decisions[]`` and ``open_questions[]`` — objects ``{text, quote}`` where
  ``quote`` is copied verbatim from the document (≤ 160 characters); the quote
  is what makes faithfulness mechanically checkable
* ``novelty`` 0–3, ``salience`` 0–3, ``contradiction_hint`` (string | null),
  ``doc_kind`` (enum)

Why the limits are enforced here rather than as JSON-Schema keywords: Bedrock's
structured-output grammar accepts only a subset of JSON Schema 2020-12 — no
``minimum`` / ``maximum``, no ``minLength`` / ``maxLength``, ``minItems`` only
0 or 1, ``additionalProperties`` only ``false``
(https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html).
The same schema is sent to Nova (forced tool, constrained decoding) and to
Gemma (``outputConfig.textFormat``), so it uses only portable keywords —
``enum`` for the 0–3 scores, plain arrays, ``["string", "null"]`` for the
nullable hint — and states the length limits in each ``description``.
:func:`validate_record` then enforces them exactly. Two modes:

* ``coerce=False`` — strict: a record that breaks a limit is invalid (the
  quality signal the probe counted in Phase 1).
* ``coerce=True`` (the default, decided after the first real probe on
  2026-09-20: all five invalid records out of 33 were length overruns, never
  wrong content) — over-long fields are trimmed instead of rejected and every
  trim is recorded in ``SkimRecord.coercions``: the summary is accepted up to
  :data:`SUMMARY_HARD_LIMIT_CHARS` and cut at the last sentence boundary
  before it (else hard cut); a quote is cut to a prefix of at most
  :data:`MAX_QUOTE_CHARS` (a prefix of a verbatim passage is still verbatim);
  ``topics`` keeps its first :data:`MAX_TOPICS`. Anything else — wrong types,
  missing fields, out-of-range scores — stays invalid in both modes.

Live check (2026-09-20, synthetic document, ``us.amazon.nova-2-lite-v1:0``):
Nova's tool path *accepts* ``maxLength`` / ``maxItems`` in the schema without an
error but does not enforce them — a 163-character quote came back unchanged
with ``maxLength: 160`` set — so schema keywords would buy nothing here either.
The quote limit is a prompt-and-validator matter; the probe and the runner
count how often the model overruns it (``coercions``).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

MAX_SUMMARY_CHARS = 200          # what the prompt asks for; the strict limit
SUMMARY_HARD_LIMIT_CHARS = 300   # coercion accepts up to this, trims beyond it
MAX_QUOTE_CHARS = 160
MAX_TOPICS = 5
SCORE_LEVELS = (0, 1, 2, 3)

#: Coercion classes recorded in ``SkimRecord.coercions`` (aggregate-safe labels).
COERCION_SUMMARY_TRIMMED = "summary_trimmed"
COERCION_TOPICS_TRUNCATED = "topics_truncated"
COERCION_QUOTE_TRIMMED = "quote_trimmed"       # recorded as "<field>.quote_trimmed"

# A sentence end: terminal punctuation, optional closing marks, then whitespace
# or the end of the (already cut) text.
_SENTENCE_END_RE = re.compile(r"[.!?]+[\"'\u201d\u2019)\]]*(?=\s|$)")
_LAST_WORD_RE = re.compile(r"\s\S*$")

#: What kind of document the text is (as observed, not the capture channel).
DOC_KINDS = (
    "article",        # web article, blog post, news, newsletter
    "conversation",   # chat transcript, Slack thread or DM, meeting transcript
    "email",
    "calendar",       # meeting invitation / event
    "note",           # personal note, memo, distilled summary, journal
    "code",           # source code, commit, PR, code review
    "reference",      # documentation, specification, manual, API reference
    "other",
)

_QUOTED_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "One short sentence stating the decision or question in the document's own terms.",
        },
        "quote": {
            "type": "string",
            "description": (
                "A passage copied verbatim from the document that states this — character for "
                f"character, at most {MAX_QUOTE_CHARS} characters. Never paraphrase or compose a quote."
            ),
        },
    },
    "required": ["text", "quote"],
    "additionalProperties": False,
}

SKIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "One neutral sentence, at most "
                f"{MAX_SUMMARY_CHARS} characters, saying what the document is and what it states. "
                "Empty string if the document has no substantive content."
            ),
        },
        "people": {
            "type": "array",
            "items": {"type": "string"},
            "description": "People named in the text, spelled exactly as written. Empty if none.",
        },
        "entities": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Organisations, teams, products, projects, systems and places named in the text, "
                "spelled exactly as written. Empty if none."
            ),
        },
        "topics": {
            "type": "array",
            "items": {"type": "string"},
            "description": f"At most {MAX_TOPICS} short topic tags (one to three words each). Empty if none.",
        },
        "decisions": {
            "type": "array",
            "items": _QUOTED_ITEM_SCHEMA,
            "description": (
                "Decisions the text explicitly records as made or agreed, each with a verbatim quote. "
                "Empty if the text records none."
            ),
        },
        "open_questions": {
            "type": "array",
            "items": _QUOTED_ITEM_SCHEMA,
            "description": (
                "Questions the text explicitly leaves open or asks, each with a verbatim quote. "
                "Empty if the text has none."
            ),
        },
        "novelty": {
            "type": "integer",
            "enum": list(SCORE_LEVELS),
            "description": (
                "How surprising or decision-bearing the content is: 0 routine or boilerplate, "
                "1 ordinary information, 2 a notable change, result or commitment, "
                "3 a surprising fact or a decision with consequences."
            ),
        },
        "salience": {
            "type": "integer",
            "enum": list(SCORE_LEVELS),
            "description": (
                "How much the text concerns the user's own work or life rather than generic content: "
                "0 generic material with no personal connection, 1 general interest, "
                "2 touches the user's projects, team or plans, 3 addressed to or about the user's own work."
            ),
        },
        "contradiction_hint": {
            "type": ["string", "null"],
            "description": (
                "One sentence if the text itself states that something changed, was reversed or "
                "conflicts with an earlier statement; otherwise null."
            ),
        },
        "doc_kind": {
            "type": "string",
            "enum": list(DOC_KINDS),
            "description": "What kind of document the text is.",
        },
    },
    "required": [
        "summary", "people", "entities", "topics", "decisions", "open_questions",
        "novelty", "salience", "contradiction_hint", "doc_kind",
    ],
    "additionalProperties": False,
}

REQUIRED_FIELDS: tuple[str, ...] = tuple(SKIM_SCHEMA["required"])


class SkimValidationError(ValueError):
    """A model reply is not a valid skim record. The message names the field."""


@dataclass(frozen=True)
class QuotedItem:
    """A decision or open question with the verbatim passage that states it."""

    text: str
    quote: str


@dataclass(frozen=True)
class SkimRecord:
    """A validated skim record (see :data:`SKIM_SCHEMA`).

    ``coercions`` lists what :func:`validate_record` trimmed to make the
    record valid (empty for a record that needed nothing). It is bookkeeping,
    not part of the record: :meth:`to_dict` leaves it out and equality
    ignores it.
    """

    summary: str
    people: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    decisions: list[QuotedItem] = field(default_factory=list)
    open_questions: list[QuotedItem] = field(default_factory=list)
    novelty: int = 0
    salience: int = 0
    contradiction_hint: str | None = None
    doc_kind: str = "other"
    coercions: list[str] = field(default_factory=list, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict in the schema's shape (without ``coercions``)."""
        d = asdict(self)
        d.pop("coercions", None)
        return d

    @property
    def quotes(self) -> list[str]:
        """Every quote the record carries, decisions first."""
        return [q.quote for q in self.decisions] + [q.quote for q in self.open_questions]

    @property
    def coerced(self) -> bool:
        return bool(self.coercions)


# --------------------------------------------------------------------------- #
# Trimming (coerce=True)                                                       #
# --------------------------------------------------------------------------- #
def trim_summary(summary: str, hard_limit: int = SUMMARY_HARD_LIMIT_CHARS) -> str:
    """Cut ``summary`` to at most ``hard_limit`` characters at the last sentence
    boundary before the limit; hard cut when there is none.

    A boundary in the first third is ignored (``"Dr."``-style false ends would
    otherwise leave almost nothing), so the result is never shorter than a third
    of the limit unless the text itself is.
    """
    if len(summary) <= hard_limit:
        return summary
    head = summary[:hard_limit]
    best: int | None = None
    for m in _SENTENCE_END_RE.finditer(head):
        if m.end() >= hard_limit // 3:
            best = m.end()
    return (head[:best] if best is not None else head).rstrip()


def trim_quote(quote: str, limit: int = MAX_QUOTE_CHARS) -> str:
    """Cut ``quote`` to a prefix of at most ``limit`` characters, at a word
    boundary when one exists. A prefix of a verbatim passage is still verbatim."""
    if len(quote) <= limit:
        return quote
    head = quote[:limit]
    if not quote[limit].isspace():
        m = _LAST_WORD_RE.search(head)
        if m is not None and m.start() > 0:
            head = head[:m.start()]
    return head.rstrip() or quote[:limit].rstrip()


def _require_str(value: Any, where: str, *, allow_empty: bool = True, max_chars: int | None = None) -> str:
    if not isinstance(value, str):
        raise SkimValidationError(f"{where} must be a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise SkimValidationError(f"{where} must not be empty")
    if max_chars is not None and len(value) > max_chars:
        raise SkimValidationError(f"{where} is {len(value)} characters; the limit is {max_chars}")
    return value


def _require_str_list(value: Any, where: str, *, max_items: int | None = None,
                      coercions: list[str] | None = None) -> list[str]:
    if not isinstance(value, list):
        raise SkimValidationError(f"{where} must be an array, got {type(value).__name__}")
    if max_items is not None and len(value) > max_items:
        if coercions is None:
            raise SkimValidationError(f"{where} has {len(value)} items; the limit is {max_items}")
        coercions.append(COERCION_TOPICS_TRUNCATED if where == "topics" else f"{where}_truncated")
        value = value[:max_items]
    out: list[str] = []
    for i, item in enumerate(value):
        out.append(_require_str(item, f"{where}[{i}]", allow_empty=False))
    return out


def _require_quoted_items(value: Any, where: str, *, coercions: list[str] | None = None) -> list[QuotedItem]:
    if not isinstance(value, list):
        raise SkimValidationError(f"{where} must be an array, got {type(value).__name__}")
    out: list[QuotedItem] = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise SkimValidationError(f"{where}[{i}] must be an object with text and quote")
        extra = set(item) - {"text", "quote"}
        if extra:
            raise SkimValidationError(f"{where}[{i}] has unknown field(s): {sorted(extra)}")
        if "quote" not in item:
            raise SkimValidationError(f"{where}[{i}] is missing its verbatim quote")
        if "text" not in item:
            raise SkimValidationError(f"{where}[{i}] is missing text")
        text = _require_str(item["text"], f"{where}[{i}].text", allow_empty=False)
        quote = _require_str(item["quote"], f"{where}[{i}].quote", allow_empty=False,
                             max_chars=None if coercions is not None else MAX_QUOTE_CHARS)
        if coercions is not None and len(quote) > MAX_QUOTE_CHARS:
            quote = trim_quote(quote)
            coercions.append(f"{where}.{COERCION_QUOTE_TRIMMED}")
        out.append(QuotedItem(text=text, quote=quote))
    return out


def _require_score(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SkimValidationError(f"{where} must be an integer 0-3, got {value!r}")
    if value not in SCORE_LEVELS:
        raise SkimValidationError(f"{where} must be between 0 and 3, got {value}")
    return value


def _summary(value: Any, coercions: list[str] | None) -> str:
    if coercions is None:
        return _require_str(value, "summary", max_chars=MAX_SUMMARY_CHARS)
    summary = _require_str(value, "summary")
    if len(summary) > SUMMARY_HARD_LIMIT_CHARS:
        summary = trim_summary(summary)
        coercions.append(COERCION_SUMMARY_TRIMMED)
    return summary


def validate_record(data: Any, *, coerce: bool = True) -> SkimRecord:
    """Validate a parsed model reply against the skim schema and its limits.

    Raises :class:`SkimValidationError` (a ``ValueError``) naming the offending
    field. Accepts exactly the schema's fields — nothing missing, nothing extra.

    With ``coerce=True`` (default) over-long fields are trimmed rather than
    rejected — see the module docstring — and the trims are listed in the
    returned record's ``coercions``. ``coerce=False`` is strict.
    """
    if not isinstance(data, dict):
        raise SkimValidationError(f"skim record must be a JSON object, got {type(data).__name__}")
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        raise SkimValidationError(f"skim record is missing field(s): {missing}")
    extra = sorted(set(data) - set(REQUIRED_FIELDS))
    if extra:
        raise SkimValidationError(f"skim record has unknown field(s): {extra}")
    coercions: list[str] | None = [] if coerce else None

    hint = data["contradiction_hint"]
    if hint is not None:
        hint = _require_str(hint, "contradiction_hint")
        if not hint.strip():
            hint = None  # an empty string means "none"; normalise to null

    doc_kind = _require_str(data["doc_kind"], "doc_kind")
    if doc_kind not in DOC_KINDS:
        raise SkimValidationError(f"doc_kind must be one of {list(DOC_KINDS)}, got {doc_kind!r}")

    return SkimRecord(
        summary=_summary(data["summary"], coercions),
        people=_require_str_list(data["people"], "people"),
        entities=_require_str_list(data["entities"], "entities"),
        topics=_require_str_list(data["topics"], "topics", max_items=MAX_TOPICS, coercions=coercions),
        decisions=_require_quoted_items(data["decisions"], "decisions", coercions=coercions),
        open_questions=_require_quoted_items(data["open_questions"], "open_questions", coercions=coercions),
        novelty=_require_score(data["novelty"], "novelty"),
        salience=_require_score(data["salience"], "salience"),
        contradiction_hint=hint,
        doc_kind=doc_kind,
        coercions=coercions or [],
    )
