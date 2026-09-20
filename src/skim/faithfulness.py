"""Mechanical faithfulness check: is every quote in a skim record verbatim?

The skim record carries a ``quote`` for each decision and open question. A
quote is faithful when it is a contiguous passage of the source document. The
check is a substring match after a small, symmetric normalisation of both
sides — whitespace runs, typographic quote marks, dashes and ellipses — so a
model that copied the passage correctly but through a Unicode-flattening
tokenizer is not counted as fabricating, while any change of wording is. Case
is not folded: a quote is verbatim or it is not.

This is the primary quality metric of the skim (docs/PLAN-skim-stage.md §2,
Decisions): it is free, it needs no second model, and it catches exactly the
failure mode — invented detail — that the panel's grounding audit exists for.
"""

from __future__ import annotations

import re
from typing import Any

from src.skim.schema import SkimRecord

_WS_RE = re.compile(r"\s+")
_ELLIPSIS_RE = re.compile(r"\.{3,}")
_QUOTE_MARKS = {
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u00ab": '"', "\u00bb": '"',
    "\u2033": '"', "\u2036": '"',
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2039": "'", "\u203a": "'",
    "\u2032": "'", "\u0060": "'", "\u00b4": "'",
}
_DASHES = {c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u00ad"}
_INVISIBLE = {c: "" for c in "\u200b\u200c\u200d\u2060\ufeff"}
_TRANSLATION = str.maketrans({**_QUOTE_MARKS, **_DASHES, **_INVISIBLE, "\u2026": "..."})
_OUTER_QUOTE_CHARS = "\"'"


def normalize(text: str) -> str:
    """Normalise whitespace, quote marks, dashes and ellipses; keep case and words."""
    if not text:
        return ""
    text = text.translate(_TRANSLATION)
    text = _ELLIPSIS_RE.sub("...", text)
    return _WS_RE.sub(" ", text).strip()


def _strip_outer(quote: str) -> str:
    """Drop one layer of surrounding quote marks and edge ellipses from a quote.

    Models often wrap a copied passage in quotation marks or mark a cut with
    ``...``; neither changes what was copied. Stripping happens on the quote
    only and can never turn a fabricated quote into a verbatim one, since the
    remainder still has to occur in the source.
    """
    q = quote.strip()
    while len(q) >= 2 and q[0] in _OUTER_QUOTE_CHARS and q[-1] in _OUTER_QUOTE_CHARS:
        q = q[1:-1].strip()
    if q.startswith("..."):
        q = q[3:].lstrip()
    if q.endswith("..."):
        q = q[:-3].rstrip()
    return q


def quote_is_verbatim(quote: str, source_text: str) -> bool:
    """True when ``quote`` occurs in ``source_text`` after normalisation.

    An empty quote (or one that is only quote marks / ellipsis) is never
    verbatim — there is nothing to check.
    """
    if not isinstance(quote, str) or not isinstance(source_text, str):
        return False
    q = _strip_outer(normalize(quote))
    if not q:
        return False
    return q in normalize(source_text)


def check_record(record: SkimRecord, source_text: str) -> dict[str, Any]:
    """Count verbatim vs fabricated quotes in ``record`` against ``source_text``.

    Returns counts only plus the ``(field, index)`` positions of fabricated
    quotes — never the quote text itself, so the result is safe to aggregate
    and print. ``all_verbatim`` is True when every emitted quote was found
    (vacuously True for a record with no quotes).
    """
    fabricated: list[tuple[str, int]] = []
    per_field: dict[str, dict[str, int]] = {}
    for field_name in ("decisions", "open_questions"):
        items = getattr(record, field_name)
        found = 0
        for i, item in enumerate(items):
            if quote_is_verbatim(item.quote, source_text):
                found += 1
            else:
                fabricated.append((field_name, i))
        per_field[field_name] = {"total": len(items), "verbatim": found}
    emitted = sum(v["total"] for v in per_field.values())
    verbatim = sum(v["verbatim"] for v in per_field.values())
    return {
        "quotes_emitted": emitted,
        "quotes_verbatim": verbatim,
        "quotes_fabricated": emitted - verbatim,
        "fabricated_positions": fabricated,
        "per_field": per_field,
        "all_verbatim": emitted == verbatim,
    }
