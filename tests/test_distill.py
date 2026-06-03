"""Tests for the chat-session distiller (Refactor P1)."""

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
