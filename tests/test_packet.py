"""Tests for src/dream_cycle/packet.py — context packets for tool-less backends."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch
import pytest

from src.dream_cycle import packet
from src.dream_cycle.packet import (
    MAX_CONTENT_CHARS_PER_MEMORY,
    MAX_TOTAL_CONTENT_CHARS,
    build_packet,
    format_sources_for_evaluator,
    load_memories_by_ids,
)

UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"


def _mem(mid, content="body", title="T"):
    return {"id": mid, "title": title, "type": "notes", "content": content,
            "created_at": datetime(2026, 3, 1, tzinfo=timezone.utc), "tags": ["x"], "project": None}


class TestLoader:
    def test_filters_non_uuids_before_querying(self):
        with patch.object(packet, "get_memories_by_ids", return_value=[_mem(UUID_A)]) as db_call:
            rows = load_memories_by_ids(["m1", UUID_A, None, 42])
        db_call.assert_called_once_with([UUID_A])
        assert [r["id"] for r in rows] == [UUID_A]

    def test_no_valid_ids_means_no_query(self):
        with patch.object(packet, "get_memories_by_ids") as db_call:
            assert load_memories_by_ids(["m1", "m2"]) == []
        db_call.assert_not_called()

    def test_database_failure_degrades_to_empty(self):
        with patch.object(packet, "get_memories_by_ids", side_effect=OSError("db down")):
            assert load_memories_by_ids([UUID_A]) == []

    def test_programming_failure_propagates(self):
        with patch.object(packet, "get_memories_by_ids", side_effect=AttributeError("bug")):
            with pytest.raises(AttributeError):
                load_memories_by_ids([UUID_A])


class TestBuildPacket:
    def test_entry_shape_and_iso_dates(self):
        [entry] = build_packet([_mem(UUID_A, content="hello", title="Hi")])
        assert entry == {
            "id": UUID_A, "title": "Hi", "type": "notes", "project": None, "tags": ["x"],
            "created_at": "2026-03-01T00:00:00+00:00", "content": "hello",
        }

    def test_per_memory_truncation(self):
        [entry] = build_packet([_mem(UUID_A, content="x" * (MAX_CONTENT_CHARS_PER_MEMORY + 500))])
        assert entry["content"].startswith("x" * MAX_CONTENT_CHARS_PER_MEMORY)
        assert entry["content"].endswith("[truncated]")

    def test_total_budget_enforced(self):
        n = MAX_TOTAL_CONTENT_CHARS // MAX_CONTENT_CHARS_PER_MEMORY + 2
        entries = build_packet([_mem(f"{i:08d}-0000-4000-8000-000000000000", content="y" * MAX_CONTENT_CHARS_PER_MEMORY)
                                for i in range(n)])
        contents = [e["content"] for e in entries]
        assert len(entries) == n  # every memory keeps its id/title
        assert sum(len(c) for c in contents) <= MAX_TOTAL_CONTENT_CHARS + len(" …[truncated: packet budget]")
        assert sum(c.endswith("[truncated: packet budget]") for c in contents) == 1
        assert contents[-1] == ""  # past the budget: titles only


class TestEvaluatorSources:
    def test_renders_only_cached_cited_sources(self):
        cache = {UUID_A: _mem(UUID_A, content="seven am focus block", title="Focus")}
        text = format_sources_for_evaluator(cache, [UUID_A, UUID_B, "not-a-uuid"])
        assert text.startswith("Source memory contents:")
        assert "seven am focus block" in text and "Focus" in text
        assert UUID_B not in text

    def test_empty_when_nothing_cached(self):
        assert format_sources_for_evaluator({}, [UUID_A]) == ""
        assert format_sources_for_evaluator({UUID_A: _mem(UUID_A)}, []) == ""
