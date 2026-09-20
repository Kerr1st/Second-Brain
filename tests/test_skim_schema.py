"""Tests for src/skim/schema.py — the skim record schema, dataclass and validation.

All fixtures are synthetic documents written for these tests (no corpus content).
"""

from __future__ import annotations

import copy
import json

import pytest

from src.skim.schema import (
    DOC_KINDS,
    MAX_QUOTE_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TOPICS,
    SKIM_SCHEMA,
    QuotedItem,
    SkimRecord,
    SkimValidationError,
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
        validate_record(data)


class TestValidateRejects:
    def _rejects(self, data, match):
        with pytest.raises(SkimValidationError, match=match) as excinfo:
            validate_record(data)
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
        self._rejects(data, r"decisions\[0\].quote is 161 characters; the limit is 160")

    def test_too_many_topics(self):
        data = good_record()
        data["topics"] = [f"topic {i}" for i in range(MAX_TOPICS + 1)]
        self._rejects(data, r"topics has 6 items; the limit is 5")

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
        self._rejects(data, r"summary is 201 characters; the limit is 200")

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
