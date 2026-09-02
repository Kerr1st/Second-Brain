"""Tests for migration utilities and parser functions.

All tests use synthetic data — no database required.
"""

import csv
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.migrate.migration_utils import (
    MIN_CONTENT_CHARS,
    MIN_PARAGRAPH_WORDS,
    MIN_USER_MESSAGES,
    MigrationError,
    format_chat_as_markdown,
    format_markdown_header,
    passes_chat_filter,
    passes_page_filter,
)
from scripts.migrate.migrate_claude import parse_claude_export, _extract_text
from scripts.migrate.migrate_chatgpt import linearize_mapping, parse_chatgpt_export
from scripts.migrate.migrate_notion import (
    strip_notion_id,
    strip_notion_artifacts,
    find_title_column,
    parse_notion_export,
    _path_slug,
)
from src.ingest import parse_metadata_header


# ---------------------------------------------------------------------------
# format_markdown_header
# ---------------------------------------------------------------------------

class TestFormatMarkdownHeader:
    def test_contains_title_line(self):
        out = format_markdown_header("My Title", "claude_chat", "claude://abc", "2024-01-01")
        assert out.startswith("# My Title\n")

    def test_contains_source_type(self):
        out = format_markdown_header("T", "claude_chat", "claude://abc", "2024-01-01")
        assert "Source-Type: claude_chat" in out

    def test_contains_source_id(self):
        out = format_markdown_header("T", "claude_chat", "claude://abc", "2024-01-01")
        assert "Source-ID: claude://abc" in out

    def test_contains_date(self):
        out = format_markdown_header("T", "claude_chat", "claude://abc", "2024-01-01")
        assert "Date: 2024-01-01" in out

    def test_contains_separator(self):
        out = format_markdown_header("T", "claude_chat", "claude://abc", "2024-01-01")
        assert "\n---\n" in out

    def test_extra_fields_included(self):
        out = format_markdown_header("T", "claude_chat", "claude://abc", "2024-01-01",
                                     extra_fields={"Model": "claude-3"})
        assert "Model: claude-3" in out

    def test_no_extra_fields_no_crash(self):
        out = format_markdown_header("T", "claude_chat", "claude://abc", "2024-01-01",
                                     extra_fields=None)
        assert "---" in out


class TestFormatMarkdownHeaderProperty:
    """Property: output always contains the --- separator."""

    @pytest.mark.parametrize("title", ["", "A", "A very long title " * 20])
    @pytest.mark.parametrize("source_type", ["claude_chat", "chatgpt_chat", "notion_page"])
    def test_separator_always_present(self, title, source_type):
        out = format_markdown_header(title, source_type, "url://x", "2024-01-01")
        assert "\n---\n" in out


# ---------------------------------------------------------------------------
# format_chat_as_markdown — round-trip through parse_metadata_header
# ---------------------------------------------------------------------------

class TestFormatChatAsMarkdown:
    def test_round_trip_metadata_survives(self):
        md = format_chat_as_markdown(
            "Test Chat", [("human", "Hello"), ("bot", "Hi there")],
            "claude_chat", "claude://abc", "2024-06-15",
        )
        meta, body = parse_metadata_header(md)
        assert meta["title"] == "Test Chat"
        assert meta["source_type"] == "claude_chat"
        assert meta["source_id"] == "claude://abc"
        assert meta["date"] == "2024-06-15"
        assert "Hello" in body
        assert "Hi there" in body

    def test_extra_fields_round_trip(self):
        md = format_chat_as_markdown(
            "Chat", [("human", "Q")], "chatgpt_chat", "chatgpt://1", "2024-01-01",
            extra_fields={"Model": "gpt-4"},
        )
        meta, _ = parse_metadata_header(md)
        assert meta["model"] == "gpt-4"

    def test_user_role_variants(self):
        """Both 'human' and 'user' roles produce **User:** labels."""
        md_human = format_chat_as_markdown("C", [("human", "Q")], "t", "u", "d")
        md_user = format_chat_as_markdown("C", [("user", "Q")], "t", "u", "d")
        assert "**User:**" in md_human
        assert "**User:**" in md_user


