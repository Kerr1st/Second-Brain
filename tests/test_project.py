"""Unit tests for normalize_project_tag() — exact examples from the design document.

Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 8.6
"""

import pytest

from src.project import normalize_project_tag


class TestNormalizeProjectTagExactExamples:
    """Test every example from the Normalization Function Contract in design.md."""

    def test_none_returns_none(self):
        assert normalize_project_tag(None) is None

    def test_empty_string_returns_none(self):
        assert normalize_project_tag("") is None

    def test_whitespace_only_returns_none(self):
        assert normalize_project_tag("  ") is None

    def test_bare_name_lowercased(self):
        assert normalize_project_tag("RetailStore") == "retailstore"

    def test_bare_name_with_surrounding_whitespace(self):
        assert normalize_project_tag("  RetailStore  ") == "retailstore"

    def test_absolute_path_extracts_leaf(self):
        assert normalize_project_tag("/Users/example/Projects/RetailStore") == "retailstore"

    def test_absolute_short_path_home_dir_returns_none(self):
        # absolute, < 3 components
        assert normalize_project_tag("/Users/example") is None

    def test_absolute_root_returns_none(self):
        # absolute, < 3 components
        assert normalize_project_tag("/") is None

    def test_dot_prefixed_bare_kiro_returns_none(self):
        assert normalize_project_tag(".kiro") is None

    def test_dot_prefixed_bare_git_returns_none(self):
        assert normalize_project_tag(".git") is None

    def test_dot_prefixed_in_relative_path_returns_none(self):
        assert normalize_project_tag("path/to/.vscode") is None

    def test_relative_path_extracts_leaf(self):
        assert normalize_project_tag("Projects/RetailStore") == "retailstore"


class TestNormalizeProjectTagNonStringTypes:
    """Non-string types should all return None."""

    def test_int_returns_none(self):
        assert normalize_project_tag(42) is None

    def test_list_returns_none(self):
        assert normalize_project_tag(["a", "b"]) is None

    def test_dict_returns_none(self):
        assert normalize_project_tag({"key": "value"}) is None

    def test_bool_returns_none(self):
        assert normalize_project_tag(True) is None

    def test_float_returns_none(self):
        assert normalize_project_tag(3.14) is None


# ---------------------------------------------------------------------------
# Unit tests for IDE parser: extract_project_context()
# Validates: Requirements 1.1, 1.2, 1.3, 1.4
# ---------------------------------------------------------------------------

from src.parsers.ide_chat import extract_project_context


def _make_chat_data(context=None, metadata=None):
    """Build a minimal .chat JSON structure for testing."""
    return {
        "context": context or [],
        "metadata": metadata or {
            "modelId": "claude-sonnet",
            "workflow": "chat",
            "startTime": 1700000000000,
            "endTime": 1700001000000,
        },
        "chat": [],
    }


class TestExtractProjectContextUnit:
    """Unit tests for extract_project_context() with .chat JSON fixtures."""

    def test_filetree_with_expanded_paths_extracts_project(self):
        """fileTree with expandedPaths → project_hint is normalized top-level dir."""
        data = _make_chat_data(
            context=[{"type": "fileTree", "expandedPaths": ["RetailStore/src/main.py"]}],
        )
        meta = extract_project_context(data)
        assert meta["project_hint"] == "retailstore"

    def test_no_filetree_context_returns_none(self):
        """No fileTree context entry at all → project_hint is None."""
        data = _make_chat_data(context=[])
        meta = extract_project_context(data)
        assert meta["project_hint"] is None

    def test_filetree_empty_expanded_paths_returns_none(self):
        """fileTree present but expandedPaths is empty → project_hint is None."""
        data = _make_chat_data(
            context=[{"type": "fileTree", "expandedPaths": []}],
        )
        meta = extract_project_context(data)
        assert meta["project_hint"] is None

    def test_filetree_dot_prefixed_top_dir_returns_none(self):
        """First expanded path's top dir is dot-prefixed → project_hint is None."""
        data = _make_chat_data(
            context=[{"type": "fileTree", "expandedPaths": [".kiro/settings"]}],
        )
        meta = extract_project_context(data)
        assert meta["project_hint"] is None

    def test_filetree_bare_filename_returns_normalized(self):
        """Bare name like 'README.md' → split('/')[0] is 'README.md',
        normalize_project_tag returns 'readme.md' (not dot-prefixed, valid)."""
        data = _make_chat_data(
            context=[{"type": "fileTree", "expandedPaths": ["README.md"]}],
        )
        meta = extract_project_context(data)
        assert meta["project_hint"] == "readme.md"

    def test_metadata_fields_propagated(self):
        """Verify model, workflow, start_time, end_time are extracted."""
        data = _make_chat_data(
            metadata={
                "modelId": "claude-sonnet",
                "workflow": "chat",
                "startTime": 1700000000000,
                "endTime": 1700001000000,
            },
        )
        meta = extract_project_context(data)
        assert meta["model"] == "claude-sonnet"
        assert meta["workflow"] == "chat"
        assert meta["start_time"] == 1700000000000
        assert meta["end_time"] == 1700001000000

    def test_non_filetree_context_ignored(self):
        """Context entries that aren't fileTree are ignored."""
        data = _make_chat_data(
            context=[{"type": "selection", "content": "some code"}],
        )
        meta = extract_project_context(data)
        assert meta["project_hint"] is None

    def test_multiple_context_entries_uses_filetree(self):
        """When multiple context entries exist, fileTree is found and used."""
        data = _make_chat_data(
            context=[
                {"type": "selection", "content": "code"},
                {"type": "fileTree", "expandedPaths": ["MyProject/lib/utils.py"]},
            ],
        )
        meta = extract_project_context(data)
        assert meta["project_hint"] == "myproject"


