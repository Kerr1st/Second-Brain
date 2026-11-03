"""Tests for src/skim/faithfulness.py — verbatim-quote checking.

Synthetic source documents only.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from src.skim.faithfulness import (
    VERBATIM_EXACT,
    VERBATIM_TOKENS,
    check_record,
    match_quote,
    normalize,
    quote_is_verbatim,
    stored_form,
    tokenize,
)
from src.skim.schema import QuotedItem, SkimRecord

SOURCE = (
    "Orrery sync — 18 Sep\n\n"
    "Priya: after the load test we agreed to move the beta launch to the 14th.\n"
    "Tomasz: fine by me — the “pricing tier” question is still open:  which\n"
    "tier goes first?\n"
    "Action: Priya drafts the announcement… by Friday.\n"
)

# A markdown-heavy source, as captured articles and Slack exports arrive.
MD_SOURCE = (
    "# Orrery beta — decisions\n\n"
    "- **Launch:** we agreed to move the *beta launch* to the 14th.\n"
    "- Tooling: run `skim_probe.py --since 2026-09-17` before every release.\n"
    "> Priya: “the pricing tier question is still open — which tier goes first?”\n\n"
    "| owner | task |\n|---|---|\n| Tomasz | drafts the announcement by Friday |\n"
    "See https://example.com/orrery-beta_plan#launch for the plan.\n"
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

    def test_case_difference_is_not_exact_but_matches_by_tokens(self):
        """The exact path keeps case; the token path folds it (a quote that starts
        mid-sentence and capitalises its first word is not a fabrication)."""
        assert match_quote("We agreed to move the beta launch", SOURCE) == VERBATIM_TOKENS
        assert match_quote("we agreed to move the beta launch", SOURCE) == VERBATIM_EXACT

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


class TestTokenPath:
    """The second path: markdown and punctuation differences are not fabrication,
    but the word sequence must be the document's."""

    def test_tokenize_strips_markdown_punctuation_and_case(self):
        assert tokenize("- **Decision:** ship `v2` on the 14th — “now”!") == [
            "decision", "ship", "v2", "on", "the", "14th", "now"]
        assert tokenize("https://example.com/orrery-beta_plan#launch") == [
            "https", "example", "com", "orrery", "beta", "plan", "launch"]
        assert tokenize("") == [] and tokenize("*** --- >>> |||") == []

    def test_bold_and_italic_wrapping_is_ignored(self):
        assert match_quote("Launch: we agreed to move the beta launch to the 14th.", MD_SOURCE) == VERBATIM_TOKENS
        assert match_quote("we agreed to move the beta launch to the 14th", MD_SOURCE) == VERBATIM_TOKENS

    def test_bulleted_line_quoted_without_the_bullet(self):
        assert match_quote("Tooling: run skim_probe.py --since 2026-09-17 before every release.",
                           MD_SOURCE) == VERBATIM_TOKENS
        assert match_quote("- Tooling: run `skim_probe.py --since 2026-09-17` before every release.",
                           MD_SOURCE) == VERBATIM_EXACT

    def test_backticks_dropped_or_added(self):
        assert match_quote("run skim_probe.py --since 2026-09-17", MD_SOURCE) == VERBATIM_TOKENS
        assert match_quote("drafts the `announcement` by Friday", MD_SOURCE) == VERBATIM_TOKENS

    def test_smart_quotes_and_blockquote_marker(self):
        assert match_quote('Priya: "the pricing tier question is still open - which tier goes first?"',
                           MD_SOURCE) == VERBATIM_EXACT          # normalisation already covers marks
        assert match_quote("Priya said the pricing tier question is still open", MD_SOURCE) is None
        assert match_quote("the pricing tier question is still open, which tier goes first",
                           MD_SOURCE) == VERBATIM_TOKENS         # comma for dash, marks dropped

    def test_table_cell_and_url_punctuation(self):
        assert match_quote("Tomasz drafts the announcement by Friday", MD_SOURCE) == VERBATIM_TOKENS
        assert match_quote("example.com/orrery-beta_plan", MD_SOURCE) == VERBATIM_EXACT
        assert match_quote("example com orrery beta plan", MD_SOURCE) == VERBATIM_TOKENS

    def test_paraphrase_reordered_or_replaced_words_still_fail(self):
        assert match_quote("we agreed to move the launch of the beta to the 14th", MD_SOURCE) is None  # reordered
        assert match_quote("we decided to move the beta launch to the 14th", MD_SOURCE) is None       # replaced
        assert match_quote("we agreed to move the beta launch to the 15th", MD_SOURCE) is None        # invented
        assert match_quote("move the beta launch to the 14th before every release", MD_SOURCE) is None  # spliced

    def test_token_boundaries_are_respected(self):
        assert match_quote("beta launch", "the betalaunch is on") is None
        assert match_quote("the 14", "we agreed on the14th") is None

    def test_punctuation_only_quote_never_matches_by_tokens(self):
        assert match_quote("^^^", MD_SOURCE) is None
        assert match_quote("- | -", MD_SOURCE) is None

    def test_exact_path_wins_when_both_apply(self):
        assert match_quote("drafts the announcement by Friday", MD_SOURCE) == VERBATIM_EXACT


