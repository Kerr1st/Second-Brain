"""Tests for the chat-session distiller (Refactor P1)."""

from unittest.mock import Mock, patch

import scripts.distill_sessions as ds
from scripts.distill_sessions import valid_items, build_user_message, MAX_CHARS, DISTILL_TYPES


def test_valid_items_filters_malformed():
    raw = [
        {"type": "decision", "title": "x", "content": "WHAT: a WHY: b"},
        {"type": "insight", "content": "y"},
        {"type": "chatter", "content": "skip"},   # wrong type
        {"type": "decision", "content": ""},        # empty content
        "not a dict",                                # junk
        {"title": "no type"},                        # missing type
    ]
    out = valid_items(raw)
    assert len(out) == 2
    assert {it["type"] for it in out} <= DISTILL_TYPES


def test_valid_items_handles_none_and_empty():
    assert valid_items(None) == []
    assert valid_items([]) == []


def test_build_user_message_frames_and_truncates():
    msg = build_user_message("hello world")
    assert "do not reply" in msg.lower()   # guards the conversational-response bug
    assert '"""' in msg                     # transcript is delimited
    assert "hello world" in msg
    big = build_user_message("x" * (MAX_CHARS + 5000))
    assert "x" * MAX_CHARS in big and "x" * (MAX_CHARS + 1) not in big  # capped at MAX_CHARS


# --- Distill-time contradiction detection (decision supersession, detect-and-link) ---

def _decision(mid, title, content, distilled_from=None):
    return {"id": mid, "title": title, "content": content,
            "metadata": {"distilled_from": distilled_from} if distilled_from else {}}


class TestFindPriorDecision:
    """_find_prior_decision picks the top prior decision, skipping self + same-session siblings."""

    def test_skips_self_and_same_session_siblings(self):
        results = [
            _decision("new-1", "self", "self content"),                              # self -> skip
            _decision("sib-1", "sibling", "sib content", distilled_from="sessA"),     # same session -> skip
            _decision("prior-1", "prior", "prior content", distilled_from="sessB"),   # valid candidate
        ]
        with patch.object(ds, "hybrid_search", return_value=list(results)), \
             patch.object(ds, "rerank", side_effect=lambda r, q: r):
            cand = ds._find_prior_decision("new-1", "sessA", "query", [0.1])
        assert cand is not None and cand["id"] == "prior-1"

    def test_returns_none_when_only_self(self):
        with patch.object(ds, "hybrid_search", return_value=[_decision("new-1", "self", "c")]), \
             patch.object(ds, "rerank", side_effect=lambda r, q: r):
            assert ds._find_prior_decision("new-1", "sessA", "query", [0.1]) is None


class TestDetectAndLinkContradiction:
    """detect_and_link_contradiction links a `contradicts` edge only on a positive LLM judgment."""

    def test_positive_creates_contradicts_link(self):
        invoker = Mock()
        invoker.invoke.return_value = {"output": {"contradicts": True, "reason": "reverses earlier choice"}}
        prior = _decision("prior-1", "Prior", "we will use X", distilled_from="sessB")
        with patch.object(ds, "hybrid_search", return_value=[prior]), \
             patch.object(ds, "rerank", side_effect=lambda r, q: r), \
             patch.object(ds, "create_relationship") as crel:
            out = ds.detect_and_link_contradiction(
                invoker, "new-1", "New", "we will use Y instead", [0.1], "sessA")
        assert out == "prior-1"
        crel.assert_called_once()
        src, tgt, rel, note = crel.call_args[0]
        assert src == "new-1" and tgt == "prior-1" and rel == "contradicts"
        assert "reverses" in note

    def test_negative_creates_no_link(self):
        invoker = Mock()
        invoker.invoke.return_value = {"output": {"contradicts": False, "reason": "different topic"}}
        prior = _decision("prior-1", "Prior", "unrelated", distilled_from="sessB")
        with patch.object(ds, "hybrid_search", return_value=[prior]), \
             patch.object(ds, "rerank", side_effect=lambda r, q: r), \
             patch.object(ds, "create_relationship") as crel:
            out = ds.detect_and_link_contradiction(invoker, "new-1", "New", "content", [0.1], "sessA")
        assert out is None
        crel.assert_not_called()

    def test_no_candidate_skips_llm_and_link(self):
        invoker = Mock()
        with patch.object(ds, "hybrid_search", return_value=[]), \
             patch.object(ds, "rerank", side_effect=lambda r, q: r), \
             patch.object(ds, "create_relationship") as crel:
            out = ds.detect_and_link_contradiction(invoker, "new-1", "New", "content", [0.1], "sessA")
        assert out is None
        invoker.invoke.assert_not_called()
        crel.assert_not_called()

    def test_module_cannot_change_status(self):
        # Detect-and-link must NEVER flip status; the module doesn't even import a status mutator.
        assert not hasattr(ds, "update_memory")