# ---------------------------------------------------------------------------
# Unit tests for CLI parser: parse_conversation() and format_as_markdown()
# Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6
# ---------------------------------------------------------------------------

import json

from src.parsers.cli_chat import parse_conversation, format_as_markdown as cli_format_as_markdown


def _valid_messages():
    """A (role, text) message list that passes the cli_chat size/content filters:
    2 user messages, > MIN_CONTENT_CHARS total, one assistant paragraph >= 50 words."""
    return [
        ("human", "How do I set up authentication in my app?"),
        ("human", "Can you show me an example with JWT tokens?"),
        ("bot",
         "To set up authentication you need to configure your identity provider "
         "and integrate it with your application backend. First install the required "
         "dependencies using your package manager. Then create a middleware that "
         "validates incoming tokens on every request. You should also handle token "
         "refresh logic so users stay authenticated across sessions. Finally add "
         "proper error handling for expired or invalid tokens to return clear "
         "HTTP status codes to the client."),
    ]


class TestParseConversationProjectExtraction:
    """Test parse_conversation() extracts project from conversation_id paths."""

    def test_workspace_path_extracts_project(self):
        """Real workspace path → project is the lowercased leaf directory.
        Validates: Requirements 2.1, 2.2"""
        conv_id = "/Users/example/Projects/RetailStore"
        messages = _valid_messages()
        result = parse_conversation(conv_id, messages, 1700000000000)

        assert result is not None
        returned_id, markdown, project = result
        assert returned_id == conv_id
        assert project == "retailstore"

    def test_home_dir_returns_none_project(self):
        """Home directory (absolute, < 3 components) → project is None.
        Validates: Requirements 2.3, 2.4"""
        conv_id = "/Users/example"
        messages = _valid_messages()
        result = parse_conversation(conv_id, messages, 1700000000000)

        assert result is not None
        _, _, project = result
        assert project is None

    def test_empty_conversation_id_returns_none_project(self):
        """Empty string conversation_id → project is None.
        Validates: Requirement 2.3"""
        conv_id = ""
        messages = _valid_messages()
        result = parse_conversation(conv_id, messages, 1700000000000)

        assert result is not None
        _, _, project = result
        assert project is None


class TestCliFormatAsMarkdownProject:
    """Test format_as_markdown() Project header emission."""

    def test_with_project_includes_header(self):
        """When project is provided, output contains 'Project: <value>'.
        Validates: Requirement 2.5"""
        messages = [("human", "Hello"), ("bot", "Hi there")]
        md = cli_format_as_markdown("test-conv", messages, 1700000000000, project="retailstore")
        assert "Project: retailstore" in md

    def test_without_project_excludes_header(self):
        """When project is None, output does NOT contain 'Project:'.
        Validates: Requirement 2.5"""
        messages = [("human", "Hello"), ("bot", "Hi there")]
        md = cli_format_as_markdown("test-conv", messages, 1700000000000, project=None)
        assert "Project:" not in md


# ---------------------------------------------------------------------------
# Unit tests for parse_metadata_header() reading Project: header
# Validates: Requirements 4.1, 4.2
# ---------------------------------------------------------------------------

from src.ingest import parse_metadata_header


