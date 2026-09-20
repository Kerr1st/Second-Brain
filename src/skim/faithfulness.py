"""Mechanical faithfulness check: is every quote in a skim record verbatim?

The skim record carries a ``quote`` for each decision and open question. A
quote is faithful when it is a contiguous passage of the source document. Two
paths establish that, tried in order:

``verbatim_exact``
    Substring match after a small, symmetric normalisation of both sides —
    whitespace runs, typographic quote marks, dashes and ellipses — so a model
    that copied the passage correctly but through a Unicode-flattening
    tokenizer is not counted as fabricating. Case is kept.

``verbatim_tokens``
    Both sides are reduced to a list of lower-cased alphanumeric tokens
    (markdown marks, bullets, punctuation, quote marks, URL separators all
    vanish) and the quote passes when its token list occurs as a *contiguous*
    sublist of the source's. This is what the first real probe needed
    (2026-09-20): every "fabricated" quote had 100% token overlap with the
    source and differed only in markdown / punctuation — ``**bold**`` markers,
    a bullet dash, a backtick, a smart quote — not in wording. Reordered or
    replaced words still fail, because the *sequence* must match.

Both paths accept only what the document says; neither can turn a paraphrase
into a verbatim quote. This is the primary quality metric of the skim
(docs/PLAN-skim-stage.md §2, Decisions): it is free, it needs no second model,
and it catches exactly the failure mode — invented detail — that the panel's
grounding audit exists for.

**Stored form** (:func:`stored_form`, policy decided 2026-09-20 after two live
probes on 33 documents: 8/10 quotes verbatim, the two misses near-verbatim edits
of one long transcript — light paraphrase, not invention): a decision or open
question whose quote fails *both* paths keeps its ``text`` but its ``quote`` is
set to ``null`` and counted as *unverified*; the record gains
``quotes_unverified: n``. The item stays in the record because the model's
reading is still useful to the Explorer; the quote goes because nothing
downstream may treat an unverified passage as the document's words.
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

#: A token is a maximal run of Unicode letters and digits; everything else —
#: markdown ``* _ \` # > | -``, bullets, quote marks, dashes, the punctuation of
#: URLs — is a separator. Underscore is excluded from ``\w`` on purpose (it is
#: markdown emphasis, not a word character here).
_TOKEN_RE = re.compile(r"[^\W_]+")

#: How a quote was matched (see module docstring); ``None`` means not found.
VERBATIM_EXACT = "verbatim_exact"
VERBATIM_TOKENS = "verbatim_tokens"
MATCH_PATHS = (VERBATIM_EXACT, VERBATIM_TOKENS)


def normalize(text: str) -> str:
    """Normalise whitespace, quote marks, dashes and ellipses; keep case and words."""
    if not text:
        return ""
    text = text.translate(_TRANSLATION)
    text = _ELLIPSIS_RE.sub("...", text)
    return _WS_RE.sub(" ", text).strip()


def tokenize(text: str) -> list[str]:
    """Lower-cased alphanumeric tokens of ``text``; markdown and punctuation vanish.

    ``"- **Decision:** ship `v2` on the 14th"`` → ``["decision", "ship", "v2",
    "on", "the", "14th"]``. Applied identically to quote and source.
    """
    if not text:
        return []
    return [t.casefold() for t in _TOKEN_RE.findall(text.translate(_TRANSLATION))]


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


def _tokens_are_contiguous_sublist(needle: list[str], haystack: list[str]) -> bool:
    """True when ``needle`` occurs as a contiguous run inside ``haystack``.

    Implemented as a substring search over the tokens joined with a separator
    that cannot occur inside a token, so token boundaries are respected
    (``["a", "bc"]`` never matches ``["ab", "c"]``) and the search runs in C.
    """
    if not needle or len(needle) > len(haystack):
        return False
    sep = "\x00"
    return f"{sep}{sep.join(needle)}{sep}" in f"{sep}{sep.join(haystack)}{sep}"


def match_quote(quote: str, source_text: str) -> str | None:
    """How ``quote`` occurs in ``source_text``: ``"verbatim_exact"``,
    ``"verbatim_tokens"`` or ``None`` when it does not.

    The exact path is tried first (fast, case-preserving); the token path only
    when it fails. An empty quote (or one that is only quote marks / ellipsis /
    punctuation) never matches — there is nothing to check.
    """
    if not isinstance(quote, str) or not isinstance(source_text, str):
        return None
    q = _strip_outer(normalize(quote))
    if not q:
        return None
    if q in normalize(source_text):
        return VERBATIM_EXACT
    if _tokens_are_contiguous_sublist(tokenize(q), tokenize(source_text)):
        return VERBATIM_TOKENS
    return None


def quote_is_verbatim(quote: str, source_text: str) -> bool:
    """True when ``quote`` is found in ``source_text`` by either path."""
    return match_quote(quote, source_text) is not None


def check_record(record: SkimRecord, source_text: str) -> dict[str, Any]:
    """Count verbatim vs fabricated quotes in ``record`` against ``source_text``.

    Returns counts only plus the ``(field, index)`` positions of fabricated
    quotes — never the quote text itself, so the result is safe to aggregate
    and print. Per field and overall, ``verbatim`` is split by the path that
    established it (``verbatim_exact`` / ``verbatim_tokens``).
    ``all_verbatim`` is True when every emitted quote was found (vacuously
    True for a record with no quotes).
    """
    fabricated: list[tuple[str, int]] = []
    per_field: dict[str, dict[str, int]] = {}
    for field_name in ("decisions", "open_questions"):
        items = getattr(record, field_name)
        counts = {"total": len(items), "verbatim": 0, VERBATIM_EXACT: 0, VERBATIM_TOKENS: 0}
        for i, item in enumerate(items):
            path = match_quote(item.quote, source_text)
            if path is None:
                fabricated.append((field_name, i))
            else:
                counts["verbatim"] += 1
                counts[path] += 1
        per_field[field_name] = counts
    emitted = sum(v["total"] for v in per_field.values())
    verbatim = sum(v["verbatim"] for v in per_field.values())
    return {
        "quotes_emitted": emitted,
        "quotes_verbatim": verbatim,
        "quotes_fabricated": emitted - verbatim,
        "fabricated_positions": fabricated,
        "match_paths": {p: sum(v[p] for v in per_field.values()) for p in MATCH_PATHS},
        "per_field": per_field,
        "all_verbatim": emitted == verbatim,
    }



#: Key added to the stored record: how many of its quotes failed both paths.
QUOTES_UNVERIFIED_KEY = "quotes_unverified"


def stored_form(record: SkimRecord, source_text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The record as it is persisted, plus the :func:`check_record` result.

    Applies the unverified-quote policy (module docstring): for every
    ``(field, index)`` in ``fabricated_positions`` the item's ``quote`` becomes
    ``None`` while its ``text`` stays, and the dict gains
    ``quotes_unverified`` (0 when every quote was found). The
    :class:`SkimRecord` itself is untouched.
    """
    faith = check_record(record, source_text)
    data = record.to_dict()
    for field_name, i in faith["fabricated_positions"]:
        data[field_name][i]["quote"] = None
    data[QUOTES_UNVERIFIED_KEY] = len(faith["fabricated_positions"])
    return data, faith
