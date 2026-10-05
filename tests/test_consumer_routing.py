"""Hermetic regressions for the corpus-facing summary and delivery consumers."""
import hashlib
import json
from unittest.mock import Mock, patch

import pytest

from src import express
from src.backends.base import ModelRefusalError
from src.backends.cost import SpendCapExceeded, SpendTracker, estimate_cost_usd
from src.backends.resolver import DISTILL_ROLE, RoleBackend
import scripts.distill_sessions as ds
import scripts.redistill_capped as rd
import scripts.express_push as push
import scripts.weekly_digest as wd

SPEC = RoleBackend(DISTILL_ROLE, "bedrock", "global.anthropic.claude-sonnet-5", "medium")
USAGE = {"inputTokens": 100, "outputTokens": 20}


@pytest.fixture
def distiller(monkeypatch):
    invoker = Mock()
    invoker.invoke.return_value = {"output": [], "usage": USAGE}
    monkeypatch.setattr(ds, "resolve_stage", lambda *a, **k: (invoker, SPEC))
    monkeypatch.setattr(ds, "get_processed_source_urls", lambda *_: set())
    monkeypatch.setattr(ds, "fetch_sessions", lambda *a, **k: [("synthetic://s1", "kiro_cli_chat", "synthetic body")])
    monkeypatch.setattr(ds, "generate_embedding", Mock(return_value=[0.1]))
    monkeypatch.setattr(ds, "create_memory", Mock(return_value="new-memory"))
    monkeypatch.setattr(ds, "mark_refused", Mock())
    return invoker


def test_extraction_and_contradiction_share_one_meter(distiller, monkeypatch):
    distiller.invoke.side_effect = [
        {"output": [{"type": "decision", "content": "Prefer option two", "title": "Choice"}], "usage": USAGE},
        {"output": {"contradicts": True, "reason": "choice changed"}, "usage": USAGE},
    ]
    monkeypatch.setattr(ds, "_find_prior_decision", lambda *a: {"id": "prior", "content": "Prefer one", "title": "Old"})
    monkeypatch.setattr(ds, "create_relationship", Mock())
    tracker = SpendTracker(1.0)
    stats = ds.distill(tracker=tracker)
    assert tracker.calls == 2
    assert tracker.total_usd == pytest.approx(2 * estimate_cost_usd(SPEC.model, USAGE))
    assert stats["contradictions"] == stats["memories"] == 1
    assert stats["failed"] == 0
    assert [c.kwargs["stage"] for c in distiller.invoke.call_args_list] == ["distill", "distill_contradiction"]


def test_contradiction_cap_stops_remaining_writes_and_sessions(distiller, monkeypatch):
    monkeypatch.setattr(ds, "fetch_sessions", lambda *a, **k: [("s1", "chat", "one"), ("s2", "chat", "two")])
    distiller.invoke.side_effect = [
        {"output": [{"type": "decision", "content": "first"}, {"type": "insight", "content": "second"}], "usage": USAGE},
        {"output": {"contradicts": False}, "usage": {"inputTokens": 1000000, "outputTokens": 1000000}},
    ]
    monkeypatch.setattr(ds, "_find_prior_decision", lambda *a: {"id": "prior", "content": "old"})
    stats = ds.distill(tracker=SpendTracker(0.1))
    assert stats["aborted"] and stats["failed"] == 1
    assert stats["memories"] == 1
    assert distiller.invoke.call_count == 2
    assert ds.create_memory.call_count == 1
    assert ds.generate_embedding.call_count == 1


def test_distill_dry_run_uses_no_provider_embedding_or_write(distiller, monkeypatch):
    monkeypatch.setattr(ds, "resolve_stage", Mock(side_effect=AssertionError("provider resolution")))
    stats = ds.distill(dry_run=True)
    assert stats["dry_run"] and stats["planned_sessions"] == 1
    assert stats["sessions"] == stats["memories"] == 0
    distiller.invoke.assert_not_called()
    ds.generate_embedding.assert_not_called()
    ds.create_memory.assert_not_called()


def test_explicit_refusal_is_recorded_and_charged(distiller):
    refusal = ModelRefusalError("provider content filter")
    refusal.model_response_received = True
    refusal.usage = USAGE
    distiller.invoke.side_effect = refusal
    tracker = SpendTracker(1.0)
    stats = ds.distill(tracker=tracker)
    assert stats["refused"] == stats["failed"] == 1
    assert tracker.calls == 1 and stats["cost_usd"] > 0
    ds.mark_refused.assert_called_once_with("synthetic://s1", "kiro_cli_chat", "synthetic body", SPEC)
    ds.create_memory.assert_not_called()