class TestParseMetadataHeaderProject:
    """Test that parse_metadata_header() correctly parses the Project: header."""

    def test_project_header_parsed(self):
        """Content with 'Project: retailstore' → meta["project"] == "retailstore"."""
        content = "# My Title\n\nSource: http://example.com\nProject: retailstore\n\n---\n\nBody text here."
        meta, body = parse_metadata_header(content)
        assert meta["project"] == "retailstore"

    def test_no_project_header(self):
        """Content without Project header → 'project' not in meta."""
        content = "# My Title\n\nSource: http://example.com\nType: article\n\n---\n\nBody text here."
        meta, body = parse_metadata_header(content)
        assert "project" not in meta

    def test_project_header_whitespace_stripped(self):
        """Content with 'Project:  RetailStore  ' → meta["project"] == "RetailStore".

        parse_metadata_header does value.strip(), so surrounding whitespace is removed,
        but casing is preserved (normalization happens later in ingest_content).
        """
        content = "# My Title\n\nProject:  RetailStore  \n\n---\n\nBody text here."
        meta, body = parse_metadata_header(content)
        assert meta["project"] == "RetailStore"


# ---------------------------------------------------------------------------
# Unit tests for MCP server project normalization
# Validates: Requirements 8.8, 8.9
# ---------------------------------------------------------------------------

from unittest.mock import patch, MagicMock
import asyncio


class TestMcpMemoryCreateNormalizesProject:
    """memory_create normalizes the project parameter before passing to create_memory.

    Validates: Requirement 8.9
    """

    @patch("src.mcp_server.create_memory", return_value="fake-id")
    @patch("src.mcp_server.compute_depth_score", return_value=0.5)
    @patch("src.mcp_server.classify_memory", return_value="source")
    @patch("src.mcp_server.generate_embedding", return_value=[0.0] * 1024)
    def test_memory_create_normalizes_project(
        self, mock_embed, mock_classify, mock_depth, mock_create
    ):
        from src.mcp_server import memory_create

        memory_create(
            type="idea",
            title="Test",
            content="Test content",
            project="  RetailStore  ",
        )

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs.kwargs.get("project") == "retailstore" or \
            (call_kwargs[1].get("project") == "retailstore")


class TestMcpMemorySearchNormalizesProject:
    """memory_search normalizes project before the retrieval boundary.

    Validates: Requirement 8.8
    """

    @patch("src.mcp_server.get_memory", return_value=None)
    @patch("src.mcp_server.increment_access_count")
    @patch("src.mcp_server.retrieve_memories", return_value=[])
    @patch("src.mcp_server.generate_embedding", return_value=[0.0] * 1024)
    def test_memory_search_normalizes_project(
        self, mock_embed, mock_search, mock_inc, mock_get
    ):
        from src.mcp_server import memory_search

        memory_search(query="test", project="  RetailStore  ")

        # Verify the deep retrieval interface received normalized project.
        mock_search.assert_called_once()
        search_kwargs = mock_search.call_args
        assert search_kwargs.kwargs.get("project") == "retailstore" or \
            (search_kwargs[1].get("project") == "retailstore")


# ---------------------------------------------------------------------------
# Backfill edge case unit tests (Task 8.6)
# ---------------------------------------------------------------------------

import json
import logging
from unittest.mock import patch

import src.db as db

_backfill_log = logging.getLogger("backfill_unit_test")
_backfill_log.addHandler(logging.NullHandler())


def _insert_raw(conn, source_type, source_url=None, content="Test",
                metadata=None, parent_id=None, project=None):
    """Insert a memory via SQL for backfill tests. Returns UUID string."""
    meta_json = json.dumps(metadata or {})
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO memories (type, title, content, source_url, source_type,
                                  metadata, parent_id, project)
            VALUES ('source', 'Test', %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (content, source_url, source_type, meta_json, parent_id, project))
        return str(cur.fetchone()[0])


def _read_project(conn, mem_id):
    with conn.cursor() as cur:
        cur.execute("SELECT project FROM memories WHERE id = %s", (mem_id,))
        row = cur.fetchone()
        return row[0] if row else None


