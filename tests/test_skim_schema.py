"""Tests for src/skim/schema.py — the skim record schema, dataclass and validation.

All fixtures are synthetic documents written for these tests (no corpus content).
"""

from __future__ import annotations

import copy
import json

import pytest

from src.skim.schema import (
    COERCION_SUMMARY_TRIMMED,
    COERCION_TOPICS_TRUNCATED,
    DOC_KINDS,
    MAX_QUOTE_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TOPICS,
    SKIM_SCHEMA,
    SUMMARY_HARD_LIMIT_CHARS,
    QuotedItem,
    SkimRecord,
    SkimValidationError,
    trim_quote,
    trim_summary,
    validate_record,
)


def good_record() -> dict:
    """A synthetic, fully valid skim record (fictional project, fictional people)."""
    return {
        "summary": "Meeting notes in which the Orrery team agrees to move the beta launch to the 14th and leaves the pricing tier open.",
        "people": ["Priya Natarajan", "Tomasz Wierzbicki"],
        "entities": ["Orrery", "Launch Council"],
        "topics": ["beta launch", "pricing", "scheduling"],
        "decisions": [
            {"text": "Beta launch moves to the 14th.", "quote": "we agreed to move the beta launch to the 14th"},
        ],
        "open_questions": [
            {"text": "Which pricing tier ships first is undecided.", "quote": "still open: which pricing tier goes first?"},
        ],
        "novelty": 2,
        "salience": 3,
        "contradiction_hint": None,
        "doc_kind": "note",
    }


class TestSchemaShape:
    def test_schema_lists_exactly_the_plan_fields(self):
        expected = {"summary", "people", "entities", "topics", "decisions", "open_questions",
                    "novelty", "salience", "contradiction_hint", "doc_kind"}
        assert set(SKIM_SCHEMA["properties"]) == expected
        assert set(SKIM_SCHEMA["required"]) == expected
        assert SKIM_SCHEMA["additionalProperties"] is False

    def test_scores_are_enums_and_hint_is_nullable(self):
        assert SKIM_SCHEMA["properties"]["novelty"]["enum"] == [0, 1, 2, 3]
        assert SKIM_SCHEMA["properties"]["salience"]["enum"] == [0, 1, 2, 3]
        assert SKIM_SCHEMA["properties"]["contradiction_hint"]["type"] == ["string", "null"]
        assert SKIM_SCHEMA["properties"]["doc_kind"]["enum"] == list(DOC_KINDS)

    def test_quoted_items_require_text_and_quote(self):
        for f in ("decisions", "open_questions"):
            items = SKIM_SCHEMA["properties"][f]["items"]
            assert set(items["properties"]) == {"text", "quote"}
            assert set(items["required"]) == {"text", "quote"}
            assert items["additionalProperties"] is False

    def test_schema_uses_only_bedrock_portable_keywords(self):
        """Bedrock's structured-output grammar rejects minimum/maximum, minLength/
        maxLength, maxItems... (structured-output.html). The limits live in the
        descriptions and in validate_record instead."""
        banned = {"minimum", "maximum", "minLength", "maxLength", "maxItems", "pattern", "format"}
        def walk(node):
            if isinstance(node, dict):
                assert not (set(node) & banned), f"non-portable keyword in {node}"
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(SKIM_SCHEMA)
        assert str(MAX_SUMMARY_CHARS) in SKIM_SCHEMA["properties"]["summary"]["description"]
        assert str(MAX_QUOTE_CHARS) in SKIM_SCHEMA["properties"]["decisions"]["items"]["properties"]["quote"]["description"]
        assert str(MAX_TOPICS) in SKIM_SCHEMA["properties"]["topics"]["description"]

    def test_schema_is_json_serialisable(self):
        json.dumps(SKIM_SCHEMA)