# ---------------------------------------------------------------------------
# passes_chat_filter
# ---------------------------------------------------------------------------

class TestPassesChatFilter:
    def test_rejects_too_few_user_messages(self):
        messages = [("human", "short"), ("bot", "x " * 100)]
        assert not passes_chat_filter(messages)

    def test_rejects_too_few_total_chars(self):
        messages = [("human", "hi"), ("human", "yo"), ("bot", "ok")]
        assert not passes_chat_filter(messages)

    def test_rejects_no_substantive_paragraph(self):
        short_para = "word " * (MIN_PARAGRAPH_WORDS - 1)
        messages = [("human", "a" * 100), ("human", "b" * 100), ("bot", short_para)]
        assert not passes_chat_filter(messages)

    def test_accepts_above_all_thresholds(self):
        long_para = "word " * MIN_PARAGRAPH_WORDS
        messages = [("human", "a" * 100), ("human", "b" * 100), ("bot", long_para)]
        assert passes_chat_filter(messages)

    def test_accepts_user_role_variant(self):
        long_para = "word " * MIN_PARAGRAPH_WORDS
        messages = [("user", "a" * 100), ("user", "b" * 100), ("bot", long_para)]
        assert passes_chat_filter(messages)


class TestPassesChatFilterMonotonic:
    """Property: adding messages to a passing conversation never makes it fail."""

    def _make_passing(self):
        long_para = "word " * MIN_PARAGRAPH_WORDS
        return [("human", "a" * 100), ("human", "b" * 100), ("bot", long_para)]

    @pytest.mark.parametrize("extra", [
        ("human", "another question"),
        ("bot", "another answer with enough words " * 10),
        ("human", "x"),
    ])
    def test_adding_message_preserves_pass(self, extra):
        base = self._make_passing()
        assert passes_chat_filter(base)
        assert passes_chat_filter(base + [extra])


# ---------------------------------------------------------------------------
# passes_page_filter
# ---------------------------------------------------------------------------

class TestPassesPageFilter:
    def test_rejects_short_content(self):
        assert not passes_page_filter("short")

    def test_rejects_whitespace_only(self):
        assert not passes_page_filter("   \n\n  ")

    def test_accepts_long_content(self):
        assert passes_page_filter("x" * MIN_CONTENT_CHARS)

    def test_boundary_exact_threshold(self):
        assert passes_page_filter("x" * MIN_CONTENT_CHARS)
        assert not passes_page_filter("x" * (MIN_CONTENT_CHARS - 1))


# ---------------------------------------------------------------------------
# run_migration — tested via integration with mock parse_fn
# ---------------------------------------------------------------------------

class TestRunMigrationValidation:
    """Tests for run_migration path validation (no DB needed)."""

    def test_raises_on_missing_directory(self, tmp_path):
        fake_path = str(tmp_path / "nonexistent")
        with pytest.raises(MigrationError, match="does not exist"):
            from scripts.migrate.migration_utils import run_migration
            run_migration("test", fake_path, lambda p: iter([]), "test_type")

    def test_raises_on_missing_expected_file(self, tmp_path):
        with pytest.raises(MigrationError, match="not found"):
            from scripts.migrate.migration_utils import run_migration
            run_migration("test", str(tmp_path), lambda p: iter([]), "test_type",
                          expected_file="conversations.json")

    def test_accepts_valid_directory(self, tmp_path, monkeypatch):
        """Dry-run with empty parse_fn should succeed without DB."""
        from scripts.migrate.migration_utils import run_migration
        run_migration("test", str(tmp_path), lambda p: iter([]), "test_type",
                      dry_run=True)

    def test_accepts_valid_expected_file(self, tmp_path, monkeypatch):
        (tmp_path / "conversations.json").write_text("[]")
        from scripts.migrate.migration_utils import run_migration
        run_migration("test", str(tmp_path), lambda p: iter([]), "test_type",
                      expected_file="conversations.json", dry_run=True)