class TestDistillRunsContradictionForDecisionsOnly:
    """distill() runs contradiction detection for decisions only, and honors the disable flag."""

    def test_detection_called_only_for_decisions(self):
        invoker = Mock()
        invoker.invoke.return_value = {"output": [
            {"type": "decision", "title": "D", "content": "decision body"},
            {"type": "insight", "title": "I", "content": "insight body"},
        ]}
        detector = Mock(return_value=None)
        with patch.object(ds, "KiroInvoker", return_value=invoker), \
             patch.object(ds, "get_processed_source_urls", return_value=set()), \
             patch.object(ds, "fetch_sessions", return_value=[("sessX", "kiro_cli_chat", "transcript")]), \
             patch.object(ds, "generate_embedding", return_value=[0.1]), \
             patch.object(ds, "create_memory", side_effect=lambda **k: "mem-" + k["type"]), \
             patch.object(ds, "detect_and_link_contradiction", detector):
            stats = ds.distill(detect_contradictions=True)
        assert stats["memories"] == 2
        assert detector.call_count == 1                      # decision only, not the insight
        assert detector.call_args[0][1] == "mem-decision"    # (invoker, new_id, ...)

    def test_detection_skipped_when_disabled(self):
        invoker = Mock()
        invoker.invoke.return_value = {"output": [{"type": "decision", "title": "D", "content": "body"}]}
        detector = Mock(return_value=None)
        with patch.object(ds, "KiroInvoker", return_value=invoker), \
             patch.object(ds, "get_processed_source_urls", return_value=set()), \
             patch.object(ds, "fetch_sessions", return_value=[("sessX", "kiro_cli_chat", "t")]), \
             patch.object(ds, "generate_embedding", return_value=[0.1]), \
             patch.object(ds, "create_memory", side_effect=lambda **k: "mem-x"), \
             patch.object(ds, "detect_and_link_contradiction", detector):
            ds.distill(detect_contradictions=False)
        detector.assert_not_called()


class TestDistillInvokerSettings:
    """The nightly job died on 300s Opus timeouts; pin the settings that fixed it."""

    def test_invokes_extraction_model_with_bounded_timeout(self):
        invoker = Mock()
        invoker.invoke.return_value = {"output": []}
        with patch.object(ds, "KiroInvoker", return_value=invoker) as ctor, \
             patch.object(ds, "get_processed_source_urls", return_value=set()), \
             patch.object(ds, "fetch_sessions", return_value=[("sessX", "kiro_cli_chat", "transcript")]):
            ds.distill()
        ctor.assert_called_once_with(model=ds.DISTILL_MODEL)
        kwargs = invoker.invoke.call_args.kwargs
        assert kwargs["timeout"] == ds.DISTILL_TIMEOUT >= 600
        assert kwargs["effort"] == ds.DISTILL_EFFORT
        assert ds.DISTILL_MODEL != "claude-opus-4.8"          # extraction does not need Opus

    def test_nightly_batch_is_bounded_newest_first(self):
        assert 0 < ds.DEFAULT_LIMIT <= 100
        sessions = [(f"s{i}", "kiro_cli_chat", "c") for i in range(5)]
        with patch.object(ds, "get_connection") as gc:
            cur = gc.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            cur.fetchall.return_value = [(u, st, c) for u, st, c in sessions]
            out = ds.fetch_sessions(("kiro_cli_chat",), 2, set())
        assert [u for u, _, _ in out] == ["s0", "s1"]
        assert "ORDER BY created_at DESC" in cur.execute.call_args[0][0]
