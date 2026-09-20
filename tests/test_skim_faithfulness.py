"""Tests for src/skim/faithfulness.py — verbatim-quote checking.

Synthetic source documents only.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from src.skim.faithfulness import check_record, normalize, quote_is_verbatim
from src.skim.schema import QuotedItem, SkimRecord

SOURCE = (
    "Orrery sync — 18 Sep\n\n"
    "Priya: after the load test we agreed to move the beta launch to the 14th.\n"
    "Tomasz: fine by me — the “pricing tier” question is still open:  which\n"
    "tier goes first?\n"
    "Action: Priya drafts the announcement… by Friday.\n"
)


class TestQuoteIsVerbatim:
    def test_exact_substring(self):
        assert quote_is_verbatim("we agreed to move the beta launch to the 14th", SOURCE)

    def test_whitespace_runs_and_line_breaks_normalised(self):
        assert quote_is_verbatim("question is still open: which tier goes first?", SOURCE)
        assert quote_is_verbatim("still   open:\nwhich tier", SOURCE)

    def test_typographic_quotes_and_dashes_normalised(self):
        assert quote_is_verbatim('the "pricing tier" question', SOURCE)          # straight vs curly
        assert quote_is_verbatim("fine by me - the", SOURCE)                       # hyphen vs em dash
        assert quote_is_verbatim("Orrery sync - 18 Sep", SOURCE)
        assert quote_is_verbatim("drafts the announcement... by Friday", SOURCE)  # ... vs …

    def test_surrounding_quote_marks_and_edge_ellipses_on_the_quote_are_ignored(self):
        assert quote_is_verbatim('"we agreed to move the beta launch"', SOURCE)
        assert quote_is_verbatim("…move the beta launch to the 14th…", SOURCE)
        assert quote_is_verbatim("'Priya drafts the announcement'", SOURCE)

    def test_paraphrase_is_not_verbatim(self):
        assert not quote_is_verbatim("we decided to move the beta launch to the 14th", SOURCE)
        assert not quote_is_verbatim("the beta launch was moved to the 14th", SOURCE)

    def test_case_is_not_folded(self):
        assert not quote_is_verbatim("We agreed to move the beta launch", SOURCE)

    def test_invented_detail_is_not_verbatim(self):
        assert not quote_is_verbatim("we agreed to move the beta launch to the 21st", SOURCE)

    def test_empty_or_marks_only_quote_is_never_verbatim(self):
        assert not quote_is_verbatim("", SOURCE)
        assert not quote_is_verbatim('""', SOURCE)
        assert not quote_is_verbatim("...", SOURCE)
        assert not quote_is_verbatim("   ", SOURCE)
        assert not quote_is_verbatim(None, SOURCE)  # type: ignore[arg-type]
        assert not quote_is_verbatim("x", None)     # type: ignore[arg-type]

    def test_normalize_is_idempotent_and_keeps_words(self):
        n = normalize(SOURCE)
        assert normalize(n) == n
        assert "beta launch" in n and "\n" not in n and "“" not in n and "—" not in n

    @given(st.text(min_size=1, max_size=200), st.integers(min_value=0, max_value=200),
           st.integers(min_value=1, max_value=200))
    @settings(max_examples=100)
    def test_any_real_substring_is_verbatim_unless_it_normalises_to_nothing(self, text, start, length):
        """A passage genuinely copied from the source is always accepted — except
        the degenerate case where it consists only of whitespace / quote marks /
        ellipsis, which carries no checkable content."""
        piece = text[start:start + length]
        result = quote_is_verbatim(piece, text)
        from src.skim.faithfulness import _strip_outer
        assert result == bool(_strip_outer(normalize(piece)))


class TestCheckRecord:
    def _record(self, decisions, questions):
        return SkimRecord(summary="s", decisions=[QuotedItem("d", q) for q in decisions],
                          open_questions=[QuotedItem("o", q) for q in questions])

    def test_counts_and_positions(self):
        rec = self._record(
            ["we agreed to move the beta launch to the 14th", "the launch is cancelled"],
            ["which tier goes first?"],
        )
        res = check_record(rec, SOURCE)
        assert res["quotes_emitted"] == 3 and res["quotes_verbatim"] == 2 and res["quotes_fabricated"] == 1
        assert res["fabricated_positions"] == [("decisions", 1)]
        assert res["per_field"] == {"decisions": {"total": 2, "verbatim": 1},
                                    "open_questions": {"total": 1, "verbatim": 1}}
        assert res["all_verbatim"] is False

    def test_no_quotes_is_vacuously_faithful(self):
        res = check_record(self._record([], []), SOURCE)
        assert res == {"quotes_emitted": 0, "quotes_verbatim": 0, "quotes_fabricated": 0,
                       "fabricated_positions": [], "per_field": {"decisions": {"total": 0, "verbatim": 0},
                                                                 "open_questions": {"total": 0, "verbatim": 0}},
                       "all_verbatim": True}

    def test_result_never_carries_quote_text(self):
        """Aggregates only: the check result is printed by the probe, so it must not
        leak document-derived text."""
        secret = "we agreed to move the beta launch to the 14th"
        res = check_record(self._record([secret], []), SOURCE)
        assert secret not in repr(res)