class TestRunMigrationDryRun:
    """Tests for run_migration dry-run mode."""

    def test_dry_run_counts_items(self, tmp_path, capsys):
        def fake_parse(path):
            yield ("url://1", "# T\n\n---\n\nbody")
            yield ("url://2", "# T\n\n---\n\nbody")

        from scripts.migrate.migration_utils import run_migration
        run_migration("test", str(tmp_path), fake_parse, "test_type", dry_run=True)
        captured = capsys.readouterr()
        assert "Processed:        2" in captured.out

    def test_dry_run_respects_limit(self, tmp_path, capsys):
        def fake_parse(path):
            for i in range(10):
                yield (f"url://{i}", "# T\n\n---\n\nbody")

        from scripts.migrate.migration_utils import run_migration
        run_migration("test", str(tmp_path), fake_parse, "test_type",
                      dry_run=True, limit=3)
        captured = capsys.readouterr()
        # limit caps total scanned items
        assert "Total scanned:    3" in captured.out


# ---------------------------------------------------------------------------
# Constants consistency
# ---------------------------------------------------------------------------

class TestConstantsMatchExistingParsers:
    def test_min_content_chars(self):
        from src.parsers.cli_chat import MIN_CONTENT_CHARS as CLI_MIN
        assert MIN_CONTENT_CHARS == CLI_MIN

    def test_min_user_messages(self):
        from src.parsers.cli_chat import MIN_USER_MESSAGES as CLI_MIN
        assert MIN_USER_MESSAGES == CLI_MIN

    def test_min_paragraph_words(self):
        from src.parsers.cli_chat import MIN_PARAGRAPH_WORDS as CLI_MIN
        assert MIN_PARAGRAPH_WORDS == CLI_MIN


# ===========================================================================
# Claude parser tests (Task 2)
# ===========================================================================

def _make_claude_conversation(uuid="abc-123", name="Test Conv", messages=None, model=None):
    """Build a synthetic Claude conversation dict."""
    if messages is None:
        long_para = "word " * MIN_PARAGRAPH_WORDS
        messages = [
            {"sender": "human", "text": "a" * 100},
            {"sender": "human", "text": "b" * 100},
            {"sender": "assistant", "text": long_para},
        ]
    conv = {
        "uuid": uuid,
        "name": name,
        "created_at": "2024-06-15T10:00:00Z",
        "updated_at": "2024-06-15T11:00:00Z",
        "chat_messages": messages,
    }
    if model:
        conv["model"] = model
    return conv


def _write_claude_export(tmp_path, conversations):
    with open(tmp_path / "conversations.json", "w") as f:
        json.dump(conversations, f)


class TestExtractText:
    def test_plain_string(self):
        assert _extract_text("hello") == "hello"

    def test_text_blocks(self):
        blocks = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
        assert "hello" in _extract_text(blocks)
        assert "world" in _extract_text(blocks)

    def test_strips_tool_use_blocks(self):
        blocks = [
            {"type": "text", "text": "visible"},
            {"type": "tool_use", "name": "search", "input": {}},
            {"type": "tool_result", "content": "result"},
        ]
        result = _extract_text(blocks)
        assert "visible" in result
        assert "search" not in result
        assert "result" not in result

    def test_handles_none(self):
        assert _extract_text(None) == ""

    def test_handles_mixed_string_and_blocks(self):
        blocks = ["plain text", {"type": "text", "text": "block text"}]
        result = _extract_text(blocks)
        assert "plain text" in result
        assert "block text" in result


