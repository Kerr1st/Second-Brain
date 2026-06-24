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


# --------------------------------------------------------------------------- #
# P2: should_push (DB-backed)
# --------------------------------------------------------------------------- #

import src.dream_cycle_db as dcdb  # noqa: E402


@pytest.fixture()
def clean_dream_cycle(test_db):
    """Truncate dream-cycle tables around a test (clean_tables doesn't touch them)."""
    def _clean():
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM dream_cycle_candidates")
                cur.execute("DELETE FROM dream_cycle_runs")
            conn.commit()
    _clean()
    yield
    _clean()


def _completed_run_with(candidate: dict, verdict: str = "ACCEPTED") -> str:
    run_id = dcdb.create_run("scheduled")
    dcdb.store_candidate(run_id, candidate, {}, verdict)
    dcdb.complete_run(run_id, {"candidates_generated": 1, "candidates_accepted": 1,
                               "candidates_rejected": 0}, "digest")
    return run_id


def test_should_push_true_on_contradiction(clean_dream_cycle):
    _completed_run_with({
        "title": "Reversal detected",
        "relationships": [{"relation_type": "contradicts", "target_id": "x"}],
    })
    d = express.should_push()
    assert d["push"] is True
    assert any(t["kind"] == "contradiction" for t in d["triggers"])


def test_should_push_true_on_cross_project_strategy(clean_dream_cycle):
    _completed_run_with({
        "title": "A connection across projects",
        "strategy_that_found_it": "cross_project_collision",
        "source_memories": [],
    })
    d = express.should_push()
    assert d["push"] is True
    assert any(t["kind"] == "cross_project" for t in d["triggers"])


def test_should_push_false_when_routine(clean_dream_cycle):
    _completed_run_with({
        "title": "A routine single-project insight",
        "strategy_that_found_it": "depth_gradient",
        "source_memories": [],
        "relationships": [{"relation_type": "extends", "target_id": "x"}],
    })
    d = express.should_push()
    assert d["push"] is False


def test_should_push_respects_already_pushed(clean_dream_cycle):
    run_id = _completed_run_with({
        "title": "Reversal", "relationships": [{"relation_type": "contradicts", "target_id": "x"}],
    })
    assert express.should_push(last_pushed_run_id=run_id)["push"] is False


def test_should_push_false_when_no_runs(clean_dream_cycle):
    d = express.should_push()
    assert d["push"] is False
    assert d["run_id"] is None


# --------------------------------------------------------------------------- #
# P2: render_email + send_email
# --------------------------------------------------------------------------- #

def _edited(items, invoke_return):
    invoker = MagicMock()
    invoker.invoke.return_value = {"output": invoke_return, "raw": "{}"}
    return express.edit_briefing({"items": items}, invoker=invoker)


def test_render_email_has_subject_headlines_and_detail():
    briefing = _edited(
        [_item("contradiction", "c", detail="conflicting detail"),
         _item("insight", "i", detail="insight detail", cross_project=True, projects=["P1", "P2"])],
        {"items": [{"idx": 0, "headline": "Lead headline"}, {"idx": 1, "headline": "Second headline"}]},
    )
    em = express.render_email(briefing)
    assert em["subject"].startswith("🧠")
    assert "Lead headline" in em["html"] and "<h3" in em["html"]
    assert "conflicting detail" in em["html"]
    assert "Lead headline" in em["text"] and "Second headline" in em["text"]


def test_render_email_empty_is_graceful():
    em = express.render_email({"ranked": []})
    assert "nothing" in em["subject"].lower() or "nothing" in em["text"].lower()


def test_send_email_requires_config(monkeypatch):
    for var in (express.ENV_TO, express.ENV_FROM, express.ENV_PASSWORD):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        express.send_email("s", "<p>h</p>", "t")


def test_send_email_uses_transport():
    sent = {}
    smtp = MagicMock()
    smtp.sendmail.side_effect = lambda frm, to, msg: sent.update(frm=frm, to=to, msg=msg)

    ok = express.send_email(
        "Subject line", "<h3>HTML</h3>", "plain text",
        to="me@example.com", sender="me@example.com", password="app-pw",
        smtp_factory=lambda: smtp,
    )
    assert ok is True
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("me@example.com", "app-pw")
    assert sent["to"] == ["me@example.com"]

    import email as email_lib
    parsed = email_lib.message_from_string(sent["msg"])
    assert parsed["Subject"] == "Subject line"
    bodies = "\n".join(
        p.get_payload(decode=True).decode("utf-8")
        for p in parsed.walk()
        if p.get_content_type() in ("text/plain", "text/html")
    )
    assert "HTML" in bodies and "plain text" in bodies