def test_malformed_output_is_failure_not_refusal_or_empty(distiller):
    distiller.invoke.return_value = {"output": {"wrong": "shape"}, "usage": USAGE}
    stats = ds.distill()
    assert stats["failed"] == 1 and stats["empty"] == stats["refused"] == 0
    ds.mark_refused.assert_not_called()


def test_transport_failure_reserves_spend_and_the_next_session_still_runs(distiller, monkeypatch):
    """A throttle/timeout on one session is not a reason to abandon the night:
    the attempt is charged a conservative reservation and the loop continues."""
    monkeypatch.setattr(ds, "fetch_sessions", lambda *a, **k: [("s1", "chat", "one"), ("s2", "chat", "two")])
    distiller.invoke.side_effect = TimeoutError("transport")
    tracker = SpendTracker(1.0)
    stats = ds.distill(tracker=tracker)
    assert distiller.invoke.call_count == 2
    assert stats["failed"] == 2 and not stats["aborted"] and not stats["spend_unknown"]
    assert tracker.reserved_calls == 2 and tracker.reserved_usd > 0
    assert tracker.total_usd == pytest.approx(tracker.reserved_usd)
    assert tracker.blocked_reason is None
    ds.mark_refused.assert_not_called()


def test_transport_reservations_are_bounded_by_the_cap(distiller, monkeypatch):
    """Repeated failures cannot retry forever: reservations accumulate against
    the cap and the loop stops with SpendCapExceeded like any other overspend."""
    monkeypatch.setattr(ds, "fetch_sessions",
                        lambda *a, **k: [(f"s{i}", "chat", "x" * 1000) for i in range(50)])
    distiller.invoke.side_effect = TimeoutError("transport")
    tracker = SpendTracker(0.05)
    stats = ds.distill(tracker=tracker)
    assert stats["aborted"] and not stats["spend_unknown"]
    assert 1 <= distiller.invoke.call_count < 50
    assert tracker.total_usd > tracker.cap_usd


def test_refusal_marker_is_input_and_provider_scoped():
    scope = {"role": DISTILL_ROLE, "backend": SPEC.backend, "model": SPEC.model, "operation": "distill"}
    metadata = {"distill_refusal": {**scope, "do_not_send": True,
                "input_sha256": hashlib.sha256(b"old body").hexdigest()}}
    assert ds.refusal_matches(metadata, scope, "old body")
    assert not ds.refusal_matches(metadata, scope, "revised body")
    assert not ds.refusal_matches(metadata, {**scope, "model": "another-model"}, "old body")
    assert not ds.refusal_matches(metadata, {**scope, "operation": "redistill"}, "old body")


def test_refusal_is_durable_without_copying_refusal_text():
    with patch.object(ds, "get_connection") as connection:
        ds.mark_refused("synthetic://session", "kiro_cli_chat", "source body", SPEC)
    conn = connection.return_value.__enter__.return_value
    cur = conn.cursor.return_value.__enter__.return_value
    sql, args = cur.execute.call_args.args
    marker = json.loads(args[0])["distill_refusal"]
    assert marker["do_not_send"] is True
    assert marker["input_sha256"] == hashlib.sha256(b"source body").hexdigest()
    assert "source body" not in args[0]
    assert args[1:] == ("synthetic://session", "kiro_cli_chat", "source body")
    assert "content=%s" in sql and "COALESCE(metadata" in sql
    conn.commit.assert_called_once()


def test_fetch_skips_only_unchanged_refused_input():
    scope = {"role": DISTILL_ROLE, "backend": SPEC.backend, "model": SPEC.model, "operation": "distill"}
    marker = {"distill_refusal": {**scope, "do_not_send": True,
              "input_sha256": hashlib.sha256(b"body").hexdigest()}}
    with patch.object(ds, "get_connection") as connection:
        cur = connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [("same", "chat", "body", marker), ("changed", "chat", "new body", marker)]
        sessions = ds.fetch_sessions(("chat",), 5, set(), refusal_scope=scope)
    assert sessions == [("changed", "chat", "new body")]


def test_distill_main_reports_partial_failure(monkeypatch):
    monkeypatch.setattr("sys.argv", ["distill_sessions.py"])
    monkeypatch.setattr(ds, "distill", lambda **k: {"failed": 1, "aborted": False})
    assert ds.main() == 1


def test_redistill_dry_run_is_selection_only(monkeypatch):
    monkeypatch.setattr(rd, "find_capped_sessions", lambda **k: ["one", "two"])
    no_call = Mock(side_effect=AssertionError("dry run invoked work"))
    for name in ("resolve_stage", "fetch_transcript", "generate_embedding", "create_memory"):
        monkeypatch.setattr(rd, name, no_call)
    result = rd.redistill(dry_run=True)
    assert result["planned_sessions"] == 2 and result["new_items"] == 0
    no_call.assert_not_called()


