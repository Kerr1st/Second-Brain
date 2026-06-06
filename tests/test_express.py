"""Tests for src/express.py — the Express delivery layer (P1 briefing).

Pure-logic tests (editor ranking, LLM-pass parsing, fallback, rendering) run with
no DB. The composer tests use the real test_db/clean_tables fixtures to validate
the SQL against an actual schema.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import src.db as db
from src import express


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _item(kind, title, detail="detail", cross_project=False, created_at=None, **meta):
    m = {"cross_project": cross_project, **meta}
    return {
        "kind": kind,
        "id": title,
        "title": title,
        "detail": detail,
        "created_at": created_at or datetime.now(timezone.utc),
        "meta": m,
    }


# --------------------------------------------------------------------------- #
# Editor: deterministic ordering (no LLM)
# --------------------------------------------------------------------------- #

def test_deterministic_edit_leads_with_cross_project_insight():
    items = [
        _item("insight", "plain insight", cross_project=False),
        _item("contradiction", "a conflict"),
        _item("insight", "cross insight", cross_project=True),
    ]
    out = express._deterministic_edit(items)
    assert out["editor"] == "deterministic"
    assert out["items"][0]["title"] == "cross insight"  # cross-project outranks all
    assert out["items"][0]["headline"] == "cross insight"


def test_deterministic_edit_orders_contradiction_above_resurface_and_digest():
    items = [
        _item("digest", "weekly"),
        _item("resurface", "forgotten"),
        _item("contradiction", "conflict"),
    ]
    titles = [it["title"] for it in express._deterministic_edit(items)["items"]]
    assert titles == ["conflict", "forgotten", "weekly"]


def test_deterministic_edit_caps_at_max_items():
    items = [_item("insight", f"i{n}") for n in range(8)]
    assert len(express._deterministic_edit(items)["items"]) == express.MAX_ITEMS


# --------------------------------------------------------------------------- #
# Editor: LLM pass
# --------------------------------------------------------------------------- #

def test_edit_briefing_applies_llm_headlines_and_order():
    items = [_item("insight", "first"), _item("contradiction", "second")]
    invoker = MagicMock()
    invoker.invoke.return_value = {
        "output": {"lead": 1, "items": [
            {"idx": 1, "headline": "Punchy headline for second"},
            {"idx": 0, "headline": "Punchy headline for first"},
        ]},
        "raw": "{}",
    }
    out = express.edit_briefing({"items": items}, invoker=invoker)
    assert out["editor"] == "llm"
    assert out["lead_idx"] == 0
    assert out["ranked"][0]["title"] == "second"
    assert out["ranked"][0]["headline"] == "Punchy headline for second"
    assert out["ranked"][1]["title"] == "first"


def test_edit_briefing_caps_llm_output_at_max_items():
    items = [_item("insight", f"i{n}") for n in range(8)]
    invoker = MagicMock()
    invoker.invoke.return_value = {
        "output": {"items": [{"idx": n, "headline": f"h{n}"} for n in range(8)]},
        "raw": "{}",
    }
    out = express.edit_briefing({"items": items}, invoker=invoker)
    assert len(out["ranked"]) == express.MAX_ITEMS


def test_edit_briefing_ignores_out_of_range_indices():
    items = [_item("insight", "only")]
    invoker = MagicMock()
    invoker.invoke.return_value = {
        "output": {"items": [{"idx": 5, "headline": "bad"}, {"idx": 0, "headline": "good"}]},
        "raw": "{}",
    }
    out = express.edit_briefing({"items": items}, invoker=invoker)
    assert [it["headline"] for it in out["ranked"]] == ["good"]


def test_edit_briefing_falls_back_to_deterministic_on_llm_error():
    items = [_item("contradiction", "c"), _item("insight", "cross", cross_project=True)]
    invoker = MagicMock()
    invoker.invoke.side_effect = RuntimeError("kiro-cli unavailable")
    out = express.edit_briefing({"items": items}, invoker=invoker)
    assert out["editor"] == "deterministic"
    assert out["ranked"][0]["title"] == "cross"  # cross-project still leads


def test_edit_briefing_handles_empty():
    out = express.edit_briefing({"items": []}, invoker=MagicMock())
    assert out["editor"] == "empty"
    assert out["ranked"] == []
    assert out["lead_idx"] is None


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def test_render_markdown_marks_lead_and_lists_all_headlines():
    items = [_item("contradiction", "c", detail="d1"), _item("insight", "i", detail="d2")]
    briefing = express.edit_briefing({"items": items}, invoker=MagicMock(
        invoke=MagicMock(return_value={"output": {"items": [
            {"idx": 0, "headline": "Lead headline"},
            {"idx": 1, "headline": "Second headline"},
        ]}, "raw": "{}"})
    ))
    md = express.render_markdown(briefing)
    assert "# Your briefing" in md
    assert "★" in md  # lead marker
    assert "Lead headline" in md and "Second headline" in md
    assert "d1" in md and "d2" in md  # detail layer present


def test_render_markdown_empty_is_graceful():
    md = express.render_markdown({"ranked": []})
    assert "quiet" in md.lower()


# --------------------------------------------------------------------------- #
# Composer (DB-backed)
# --------------------------------------------------------------------------- #

def test_compose_gathers_all_sources(clean_tables, sample_memory_factory):
    # Cross-project dream-cycle insight (cross via strategy name).
    sample_memory_factory(
        type="insight", title="DC cross insight", content="connects A and B",
        tags=["dream-cycle", "assimilation"],
        metadata={"dream_cycle": True, "strategy": "cross_project_collision",
                  "source_memories": [], "confidence": "high"},
    )
    # A contradiction between two decisions.
    a = sample_memory_factory(type="decision", title="Old decision", content="do X at 10pm")
    b = sample_memory_factory(type="decision", title="New decision", content="do X at 4pm")
    db.create_relationship(a, b, "contradicts", "New overrides old")
    # Latest weekly digest.
    sample_memory_factory(type="synthesis", title="This week", content="worked on Express",
                          source_type="weekly_digest")

    b = express.compose_briefing()
    counts = b["counts"]
    assert counts.get("insight") == 1
    assert counts.get("contradiction") == 1
    assert counts.get("digest") == 1

    insight = next(i for i in b["items"] if i["kind"] == "insight")
    assert insight["meta"]["cross_project"] is True

    contradiction = next(i for i in b["items"] if i["kind"] == "contradiction")
    assert contradiction["title"] == "Old decision"
    assert "New overrides old" in contradiction["detail"]


def test_compose_resurfaces_old_high_value_memory(clean_tables, sample_memory_factory):
    mid = sample_memory_factory(type="synthesis", title="Forgotten principle",
                                content="a deeply-held principle")
    # Backdate so it qualifies as "old + not accessed recently"; give it prior value.
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memories SET created_at = now() - interval '90 days', "
                "access_count = 5, last_accessed_at = NULL WHERE id = %s", (mid,))
        conn.commit()

    items = express.compose_briefing()["items"]
    resurfaced = [i for i in items if i["kind"] == "resurface"]
    assert any(i["title"] == "Forgotten principle" for i in resurfaced)


def test_compose_resurface_suppresses_recently_surfaced(clean_tables, sample_memory_factory):
    mid = sample_memory_factory(type="synthesis", title="Recently surfaced", content="x")
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memories SET created_at = now() - interval '90 days', "
                "metadata = jsonb_set(coalesce(metadata,'{}'), '{express_last_surfaced}', "
                "to_jsonb(now()::text)) WHERE id = %s", (mid,))
        conn.commit()

    items = express.compose_briefing()["items"]
    assert not any(i["title"] == "Recently surfaced" for i in items)