# --------------------------------------------------------------------------- #
# Feedback loop (delivery preferences)
# --------------------------------------------------------------------------- #

@pytest.fixture()
def clean_express_feedback(test_db):
    def _c():
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM express_feedback")
            conn.commit()
    _c()
    yield
    _c()


def test_classify_target():
    assert express._classify_target("insight") == ("kind", "insight")
    assert express._classify_target("Contradiction") == ("kind", "contradiction")
    assert express._classify_target("aba591ea-cb15-477f-9730-9e267561c54f")[0] == "item"
    assert express._classify_target("abcd1234")[0] == "item"
    assert express._classify_target("kiro") == ("topic", "kiro")
    assert express._classify_target("my-project") == ("topic", "my-project")


def test_apply_prefs_filters_muted_kind_item_topic():
    items = [
        _item("insight", "abcd1234ef", topics=["P1"]),
        _item("resurface", "r1"),
        _item("contradiction", "c1", topics=["secret"]),
    ]
    prefs = {
        "muted": {"item": {"abcd1234"}, "kind": {"resurface"}, "topic": {"secret"}},
        "weight": {"item": {}, "kind": {}, "topic": {}},
    }
    out = express._apply_prefs(items, prefs)
    assert out == []  # insight muted by item-prefix, resurface by kind, contradiction by topic


def test_apply_prefs_annotates_soft_weight():
    items = [_item("insight", "i1", topics=["P1"])]
    prefs = {
        "muted": {"item": set(), "kind": set(), "topic": set()},
        "weight": {"item": {}, "kind": {"insight": 1.0}, "topic": {"P1": 0.5}},
    }
    out = express._apply_prefs(items, prefs)
    assert out[0]["meta"]["pref_weight"] == 1.5


def test_deterministic_edit_respects_pref_weight():
    items = [_item("contradiction", "c"), _item("resurface", "r")]
    items[1]["meta"]["pref_weight"] = 5.0  # heavily boost the normally-lower resurface
    out = express._deterministic_edit(items)
    assert out["items"][0]["title"] == "r"


def test_pref_hint_summarizes_weights():
    prefs = {
        "muted": {"item": set(), "kind": set(), "topic": set()},
        "weight": {"item": {}, "kind": {"contradiction": 1.0}, "topic": {"kiro": -0.6}},
    }
    hint = express._pref_hint(prefs)
    assert "contradiction" in hint and "kiro" in hint


def test_record_list_remove_feedback(clean_express_feedback):
    express.record_feedback("insight", "useful")
    fb = express.list_feedback()
    assert len(fb) == 1 and fb[0]["target_key"] == "insight" and fb[0]["signal"] == "useful"
    assert express.remove_feedback("insight") == 1
    assert express.list_feedback() == []


def test_record_feedback_upserts_latest(clean_express_feedback):
    express.record_feedback("resurface", "less")
    express.record_feedback("resurface", "mute")  # same target → replace
    fb = express.list_feedback()
    assert len(fb) == 1 and fb[0]["signal"] == "mute"


def test_record_feedback_rejects_bad_signal(clean_express_feedback):
    with pytest.raises(ValueError):
        express.record_feedback("insight", "love-it")


def test_load_prefs_shape(clean_express_feedback):
    express.record_feedback("insight", "mute")
    express.record_feedback("kiro", "less")  # topic
    p = express.load_prefs()
    assert "insight" in p["muted"]["kind"]
    assert p["weight"]["topic"]["kiro"] < 0


def test_compose_respects_muted_kind(clean_express_feedback, sample_memory_factory):
    sample_memory_factory(
        type="insight", title="DC insight", content="x",
        tags=["dream-cycle", "assimilation"],
        metadata={"dream_cycle": True, "strategy": "cross_project_collision",
                  "source_memories": [], "confidence": "high"},
    )
    assert any(i["kind"] == "insight" for i in express.compose_briefing()["items"])
    express.record_feedback("insight", "mute")
    assert not any(i["kind"] == "insight" for i in express.compose_briefing()["items"])