class TestValidateAccepts:
    def test_good_record_round_trips(self):
        rec = validate_record(good_record())
        assert isinstance(rec, SkimRecord)
        assert rec.people == ["Priya Natarajan", "Tomasz Wierzbicki"]
        assert rec.decisions == [QuotedItem(text="Beta launch moves to the 14th.",
                                            quote="we agreed to move the beta launch to the 14th")]
        assert rec.novelty == 2 and rec.salience == 3 and rec.contradiction_hint is None
        assert rec.doc_kind == "note"
        assert rec.to_dict() == good_record()
        assert rec.quotes == ["we agreed to move the beta launch to the 14th",
                              "still open: which pricing tier goes first?"]

    def test_empty_when_absent_is_valid(self):
        data = good_record()
        data.update({"summary": "", "people": [], "entities": [], "topics": [], "decisions": [],
                     "open_questions": [], "novelty": 0, "salience": 0, "contradiction_hint": None,
                     "doc_kind": "other"})
        rec = validate_record(data)
        assert rec.quotes == [] and rec.summary == ""

    def test_contradiction_hint_string_kept_and_blank_normalised_to_null(self):
        data = good_record()
        data["contradiction_hint"] = "The note says the launch was earlier fixed for the 7th."
        assert validate_record(data).contradiction_hint.startswith("The note says")
        data["contradiction_hint"] = "   "
        assert validate_record(data).contradiction_hint is None

    def test_limits_are_inclusive(self):
        data = good_record()
        data["summary"] = "x" * MAX_SUMMARY_CHARS
        data["topics"] = [f"t{i}" for i in range(MAX_TOPICS)]
        data["decisions"][0]["quote"] = "q" * MAX_QUOTE_CHARS
        rec = validate_record(data, coerce=False)
        assert rec.coercions == [] and not rec.coerced


class TestCoercion:
    """coerce=True (default): over-long fields are trimmed and the trims recorded;
    coerce=False keeps the strict Phase 1 behaviour."""

    def test_valid_record_has_no_coercions(self):
        rec = validate_record(good_record())
        assert rec.coercions == [] and rec.coerced is False

    def test_summary_between_soft_and_hard_limit_is_kept_untouched(self):
        data = good_record()
        data["summary"] = "s" * (MAX_SUMMARY_CHARS + 1)
        rec = validate_record(data)
        assert rec.summary == data["summary"] and rec.coercions == []
        data["summary"] = "s" * SUMMARY_HARD_LIMIT_CHARS
        assert validate_record(data).coercions == []

    def test_summary_over_hard_limit_is_cut_at_last_sentence_boundary(self):
        data = good_record()
        first = "The note records the Orrery beta plan and the open pricing tier question in some detail."
        second = " It then restates the launch schedule, the owners for each task and the review dates at length."
        third = (" Finally it lists follow-ups, risks, mitigations and the people responsible for each of them,"
                 " together with the dates on which every item is to be reviewed again by the whole team.")
        data["summary"] = first + second + third
        assert len(first + second) <= SUMMARY_HARD_LIMIT_CHARS < len(data["summary"])
        rec = validate_record(data)
        assert rec.summary == first + second
        assert len(rec.summary) <= SUMMARY_HARD_LIMIT_CHARS
        assert rec.coercions == [COERCION_SUMMARY_TRIMMED]

    def test_summary_without_boundary_is_hard_cut(self):
        data = good_record()
        data["summary"] = "word " * 100  # 500 chars, no sentence end
        rec = validate_record(data)
        assert len(rec.summary) <= SUMMARY_HARD_LIMIT_CHARS and rec.summary == ("word " * 60).rstrip()
        assert rec.coercions == [COERCION_SUMMARY_TRIMMED]

    def test_trim_summary_ignores_a_boundary_in_the_first_third(self):
        text = "Dr. " + "x" * 400
        out = trim_summary(text, 300)
        assert len(out) == 300 and out.startswith("Dr. x")

    def test_quote_is_cut_to_a_prefix_at_a_word_boundary(self):
        data = good_record()
        long_quote = ("we agreed to move the beta launch to the fourteenth of the month " * 4).strip()
        assert len(long_quote) > MAX_QUOTE_CHARS
        data["decisions"][0]["quote"] = long_quote
        data["open_questions"][0]["quote"] = "q" * (MAX_QUOTE_CHARS + 40)  # one unbreakable token
        rec = validate_record(data)
        q = rec.decisions[0].quote
        assert len(q) <= MAX_QUOTE_CHARS and long_quote.startswith(q) and not q.endswith(" ")
        assert long_quote[len(q)] == " ", "cut falls on a word boundary"
        assert rec.open_questions[0].quote == "q" * MAX_QUOTE_CHARS
        assert rec.coercions == ["decisions.quote_trimmed", "open_questions.quote_trimmed"]

    def test_trim_quote_keeps_verbatim_prefix(self):
        source = "alpha beta gamma delta epsilon"
        assert trim_quote(source, 12) == "alpha beta"
        assert trim_quote(source, 10) == "alpha beta"      # next char is a space: no word cut
        assert trim_quote("abcdefghij", 4) == "abcd"        # no boundary at all: hard cut
        assert trim_quote(source, 100) == source

    def test_topics_keep_the_first_five(self):
        data = good_record()
        data["topics"] = [f"topic {i}" for i in range(MAX_TOPICS + 3)]
        rec = validate_record(data)
        assert rec.topics == [f"topic {i}" for i in range(MAX_TOPICS)]
        assert rec.coercions == [COERCION_TOPICS_TRUNCATED]

    def test_coercions_are_bookkeeping_not_record(self):
        data = good_record()
        data["topics"] = [f"topic {i}" for i in range(MAX_TOPICS + 1)]
        rec = validate_record(data)
        assert "coercions" not in rec.to_dict()
        assert set(rec.to_dict()) == set(SKIM_SCHEMA["required"])
        assert rec == validate_record(rec.to_dict())          # equality ignores coercions
        assert validate_record(rec.to_dict()).coercions == []  # the trimmed record is clean

    def test_structural_errors_are_never_coerced(self):
        data = good_record()
        data["salience"] = 4
        with pytest.raises(SkimValidationError):
            validate_record(data)
        data = good_record()
        data["decisions"][0]["quote"] = ""
        with pytest.raises(SkimValidationError):
            validate_record(data)

    def test_strict_mode_rejects_what_coercion_trims(self):
        for mutate in (lambda d: d.__setitem__("summary", "s" * (SUMMARY_HARD_LIMIT_CHARS + 1)),
                       lambda d: d["decisions"][0].__setitem__("quote", "q" * (MAX_QUOTE_CHARS + 1)),
                       lambda d: d.__setitem__("topics", [f"t{i}" for i in range(MAX_TOPICS + 1)])):
            data = good_record()
            mutate(data)
            with pytest.raises(SkimValidationError):
                validate_record(data, coerce=False)
            assert validate_record(data).coerced
        # Between the soft and the hard summary limit: strict rejects, coercion keeps as-is.
        data = good_record()
        data["summary"] = "s" * (MAX_SUMMARY_CHARS + 1)
        with pytest.raises(SkimValidationError):
            validate_record(data, coerce=False)
        assert not validate_record(data).coerced