class TestBackfillEdgeCases:
    """Unit tests for backfill edge cases.

    Validates: Requirements 6.4, 6.5, 6.10, 7.2
    """

    def test_ide_chat_file_missing_falls_back_to_content_header(self, test_db, clean_tables):
        """When .chat file is missing, backfill uses Project: header from content."""
        content = "# Chat: test\n\nProject: retailstore\n\n---\n\nSome content."
        with db.get_connection() as conn:
            parent_id = _insert_raw(
                conn, "kiro_ide_chat", source_url="ide_missing_chat.md", content=content,
            )
            conn.commit()

            with patch("scripts.backfill_projects._build_chat_file_index", return_value={}):
                from scripts.backfill_projects import backfill_ide_chats
                stats = backfill_ide_chats(conn, dry_run=False, log=_backfill_log)
                conn.commit()

            assert _read_project(conn, parent_id) == "retailstore"
            assert stats["updated"] == 1

    def test_ide_neither_chat_file_nor_header_stays_null(self, test_db, clean_tables):
        """When neither .chat file nor Project: header exists, project stays NULL."""
        content = "# Chat: test\n\n---\n\nContent without project header."
        with db.get_connection() as conn:
            parent_id = _insert_raw(
                conn, "kiro_ide_chat", source_url="ide_no_project.md", content=content,
            )
            conn.commit()

            with patch("scripts.backfill_projects._build_chat_file_index", return_value={}):
                from scripts.backfill_projects import backfill_ide_chats
                stats = backfill_ide_chats(conn, dry_run=False, log=_backfill_log)
                conn.commit()

            assert _read_project(conn, parent_id) is None
            assert stats["left_null"] == 1

    def test_parent_child_consistency_after_backfill(self, test_db, clean_tables):
        """After backfill, parent and children share the same project tag."""
        content = "# Chat: test\n\nProject: myapp\n\n---\n\nParent content."
        with db.get_connection() as conn:
            parent_id = _insert_raw(
                conn, "kiro_ide_chat", source_url="ide_parent_child.md", content=content,
            )
            child1_id = _insert_raw(
                conn, "kiro_ide_chat", content="chunk 1", parent_id=parent_id,
            )
            child2_id = _insert_raw(
                conn, "kiro_ide_chat", content="chunk 2", parent_id=parent_id,
            )
            conn.commit()

            with patch("scripts.backfill_projects._build_chat_file_index", return_value={}):
                from scripts.backfill_projects import backfill_ide_chats
                backfill_ide_chats(conn, dry_run=False, log=_backfill_log)
                conn.commit()

            parent_project = _read_project(conn, parent_id)
            child1_project = _read_project(conn, child1_id)
            child2_project = _read_project(conn, child2_id)

            assert parent_project == "myapp"
            assert child1_project == "myapp"
            assert child2_project == "myapp"

    def test_cli_backfill_uses_metadata_source_id(self, test_db, clean_tables):
        """CLI backfill reads workspace path from metadata source_id."""
        metadata = {"source_id": "/Users/dev/Projects/RetailStore"}
        with db.get_connection() as conn:
            parent_id = _insert_raw(
                conn, "kiro_cli_chat", metadata=metadata,
            )
            child_id = _insert_raw(
                conn, "kiro_cli_chat", parent_id=parent_id,
            )
            conn.commit()

            from scripts.backfill_projects import backfill_cli_chats
            backfill_cli_chats(conn, dry_run=False, log=_backfill_log)
            conn.commit()

            assert _read_project(conn, parent_id) == "retailstore"
            assert _read_project(conn, child_id) == "retailstore"

    def test_cli_backfill_missing_source_id_stays_null(self, test_db, clean_tables):
        """CLI memory without source_id in metadata stays NULL."""
        metadata = {"some_other_key": "value"}
        with db.get_connection() as conn:
            parent_id = _insert_raw(
                conn, "kiro_cli_chat", metadata=metadata,
            )
            conn.commit()

            from scripts.backfill_projects import backfill_cli_chats
            with patch("scripts.backfill_projects._get_cli_conversation_ids", return_value={}):
                stats = backfill_cli_chats(conn, dry_run=False, log=_backfill_log)
                conn.commit()

            assert _read_project(conn, parent_id) is None
            assert stats["left_null"] == 1

    def test_cli_backfill_home_dir_excluded(self, test_db, clean_tables):
        """CLI memory with home dir workspace path gets excluded (< 3 components)."""
        metadata = {"source_id": "/Users/dev"}
        with db.get_connection() as conn:
            parent_id = _insert_raw(
                conn, "kiro_cli_chat", metadata=metadata,
            )
            conn.commit()

            from scripts.backfill_projects import backfill_cli_chats
            stats = backfill_cli_chats(conn, dry_run=False, log=_backfill_log)
            conn.commit()

            assert _read_project(conn, parent_id) is None
            assert stats["excluded"] == 1