class TestParseClaudeExport:
    def test_yields_passing_conversations(self, tmp_path):
        _write_claude_export(tmp_path, [_make_claude_conversation()])
        results = list(parse_claude_export(str(tmp_path)))
        assert len(results) == 1
        source_url, markdown = results[0]
        assert source_url == "claude://abc-123"
        assert "claude_chat" in markdown

    def test_skips_filtered_conversations(self, tmp_path):
        short_conv = _make_claude_conversation(messages=[
            {"sender": "human", "text": "hi"},
        ])
        _write_claude_export(tmp_path, [short_conv])
        results = list(parse_claude_export(str(tmp_path)))
        assert len(results) == 0

    def test_strips_tool_messages(self, tmp_path):
        long_para = "word " * MIN_PARAGRAPH_WORDS
        conv = _make_claude_conversation(messages=[
            {"sender": "human", "text": "a" * 100},
            {"sender": "human", "text": "b" * 100},
            {"sender": "assistant", "content": [
                {"type": "text", "text": long_para},
                {"type": "tool_use", "name": "search", "input": {}},
            ]},
            {"sender": "tool", "text": "tool output"},
        ])
        _write_claude_export(tmp_path, [conv])
        results = list(parse_claude_export(str(tmp_path)))
        assert len(results) == 1
        _, markdown = results[0]
        assert "tool output" not in markdown
        assert "search" not in markdown

    def test_extracts_model_metadata(self, tmp_path):
        conv = _make_claude_conversation(model="claude-3-opus-20240229")
        _write_claude_export(tmp_path, [conv])
        results = list(parse_claude_export(str(tmp_path)))
        meta, _ = parse_metadata_header(results[0][1])
        assert meta.get("model") == "claude-3-opus-20240229"

    def test_source_url_format(self, tmp_path):
        conv = _make_claude_conversation(uuid="550e8400-e29b-41d4-a716-446655440000")
        _write_claude_export(tmp_path, [conv])
        results = list(parse_claude_export(str(tmp_path)))
        assert results[0][0] == "claude://550e8400-e29b-41d4-a716-446655440000"

    def test_markdown_round_trips_through_parser(self, tmp_path):
        _write_claude_export(tmp_path, [_make_claude_conversation(name="Round Trip")])
        results = list(parse_claude_export(str(tmp_path)))
        meta, body = parse_metadata_header(results[0][1])
        assert meta["source_type"] == "claude_chat"
        assert "Round Trip" in meta["title"]

    def test_metadata_fields_in_markdown_header(self, tmp_path):
        """Metadata must survive into the markdown header for ingest_content()."""
        conv = _make_claude_conversation(model="claude-3-opus-20240229")
        _write_claude_export(tmp_path, [conv])
        results = list(parse_claude_export(str(tmp_path)))
        meta, _ = parse_metadata_header(results[0][1])
        assert "message_count" in meta or "message-count" in meta


# ===========================================================================
# ChatGPT parser tests (Task 3)
# ===========================================================================

def _make_chatgpt_mapping(messages):
    """Build a linear ChatGPT mapping tree from a list of (role, text) tuples."""
    mapping = {}
    prev_id = None
    for i, (role, text) in enumerate(messages):
        node_id = f"node-{i}"
        mapping[node_id] = {
            "id": node_id,
            "parent": prev_id,
            "children": [],
            "message": {
                "author": {"role": role},
                "content": {"parts": [text]},
            },
        }
        if prev_id and prev_id in mapping:
            mapping[prev_id]["children"].append(node_id)
        prev_id = node_id
    return mapping


def _make_chatgpt_conversation(conv_id="conv-1", title="Test Chat", messages=None, model_slug=None):
    """Build a synthetic ChatGPT conversation dict."""
    if messages is None:
        long_para = "word " * MIN_PARAGRAPH_WORDS
        messages = [
            ("system", "You are a helpful assistant."),
            ("user", "a" * 100),
            ("user", "b" * 100),
            ("assistant", long_para),
        ]
    conv = {
        "id": conv_id,
        "title": title,
        "create_time": 1718445600.0,  # 2024-06-15
        "update_time": 1718449200.0,
        "mapping": _make_chatgpt_mapping(messages),
    }
    if model_slug:
        # Inject model_slug into the first assistant message's metadata
        for node in conv["mapping"].values():
            msg = node.get("message", {})
            if msg.get("author", {}).get("role") == "assistant":
                msg["metadata"] = {"model_slug": model_slug}
                break
    return conv


def _write_chatgpt_export(tmp_path, conversations):
    with open(tmp_path / "conversations.json", "w") as f:
        json.dump(conversations, f)


