"""Tests for the Kiro IDE chat parser (src/parsers/ide_chat.py)."""

import json
import os
import tempfile

from src.parsers.ide_chat import (
    strip_messages,
    passes_size_filter,
    passes_content_filter,
    format_as_markdown,
    extract_project_context,
    parse_chat_file,
    MIN_USER_MESSAGES,
    MIN_CONTENT_CHARS,
    MIN_PARAGRAPH_WORDS,
)


class TestStripMessages:
    def test_drops_tool_messages(self):
        msgs = [{"role": "tool", "content": "tool output"},
                {"role": "human", "content": "real question"}]
        out = strip_messages(msgs)
        assert all(r != "tool" for r, _ in out)
        assert ("human", "real question") in out

    def test_drops_leading_system_prompt(self):
        msgs = [{"role": "human", "content": "# System Prompt\nyou are an agent"},
                {"role": "human", "content": "actual user question here"}]
        out = strip_messages(msgs)
        assert len(out) == 1
        assert out[0][1] == "actual user question here"

    def test_drops_identity_system_prompt(self):
        msgs = [{"role": "human", "content": "# Identity\nyou are Kiro"},
                {"role": "bot", "content": "hello there friend"}]
        out = strip_messages(msgs)
        assert all(not c.startswith("# Identity") for _, c in out)

    def test_drops_empty_and_boilerplate_bot(self):
        msgs = [{"role": "human", "content": "hi there question"},
                {"role": "bot", "content": "   "},
                {"role": "bot", "content": "I will follow these instructions."}]
        out = strip_messages(msgs)
        assert all(r != "bot" for r, _ in out)

    def test_strips_embedded_context_blocks(self):
        content = ("Real question <EnvironmentContext>os=mac</EnvironmentContext> "
                   "<implicit-rules>secret</implicit-rules> "
                   "<implicitInstruction>hidden</implicitInstruction> end")
        msgs = [{"role": "human", "content": content}]
        out = strip_messages(msgs)
        assert len(out) == 1
        text = out[0][1]
        assert "EnvironmentContext" not in text and "secret" not in text and "hidden" not in text
        assert "Real question" in text and "end" in text

    def test_message_empty_after_stripping_is_dropped(self):
        msgs = [{"role": "human", "content": "<EnvironmentContext>only context</EnvironmentContext>"}]
        assert strip_messages(msgs) == []


class TestSizeFilter:
    def test_rejects_too_few_user_messages(self):
        msgs = [("human", "x" * 300)]  # only 1 user message
        assert passes_size_filter(msgs) is False

    def test_rejects_too_few_chars(self):
        msgs = [("human", "hi"), ("human", "yo")]  # 2 users but tiny
        assert passes_size_filter(msgs) is False

    def test_accepts_when_thresholds_met(self):
        msgs = [("human", "a" * 150), ("bot", "b" * 150), ("human", "c" * 150)]
        assert len(msgs) >= MIN_USER_MESSAGES
        assert sum(len(c) for _, c in msgs) >= MIN_CONTENT_CHARS
        assert passes_size_filter(msgs) is True


class TestContentFilter:
    def test_accepts_substantive_bot_paragraph(self):
        para = " ".join(["word"] * MIN_PARAGRAPH_WORDS)
        msgs = [("human", "q"), ("bot", para)]
        assert passes_content_filter(msgs) is True

    def test_rejects_only_short_bot_messages(self):
        msgs = [("human", "q"), ("bot", "ok"), ("bot", "done now")]
        assert passes_content_filter(msgs) is False


class TestFormatAsMarkdown:
    def test_header_fields_and_roles(self):
        msgs = [("human", "the question"), ("bot", "the answer")]
        meta = {"model": "claude", "workflow": "vibe", "project_hint": "proj-x"}
        md = format_as_markdown("sess1", msgs, meta, None)
        assert "# Chat: sess1" in md
        assert "Source-Type: kiro_ide_chat" in md
        assert "Model: claude" in md and "Workflow: vibe" in md
        assert "Project: proj-x" in md
        assert "**User:**" in md and "**Assistant:**" in md
        assert "the question" in md and "the answer" in md

    def test_epoch_millis_date_parsing(self):
        # 1704067200000 ms == 2024-01-01 00:00:00 UTC
        md = format_as_markdown("s", [("human", "q")], {}, 1704067200000)
        assert "Date: 2024-01-01" in md

    def test_epoch_seconds_date_parsing(self):
        md = format_as_markdown("s", [("human", "q")], {}, 1704067200)
        assert "Date: 2024-01-01" in md

    def test_missing_project_hint_omits_line(self):
        md = format_as_markdown("s", [("human", "q")], {"model": "m"}, None)
        assert "Project:" not in md


class TestExtractProjectContext:
    def test_extracts_metadata_and_project(self):
        data = {
            "metadata": {"modelId": "claude-x", "workflow": "spec",
                         "startTime": 123, "endTime": 456},
            "context": [{"type": "fileTree", "expandedPaths": ["my-project/src/app.py"]}],
        }
        out = extract_project_context(data)
        assert out["model"] == "claude-x" and out["workflow"] == "spec"
        assert out["start_time"] == 123 and out["end_time"] == 456
        assert out["project_hint"]  # normalized from "my-project"

    def test_handles_missing_context(self):
        out = extract_project_context({"metadata": {}})
        assert out["project_hint"] is None
        assert out["model"] == ""


class TestParseChatFile:
    def _write(self, payload):
        fd, path = tempfile.mkstemp(suffix=".chat")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        return path

    def test_valid_chat_parses(self):
        para = " ".join(["reasoning"] * (MIN_PARAGRAPH_WORDS + 5))
        payload = {
            "metadata": {"modelId": "m", "workflow": "w", "startTime": 1704067200000},
            "context": [],
            "chat": [
                {"role": "human", "content": "first substantive question about the design"},
                {"role": "bot", "content": para},
                {"role": "human", "content": "second follow-up question with more detail here"},
            ],
        }
        path = self._write(payload)
        try:
            result = parse_chat_file(path)
            assert result is not None
            filename, markdown, meta = result
            assert "# Chat:" in markdown and meta["model"] == "m"
        finally:
            os.remove(path)

    def test_too_small_is_filtered(self):
        payload = {"metadata": {}, "context": [], "chat": [{"role": "human", "content": "hi"}]}
        path = self._write(payload)
        try:
            assert parse_chat_file(path) is None
        finally:
            os.remove(path)

    def test_malformed_json_returns_none(self):
        fd, path = tempfile.mkstemp(suffix=".chat")
        with os.fdopen(fd, "w") as f:
            f.write("{not valid json")
        try:
            assert parse_chat_file(path) is None
        finally:
            os.remove(path)

    def test_empty_chat_returns_none(self):
        path = self._write({"metadata": {}, "context": [], "chat": []})
        try:
            assert parse_chat_file(path) is None
        finally:
            os.remove(path)