class TestCheckRecord:
    def _record(self, decisions, questions):
        return SkimRecord(summary="s", decisions=[QuotedItem("d", q) for q in decisions],
                          open_questions=[QuotedItem("o", q) for q in questions])

    def test_counts_positions_and_match_paths(self):
        rec = self._record(
            ["we agreed to move the beta launch to the 14th", "the launch is cancelled",
             "We agreed to move the beta launch"],
            ["which tier goes first?"],
        )
        res = check_record(rec, SOURCE)
        assert res["quotes_emitted"] == 4 and res["quotes_verbatim"] == 3 and res["quotes_fabricated"] == 1
        assert res["fabricated_positions"] == [("decisions", 1)]
        assert res["per_field"] == {
            "decisions": {"total": 3, "verbatim": 2, "verbatim_exact": 1, "verbatim_tokens": 1},
            "open_questions": {"total": 1, "verbatim": 1, "verbatim_exact": 1, "verbatim_tokens": 0},
        }
        assert res["match_paths"] == {"verbatim_exact": 2, "verbatim_tokens": 1}
        assert res["all_verbatim"] is False

    def test_no_quotes_is_vacuously_faithful(self):
        res = check_record(self._record([], []), SOURCE)
        empty = {"total": 0, "verbatim": 0, "verbatim_exact": 0, "verbatim_tokens": 0}
        assert res == {"quotes_emitted": 0, "quotes_verbatim": 0, "quotes_fabricated": 0,
                       "fabricated_positions": [], "match_paths": {"verbatim_exact": 0, "verbatim_tokens": 0},
                       "per_field": {"decisions": dict(empty), "open_questions": dict(empty)},
                       "all_verbatim": True}

    def test_result_never_carries_quote_text(self):
        """Aggregates only: the check result is printed by the probe, so it must not
        leak document-derived text."""
        secret = "we agreed to move the beta launch to the 14th"
        res = check_record(self._record([secret], []), SOURCE)
        assert secret not in repr(res)



class TestStoredForm:
    """The persisted record: unverified quotes become null, the item stays,
    ``quotes_unverified`` counts them; the SkimRecord is untouched."""

    def _record(self):
        return SkimRecord(summary="s", people=["Priya"],
                          decisions=[QuotedItem("moved", "we agreed to move the beta launch to the 14th"),
                                     QuotedItem("cancelled", "the launch is cancelled")],
                          open_questions=[QuotedItem("tier", "which tier goes first?")])

    def test_nulls_only_the_failing_quotes(self):
        rec = self._record()
        data, faith = stored_form(rec, SOURCE)
        assert data["decisions"] == [{"text": "moved", "quote": "we agreed to move the beta launch to the 14th"},
                                     {"text": "cancelled", "quote": None}]
        assert data["open_questions"] == [{"text": "tier", "quote": "which tier goes first?"}]
        assert data["quotes_unverified"] == 1 and faith["quotes_fabricated"] == 1
        assert "coercions" not in data and data["people"] == ["Priya"]
        assert rec.decisions[1].quote == "the launch is cancelled"          # record itself untouched

    def test_all_verbatim_gives_zero(self):
        data, faith = stored_form(SkimRecord(summary="s"), SOURCE)
        assert data["quotes_unverified"] == 0 and faith["all_verbatim"] is True