class TestLinearizeMapping:
    def test_linear_conversation(self):
        mapping = _make_chatgpt_mapping([
            ("user", "hello"),
            ("assistant", "hi"),
        ])
        result = linearize_mapping(mapping)
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"

    def test_empty_mapping(self):
        assert linearize_mapping({}) == []

    def test_none_mapping(self):
        assert linearize_mapping(None) == []

    def test_branching_takes_first_child(self):
        mapping = {
            "root": {
                "id": "root", "parent": None, "children": ["a", "b"],
                "message": {"author": {"role": "user"}, "content": {"parts": ["question"]}},
            },
            "a": {
                "id": "a", "parent": "root", "children": [],
                "message": {"author": {"role": "assistant"}, "content": {"parts": ["answer A"]}},
            },
            "b": {
                "id": "b", "parent": "root", "children": [],
                "message": {"author": {"role": "assistant"}, "content": {"parts": ["answer B"]}},
            },
        }
        result = linearize_mapping(mapping)
        assert len(result) == 2
        assert result[1]["text"] == "answer A"  # first child

    def test_skips_non_string_parts(self):
        mapping = _make_chatgpt_mapping([("user", "hello")])
        # Inject a non-string part
        node = list(mapping.values())[0]
        node["message"]["content"]["parts"] = ["text part", {"type": "image"}, 42]
        result = linearize_mapping(mapping)
        assert result[0]["text"] == "text part"


class TestParseChatgptExport:
    def test_yields_passing_conversations(self, tmp_path):
        _write_chatgpt_export(tmp_path, [_make_chatgpt_conversation()])
        results = list(parse_chatgpt_export(str(tmp_path)))
        assert len(results) == 1
        source_url, markdown = results[0]
        assert source_url == "chatgpt://conv-1"
        assert "chatgpt_chat" in markdown

    def test_skips_system_and_tool_messages(self, tmp_path):
        long_para = "word " * MIN_PARAGRAPH_WORDS
        conv = _make_chatgpt_conversation(messages=[
            ("system", "You are helpful"),
            ("user", "a" * 100),
            ("user", "b" * 100),
            ("tool", "tool output"),
            ("assistant", long_para),
        ])
        _write_chatgpt_export(tmp_path, [conv])
        results = list(parse_chatgpt_export(str(tmp_path)))
        assert len(results) == 1
        _, markdown = results[0]
        assert "You are helpful" not in markdown
        assert "tool output" not in markdown

    def test_skips_filtered_conversations(self, tmp_path):
        conv = _make_chatgpt_conversation(messages=[("user", "hi")])
        _write_chatgpt_export(tmp_path, [conv])
        assert list(parse_chatgpt_export(str(tmp_path))) == []

    def test_extracts_model_slug(self, tmp_path):
        conv = _make_chatgpt_conversation(model_slug="gpt-4")
        _write_chatgpt_export(tmp_path, [conv])
        results = list(parse_chatgpt_export(str(tmp_path)))
        meta, _ = parse_metadata_header(results[0][1])
        assert meta.get("model") == "gpt-4"

    def test_epoch_to_iso_date(self, tmp_path):
        conv = _make_chatgpt_conversation()
        _write_chatgpt_export(tmp_path, [conv])
        results = list(parse_chatgpt_export(str(tmp_path)))
        meta, _ = parse_metadata_header(results[0][1])
        assert meta.get("original_created_at", "").startswith("2024-06-15")

    def test_markdown_round_trips_through_parser(self, tmp_path):
        _write_chatgpt_export(tmp_path, [_make_chatgpt_conversation(title="RT Test")])
        results = list(parse_chatgpt_export(str(tmp_path)))
        meta, body = parse_metadata_header(results[0][1])
        assert meta["source_type"] == "chatgpt_chat"
        assert "RT Test" in meta["title"]

    def test_metadata_fields_in_markdown_header(self, tmp_path):
        """Metadata must survive into the markdown header for ingest_content()."""
        conv = _make_chatgpt_conversation(model_slug="gpt-4")
        _write_chatgpt_export(tmp_path, [conv])
        results = list(parse_chatgpt_export(str(tmp_path)))
        meta, _ = parse_metadata_header(results[0][1])
        assert "message_count" in meta or "message-count" in meta