def test_redistill_metered_and_partial_write_failure_is_truthful(monkeypatch):
    invoker = Mock()
    invoker.invoke.return_value = {"output": [{"type": "insight", "content": "additional lesson"}], "usage": USAGE}
    monkeypatch.setattr(rd, "resolve_stage", lambda *a, **k: (invoker, SPEC))
    monkeypatch.setattr(rd, "find_capped_sessions", lambda **k: ["one"])
    monkeypatch.setattr(rd, "fetch_transcript", lambda s: ("body", {}, "chat"))
    monkeypatch.setattr(rd, "session_state", lambda s: ([], 4, "chat"))
    monkeypatch.setattr(rd, "generate_embedding", lambda *_: [0.1])
    monkeypatch.setattr(rd, "max_sim_existing", lambda *_: 0.0)
    monkeypatch.setattr(rd, "create_memory", Mock(side_effect=RuntimeError("write failed")))
    tracker = SpendTracker(1.0)
    stats = rd.redistill(tracker=tracker)
    assert stats["failed"] == 1 and stats["new_items"] == 0
    assert tracker.calls == 1 and stats["cost_usd"] > 0


def test_editor_deterministic_path_does_not_resolve_backend(monkeypatch):
    monkeypatch.setattr("src.backends.resolver.resolve_stage", Mock(side_effect=AssertionError("resolved")))
    item = {"kind": "insight", "title": "Synthetic insight", "meta": {}, "created_at": None}
    result = express.edit_briefing({"items": [item]}, use_llm=False)
    assert result["editor"] == "deterministic"


def test_unconfigured_email_never_composes_or_edits(monkeypatch):
    monkeypatch.setattr("sys.argv", ["express_push.py"])
    monkeypatch.setattr(push, "_read_last_pushed", lambda: None)
    monkeypatch.setattr(express, "should_push", lambda **k: {"push": True, "run_id": "synthetic-run", "reason": "new"})
    monkeypatch.setattr(express, "email_configured", lambda: False)
    compose = Mock(side_effect=AssertionError("composed"))
    monkeypatch.setattr(express, "compose_briefing", compose)
    assert push.main() == 0
    compose.assert_not_called()


def test_push_dry_run_is_deterministic_and_never_sends(monkeypatch):
    monkeypatch.setattr("sys.argv", ["express_push.py", "--dry-run"])
    monkeypatch.setattr(push, "_read_last_pushed", lambda: None)
    monkeypatch.setattr(express, "should_push", lambda **k: {"push": True, "run_id": "synthetic-run", "reason": "new"})
    monkeypatch.setattr(express, "compose_briefing", lambda: {"items": []})
    editor = Mock(return_value={"ranked": []})
    monkeypatch.setattr(express, "edit_briefing", editor)
    sender = Mock(side_effect=AssertionError("sent"))
    monkeypatch.setattr(express, "send_email", sender)
    assert push.main() == 0
    assert editor.call_args.kwargs == {"use_llm": False}
    sender.assert_not_called()


def test_mcp_brief_invalid_window_returns_validation_error(monkeypatch):
    from src.mcp_server import memory_brief
    compose = Mock(side_effect=AssertionError("composed invalid request"))
    monkeypatch.setattr(express, "compose_briefing", compose)
    assert "error" in json.loads(memory_brief(window_days="nonsense"))
    compose.assert_not_called()


def test_weekly_digest_rejects_forbidden_override_before_adapter():
    resolver = Mock()
    resolver.spec_for.return_value = RoleBackend(wd.DIGEST_ROLE, "bedrock", "us.amazon.nova-2-lite-v1:0")
    with patch("src.backends.resolver.DEFAULT_ADAPTERS", {"bedrock": Mock()}) as adapters:
        with pytest.raises(wd.DigestBackendError, match="Fable|fable"):
            wd.resolve_digest_backend(resolver, model_override="global.anthropic.claude-fable-5-1")
    adapters["bedrock"].assert_not_called()


def test_redistill_missing_source_is_failure(monkeypatch):
    monkeypatch.setattr(rd, "find_capped_sessions", lambda **k: ["missing"])
    monkeypatch.setattr(rd, "resolve_stage", lambda *a, **k: (Mock(), SPEC))
    monkeypatch.setattr(rd, "fetch_transcript", lambda s: (None, None, None))
    stats = rd.redistill()
    assert stats["no_transcript"] == stats["failed"] == 1
    assert stats["sessions"] == 0