class TestValidateRejects:
    def _rejects(self, data, match, **kw):
        with pytest.raises(SkimValidationError, match=match) as excinfo:
            validate_record(data, **kw)
        assert isinstance(excinfo.value, ValueError)

    def test_missing_quote(self):
        data = good_record()
        del data["decisions"][0]["quote"]
        self._rejects(data, r"decisions\[0\] is missing its verbatim quote")

    def test_empty_quote(self):
        data = good_record()
        data["open_questions"][0]["quote"] = ""
        self._rejects(data, r"open_questions\[0\].quote must not be empty")

    def test_quote_too_long(self):
        data = good_record()
        data["decisions"][0]["quote"] = "q" * (MAX_QUOTE_CHARS + 1)
        self._rejects(data, r"decisions\[0\].quote is 161 characters; the limit is 160", coerce=False)

    def test_too_many_topics(self):
        data = good_record()
        data["topics"] = [f"topic {i}" for i in range(MAX_TOPICS + 1)]
        self._rejects(data, r"topics has 6 items; the limit is 5", coerce=False)

    def test_salience_out_of_range(self):
        data = good_record()
        data["salience"] = 4
        self._rejects(data, r"salience must be between 0 and 3, got 4")

    def test_novelty_must_be_integer(self):
        data = good_record()
        data["novelty"] = "2"
        self._rejects(data, r"novelty must be an integer 0-3")
        data["novelty"] = True
        self._rejects(data, r"novelty must be an integer 0-3")

    def test_summary_too_long(self):
        data = good_record()
        data["summary"] = "s" * (MAX_SUMMARY_CHARS + 1)
        self._rejects(data, r"summary is 201 characters; the limit is 200", coerce=False)

    def test_missing_and_unknown_fields(self):
        data = good_record()
        del data["doc_kind"]
        self._rejects(data, r"missing field\(s\): \['doc_kind'\]")
        data = good_record()
        data["confidence"] = "high"
        self._rejects(data, r"unknown field\(s\): \['confidence'\]")

    def test_bad_doc_kind_and_non_object(self):
        data = good_record()
        data["doc_kind"] = "tweet"
        self._rejects(data, r"doc_kind must be one of")
        self._rejects([good_record()], r"must be a JSON object")
        self._rejects(None, r"must be a JSON object")

    def test_people_must_be_non_empty_strings(self):
        data = good_record()
        data["people"] = ["Priya Natarajan", ""]
        self._rejects(data, r"people\[1\] must not be empty")
        data["people"] = "Priya Natarajan"
        self._rejects(data, r"people must be an array")

    def test_quoted_item_with_extra_field(self):
        data = good_record()
        data["decisions"][0]["confidence"] = 0.9
        self._rejects(data, r"decisions\[0\] has unknown field")

    def test_input_is_not_mutated(self):
        data = good_record()
        snapshot = copy.deepcopy(data)
        validate_record(data)
        assert data == snapshot