# ===========================================================================
# Notion parser tests (Task 4)
# ===========================================================================

class TestStripNotionId:
    def test_removes_32_char_hex(self):
        assert strip_notion_id("My Page abcdef01234567890abcdef012345678.md") == "My Page"

    def test_no_hash_unchanged(self):
        assert strip_notion_id("Simple Page.md") == "Simple Page"

    def test_handles_no_extension(self):
        assert strip_notion_id("No Extension abcdef01234567890abcdef012345678") == "No Extension"

    def test_handles_multiple_spaces(self):
        assert strip_notion_id("A  B abcdef01234567890abcdef012345678.md") == "A  B"

    def test_empty_filename(self):
        assert strip_notion_id(".md") == ""


class TestStripNotionIdIdempotent:
    """Property: stripping twice equals stripping once."""

    @pytest.mark.parametrize("filename", [
        "My Page abcdef01234567890abcdef012345678.md",
        "Simple.md",
        "No Hash.md",
        "Already Clean.md",
    ])
    def test_idempotent(self, filename):
        once = strip_notion_id(filename)
        twice = strip_notion_id(once + ".md")
        assert once == twice


class TestPathSlug:
    """Tests for _path_slug which uses full relative path to avoid collisions."""

    def test_simple_file(self):
        slug = _path_slug("Notes.md")
        assert slug == "notes"

    def test_nested_path(self):
        slug = _path_slug("Projects/Work/Notes.md")
        assert "projects" in slug
        assert "work" in slug
        assert "notes" in slug

    def test_strips_notion_hash_from_filename(self):
        slug = _path_slug("My Page abcdef01234567890abcdef012345678.md")
        assert "abcdef" not in slug

    def test_different_dirs_same_name_produce_different_slugs(self):
        slug_a = _path_slug("DirA/Notes.md")
        slug_b = _path_slug("DirB/Notes.md")
        assert slug_a != slug_b

    def test_case_insensitive(self):
        slug_a = _path_slug("My Page.md")
        slug_b = _path_slug("my page.md")
        assert slug_a == slug_b


class TestStripNotionArtifacts:
    def test_removes_notion_db_references_with_block_id(self):
        content = "See [My DB](https://www.notion.so/abc123def456789012345678901234ab) for details"
        result = strip_notion_artifacts(content)
        assert "notion.so" not in result

    def test_removes_notion_urls_with_block_id(self):
        content = "Link: https://www.notion.so/workspace/page-abc123def456789012345678901234ab"
        result = strip_notion_artifacts(content)
        assert "notion.so" not in result

    def test_preserves_normal_content(self):
        content = "This is normal markdown content with **bold** and *italic*."
        assert strip_notion_artifacts(content) == content

    def test_preserves_notion_urls_without_block_id(self):
        """Issue #8: don't strip legitimate Notion URLs that lack block IDs."""
        content = "See https://www.notion.so/my-public-page for details"
        assert "notion.so" in strip_notion_artifacts(content)


class TestFindTitleColumn:
    def test_finds_name_column(self):
        assert find_title_column(["Name", "Status", "Date"]) == "Name"

    def test_finds_title_column(self):
        assert find_title_column(["ID", "Title", "Body"]) == "Title"

    def test_case_insensitive(self):
        assert find_title_column(["id", "name", "value"]) == "name"

    def test_falls_back_to_first(self):
        assert find_title_column(["Foo", "Bar", "Baz"]) == "Foo"

    def test_empty_headers(self):
        assert find_title_column([]) is None


