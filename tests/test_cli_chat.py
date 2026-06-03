"""Tests for the Kiro CLI chat parser (reads ~/.kiro/sessions/cli/*.jsonl).

Guards the format contract so a future kiro-cli storage change is caught here
(plus the liveness monitor) rather than silently dropping capture.
"""

import json

from src.parsers import cli_chat


def _entries(turns):
    """Build JSONL entries from (role, text) turns."""
    out = []
    for i, (role, text) in enumerate(turns):
        if role == "human":
            out.append({"version": "v1", "kind": "Prompt",
                        "data": {"content": [{"kind": "text", "data": text}],
                                 "meta": {"timestamp": 1780000000 + i}}})
        else:
            out.append({"version": "v1", "kind": "AssistantMessage",
                        "data": {"content": [{"kind": "text", "data": text}]}})
    return out


def _write(tmp_path, turns, extra=None):
    p = tmp_path / "sess.jsonl"
    entries = _entries(turns) + (extra or [])
    p.write_text("\n".join(json.dumps(e) for e in entries))
    return str(p)


def test_extract_yields_messages_and_timestamp(tmp_path):
    path = _write(tmp_path, [("human", "hello there"), ("bot", "hi back")])
    msgs, ts = cli_chat.extract_session_messages(path)
    assert msgs == [("human", "hello there"), ("bot", "hi back")]
    assert ts == 1780000000 * 1000  # seconds -> ms


def test_tool_results_and_tooluse_skipped(tmp_path):
    extra = [{"version": "v1", "kind": "ToolResults",
              "data": {"content": [{"kind": "toolResult", "data": {"x": 1}}]}},
             {"version": "v1", "kind": "AssistantMessage",
              "data": {"content": [{"kind": "toolUse", "data": {"name": "glob"}}]}}]
    path = _write(tmp_path, [("human", "q"), ("bot", "a")], extra=extra)
    msgs, _ = cli_chat.extract_session_messages(path)
    assert msgs == [("human", "q"), ("bot", "a")]  # no tool noise


def test_real_conversation_not_flagged_automated():
    msgs = [("human", "let's refactor the parser"), ("bot", "sure, here is how")]
    assert cli_chat.is_automated_conversation(msgs) is False


def test_automated_conversations_flagged():
    assert cli_chat.is_automated_conversation([("human", "Run type: scheduled. Memory count: 5")]) is True
    assert cli_chat.is_automated_conversation([("human", '{"memory": "slice"}')]) is True
    assert cli_chat.is_automated_conversation([("bot", "no human turn")]) is True


def test_well_formed_session_parses(tmp_path):
    para = " ".join(["word"] * 60)  # clears MIN_PARAGRAPH_WORDS
    turns = [("human", "first question about the system design here"),
             ("bot", para),
             ("human", "second follow-up question with more detail"),
             ("bot", "ok")]
    path = _write(tmp_path, turns)
    msgs, ts = cli_chat.extract_session_messages(path)
    result = cli_chat.parse_conversation("test-session-id", msgs, ts)
    assert result is not None
    conv_id, markdown, _ = result
    assert conv_id == "test-session-id"
    assert "Source-Type: kiro_cli_chat" in markdown
