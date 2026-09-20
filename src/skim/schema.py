"""The skim record: JSON schema, dataclass and validation.

One record per document (docs/PLAN-skim-stage.md §2):

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
:func:`validate_record` then enforces them exactly; a record that breaks one is
invalid (that is a quality signal the probe counts, not something to repair).

Live check (2026-09-20, synthetic document, ``us.amazon.nova-2-lite-v1:0``):
Nova's tool path *accepts* ``maxLength`` / ``maxItems`` in the schema without an
error but does not enforce them — a 163-character quote came back unchanged
with ``maxLength: 160`` set — so schema keywords would buy nothing here either.
The quote limit is a prompt-and-validator matter; the probe measures how often
the model overruns it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

MAX_SUMMARY_CHARS = 200
MAX_QUOTE_CHARS = 160
MAX_TOPICS = 5
SCORE_LEVELS = (0, 1, 2, 3)

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
    """A validated skim record (see :data:`SKIM_SCHEMA`)."""

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

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict in the schema's shape."""
        return asdict(self)

    @property
    def quotes(self) -> list[str]:
        """Every quote the record carries, decisions first."""
        return [q.quote for q in self.decisions] + [q.quote for q in self.open_questions]


def _require_str(value: Any, where: str, *, allow_empty: bool = True, max_chars: int | None = None) -> str:
    if not isinstance(value, str):
        raise SkimValidationError(f"{where} must be a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise SkimValidationError(f"{where} must not be empty")
    if max_chars is not None and len(value) > max_chars:
        raise SkimValidationError(f"{where} is {len(value)} characters; the limit is {max_chars}")
    return value


def _require_str_list(value: Any, where: str, *, max_items: int | None = None) -> list[str]:
    if not isinstance(value, list):
        raise SkimValidationError(f"{where} must be an array, got {type(value).__name__}")
    if max_items is not None and len(value) > max_items:
        raise SkimValidationError(f"{where} has {len(value)} items; the limit is {max_items}")
    out: list[str] = []
    for i, item in enumerate(value):
        out.append(_require_str(item, f"{where}[{i}]", allow_empty=False))
    return out


def _require_quoted_items(value: Any, where: str) -> list[QuotedItem]:
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
        quote = _require_str(item["quote"], f"{where}[{i}].quote", allow_empty=False, max_chars=MAX_QUOTE_CHARS)
        out.append(QuotedItem(text=text, quote=quote))
    return out


def _require_score(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SkimValidationError(f"{where} must be an integer 0-3, got {value!r}")
    if value not in SCORE_LEVELS:
        raise SkimValidationError(f"{where} must be between 0 and 3, got {value}")
    return value


def validate_record(data: Any) -> SkimRecord:
    """Validate a parsed model reply against the skim schema and its limits.

    Raises :class:`SkimValidationError` (a ``ValueError``) naming the offending
    field. Accepts exactly the schema's fields — nothing missing, nothing extra.
    """
    if not isinstance(data, dict):
        raise SkimValidationError(f"skim record must be a JSON object, got {type(data).__name__}")
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        raise SkimValidationError(f"skim record is missing field(s): {missing}")
    extra = sorted(set(data) - set(REQUIRED_FIELDS))
    if extra:
        raise SkimValidationError(f"skim record has unknown field(s): {extra}")

    hint = data["contradiction_hint"]
    if hint is not None:
        hint = _require_str(hint, "contradiction_hint")
        if not hint.strip():
            hint = None  # an empty string means "none"; normalise to null

    doc_kind = _require_str(data["doc_kind"], "doc_kind")
    if doc_kind not in DOC_KINDS:
        raise SkimValidationError(f"doc_kind must be one of {list(DOC_KINDS)}, got {doc_kind!r}")

    return SkimRecord(
        summary=_require_str(data["summary"], "summary", max_chars=MAX_SUMMARY_CHARS),
        people=_require_str_list(data["people"], "people"),
        entities=_require_str_list(data["entities"], "entities"),
        topics=_require_str_list(data["topics"], "topics", max_items=MAX_TOPICS),
        decisions=_require_quoted_items(data["decisions"], "decisions"),
        open_questions=_require_quoted_items(data["open_questions"], "open_questions"),
        novelty=_require_score(data["novelty"], "novelty"),
        salience=_require_score(data["salience"], "salience"),
        contradiction_hint=hint,
        doc_kind=doc_kind,
    )