class TestParseNotionExportMarkdown:
    def test_yields_md_files(self, tmp_path):
        (tmp_path / "My Page abcdef01234567890abcdef012345678.md").write_text("x" * 300)
        results = list(parse_notion_export(str(tmp_path)))
        assert len(results) == 1
        source_url, markdown = results[0]
        assert source_url.startswith("notion://")
        meta, _ = parse_metadata_header(markdown)
        assert meta["title"] == "My Page"
        assert meta["source_type"] == "notion_page"

    def test_skips_short_md_files(self, tmp_path):
        (tmp_path / "Short.md").write_text("tiny")
        assert list(parse_notion_export(str(tmp_path))) == []

    def test_strips_notion_artifacts_from_content(self, tmp_path):
        content = "x" * 200 + "\nSee [DB](https://www.notion.so/abc123def456789012345678901234ab) for more"
        (tmp_path / "Page.md").write_text(content)
        results = list(parse_notion_export(str(tmp_path)))
        assert len(results) == 1
        assert "notion.so" not in results[0][1]

    def test_preserves_parent_page_path(self, tmp_path):
        subdir = tmp_path / "Projects" / "Work"
        subdir.mkdir(parents=True)
        (subdir / "Note.md").write_text("x" * 300)
        results = list(parse_notion_export(str(tmp_path)))
        meta, _ = parse_metadata_header(results[0][1])
        assert meta.get("parent_page_path") == os.path.join("Projects", "Work") or \
               meta.get("parent-page-path") == os.path.join("Projects", "Work")

    def test_parent_page_path_in_markdown_header(self, tmp_path):
        """Parent page path must be in the markdown header for ingest_content()."""
        subdir = tmp_path / "Projects"
        subdir.mkdir()
        (subdir / "Note.md").write_text("x" * 300)
        results = list(parse_notion_export(str(tmp_path)))
        meta, _ = parse_metadata_header(results[0][1])
        assert meta.get("parent_page_path") == "Projects" or meta.get("parent-page-path") == "Projects"

    def test_recursive_discovery(self, tmp_path):
        (tmp_path / "top.md").write_text("x" * 300)
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "nested.md").write_text("x" * 300)
        results = list(parse_notion_export(str(tmp_path)))
        assert len(results) == 2

    def test_case_variant_pages_get_distinct_urls(self, tmp_path):
        """Issue #4: pages with same name in different dirs must not collide."""
        dir_a = tmp_path / "ProjectA"
        dir_b = tmp_path / "ProjectB"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "Notes.md").write_text("x" * 300)
        (dir_b / "Notes.md").write_text("y" * 300)
        results = list(parse_notion_export(str(tmp_path)))
        urls = [r[0] for r in results]
        assert len(urls) == 2
        assert urls[0] != urls[1]  # distinct source_urls


class TestParseNotionExportCsv:
    def _write_csv(self, path, headers, rows):
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in rows:
                writer.writerow(row)

    def test_yields_csv_rows(self, tmp_path):
        self._write_csv(
            tmp_path / "Tasks.csv",
            ["Name", "Description"],
            [["Task 1", "x" * 300]],
        )
        results = list(parse_notion_export(str(tmp_path)))
        assert len(results) == 1
        meta, _ = parse_metadata_header(results[0][1])
        assert meta["title"] == "Task 1"

    def test_skips_short_csv_rows(self, tmp_path):
        self._write_csv(
            tmp_path / "Tasks.csv",
            ["Name", "Description"],
            [["Task 1", "tiny"]],
        )
        assert list(parse_notion_export(str(tmp_path))) == []

    def test_csv_source_url_includes_row_index(self, tmp_path):
        self._write_csv(
            tmp_path / "DB.csv",
            ["Name", "Notes"],
            [["Row 0", "x" * 300], ["Row 1", "y" * 300]],
        )
        results = list(parse_notion_export(str(tmp_path)))
        urls = [r[0] for r in results]
        assert any("#0" in u for u in urls)
        assert any("#1" in u for u in urls)

    def test_csv_uses_title_column_heuristic(self, tmp_path):
        self._write_csv(
            tmp_path / "DB.csv",
            ["ID", "Title", "Body"],
            [["1", "My Title", "x" * 300]],
        )
        results = list(parse_notion_export(str(tmp_path)))
        meta, _ = parse_metadata_header(results[0][1])
        assert meta["title"] == "My Title"

    def test_csv_falls_back_to_first_column(self, tmp_path):
        self._write_csv(
            tmp_path / "DB.csv",
            ["Foo", "Bar"],
            [["First Col", "x" * 300]],
        )
        results = list(parse_notion_export(str(tmp_path)))
        meta, _ = parse_metadata_header(results[0][1])
        assert meta["title"] == "First Col"


# ===========================================================================
# Integration tests — require PostgreSQL (test_db fixture)
# ===========================================================================

class TestClaudeIntegrationRoundTrip:
    """Parse → ingest → verify records → re-run → verify no duplicates."""

    def test_ingest_and_dedup(self, test_db, clean_tables, tmp_path, monkeypatch):
        monkeypatch.setattr("src.ingest.generate_embedding", lambda text: [0.1] * 1024)

        _write_claude_export(tmp_path, [_make_claude_conversation(uuid="int-test-1")])

        from scripts.migrate.migration_utils import run_migration
        from src.db import get_processed_source_urls, list_memories

        # First run — should ingest
        run_migration("Claude", str(tmp_path), parse_claude_export, "claude_chat",
                      expected_file="conversations.json")

        urls = get_processed_source_urls("claude_chat")
        assert "claude://int-test-1" in urls

        parents = [m for m in list_memories(source_type="claude_chat") if m["parent_id"] is None]
        assert len(parents) == 1

        chunks = [m for m in list_memories(source_type="claude_chat") if m["parent_id"] is not None]
        assert len(chunks) >= 1

        # Second run — should produce zero new records
        count_before = len(list_memories(source_type="claude_chat"))
        run_migration("Claude", str(tmp_path), parse_claude_export, "claude_chat",
                      expected_file="conversations.json")
        count_after = len(list_memories(source_type="claude_chat"))
        assert count_after == count_before


class TestChatgptIntegrationRoundTrip:
    """Parse → ingest → verify records → re-run → verify no duplicates."""

    def test_ingest_and_dedup(self, test_db, clean_tables, tmp_path, monkeypatch):
        monkeypatch.setattr("src.ingest.generate_embedding", lambda text: [0.1] * 1024)

        _write_chatgpt_export(tmp_path, [_make_chatgpt_conversation(conv_id="int-test-2")])

        from scripts.migrate.migration_utils import run_migration
        from src.db import get_processed_source_urls, list_memories

        run_migration("ChatGPT", str(tmp_path), parse_chatgpt_export, "chatgpt_chat",
                      expected_file="conversations.json")

        urls = get_processed_source_urls("chatgpt_chat")
        assert "chatgpt://int-test-2" in urls

        parents = [m for m in list_memories(source_type="chatgpt_chat") if m["parent_id"] is None]
        assert len(parents) == 1

        # Second run — zero new records
        count_before = len(list_memories(source_type="chatgpt_chat"))
        run_migration("ChatGPT", str(tmp_path), parse_chatgpt_export, "chatgpt_chat",
                      expected_file="conversations.json")
        count_after = len(list_memories(source_type="chatgpt_chat"))
        assert count_after == count_before


class TestNotionIntegrationRoundTrip:
    """Parse → ingest → verify records → re-run → verify no duplicates."""

    def test_ingest_and_dedup(self, test_db, clean_tables, tmp_path, monkeypatch):
        monkeypatch.setattr("src.ingest.generate_embedding", lambda text: [0.1] * 1024)

        (tmp_path / "Test Page.md").write_text("x" * 300)

        from scripts.migrate.migration_utils import run_migration
        from src.db import get_processed_source_urls, list_memories

        run_migration("Notion", str(tmp_path), parse_notion_export, "notion_page")

        urls = get_processed_source_urls("notion_page")
        assert any("notion://" in u for u in urls)

        parents = [m for m in list_memories(source_type="notion_page") if m["parent_id"] is None]
        assert len(parents) == 1

        # Second run — zero new records
        count_before = len(list_memories(source_type="notion_page"))
        run_migration("Notion", str(tmp_path), parse_notion_export, "notion_page")
        count_after = len(list_memories(source_type="notion_page"))
        assert count_after == count_before
