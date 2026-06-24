"""Property tests for project-auto-tagging correctness properties.

Validates normalization, parser extraction, ingestion, and backfill properties
for the project auto-tagging feature.
"""

import string

from hypothesis import given, settings, strategies as st

from src.project import normalize_project_tag


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Broad string strategy covering paths, dot-prefixed, whitespace, empty, Unicode
_project_strings = st.one_of(
    st.text(min_size=0, max_size=200),                          # arbitrary Unicode
    st.from_regex(r"[a-zA-Z0-9_\-\./ \\]{0,80}", fullmatch=True),  # path-like
    st.sampled_from([
        "", " ", "  \t\n", ".", ".kiro", ".git", ".vscode",
        "/", "/Users", "/Users/dev",
        "/Users/dev/Projects/MyApp",
        "RetailStore", "  RetailStore  ",
        "path/to/project", "path\\to\\project",
        "Projects/RetailStore",
        "/home/user",
    ]),
)


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------


class TestNormalizationIdempotentAndLowercase:
    """Feature: project-auto-tagging, Property 1: Normalization is idempotent and lowercase

    **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 8.6**
    """

    @given(raw=_project_strings)
    @settings(max_examples=100)
    def test_idempotent(self, raw):
        """normalize(normalize(x)) == normalize(x) for any input."""
        once = normalize_project_tag(raw)
        twice = normalize_project_tag(once)
        assert twice == once, (
            f"Not idempotent: normalize({raw!r}) = {once!r}, "
            f"normalize({once!r}) = {twice!r}"
        )

    @given(raw=_project_strings)
    @settings(max_examples=100)
    def test_output_format_invariants(self, raw):
        """Non-None output is lowercase, no whitespace, no path separators,
        no dot prefix, and non-empty."""
        result = normalize_project_tag(raw)
        if result is None:
            return

        assert result == result.lower(), f"Not lowercase: {result!r}"
        assert result == result.strip(), f"Has leading/trailing whitespace: {result!r}"
        assert "/" not in result, f"Contains '/': {result!r}"
        assert "\\" not in result, f"Contains '\\': {result!r}"
        assert not result.startswith("."), f"Starts with dot: {result!r}"
        assert len(result) > 0, "Result is empty string"


# ---------------------------------------------------------------------------
# Strategies for Property 2
# ---------------------------------------------------------------------------

# Strategy: paths whose final component starts with a dot
_dot_prefixed_paths = st.one_of(
    # Bare dot-prefixed names
    st.from_regex(r"\.[a-zA-Z_][a-zA-Z0-9_\-]{0,30}", fullmatch=True),
    # Relative paths ending in a dot-prefixed component
    st.tuples(
        st.from_regex(r"[a-zA-Z0-9_\-]{1,20}(/[a-zA-Z0-9_\-]{1,20}){0,3}", fullmatch=True),
        st.from_regex(r"\.[a-zA-Z_][a-zA-Z0-9_\-]{0,20}", fullmatch=True),
    ).map(lambda t: f"{t[0]}/{t[1]}"),
    # Absolute paths ending in a dot-prefixed component
    st.tuples(
        st.from_regex(r"/[a-zA-Z0-9_\-]{1,20}(/[a-zA-Z0-9_\-]{1,20}){1,3}", fullmatch=True),
        st.from_regex(r"\.[a-zA-Z_][a-zA-Z0-9_\-]{0,20}", fullmatch=True),
    ).map(lambda t: f"{t[0]}/{t[1]}"),
    # Well-known dot-prefixed dirs
    st.sampled_from([
        ".kiro", ".git", ".vscode", ".idea", ".env",
        "path/to/.vscode", "/Users/dev/Projects/.hidden",
    ]),
)

# Strategy: absolute paths with fewer than 3 components
# Components are non-empty segments after splitting on "/".
# < 3 means 0, 1, or 2 non-empty segments.
_short_absolute_paths = st.one_of(
    # Just "/"  (0 components)
    st.just("/"),
    # "/segment" (1 component)
    st.from_regex(r"[a-zA-Z0-9_\-]{1,30}", fullmatch=True).map(lambda s: f"/{s}"),
    # "/seg1/seg2" (2 components)
    st.tuples(
        st.from_regex(r"[a-zA-Z0-9_\-]{1,20}", fullmatch=True),
        st.from_regex(r"[a-zA-Z0-9_\-]{1,20}", fullmatch=True),
    ).map(lambda t: f"/{t[0]}/{t[1]}"),
    # Well-known short absolute paths
    st.sampled_from([
        "/", "/Users", "/Users/example", "/home/user", "/tmp",
    ]),
)


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------


class TestNormalizationExcludesDotPrefixedAndShortPaths:
    """Feature: project-auto-tagging, Property 2: Normalization excludes dot-prefixed and short paths

    **Validates: Requirements 8.5, 8.6**
    """

    # Feature: project-auto-tagging, Property 2: Normalization excludes dot-prefixed and short paths

    @given(raw=_dot_prefixed_paths)
    @settings(max_examples=100)
    def test_dot_prefixed_returns_none(self, raw):
        """Any path whose final component starts with a dot normalizes to None."""
        result = normalize_project_tag(raw)
        assert result is None, (
            f"Expected None for dot-prefixed input {raw!r}, got {result!r}"
        )

    @given(raw=_short_absolute_paths)
    @settings(max_examples=100)
    def test_short_absolute_path_returns_none(self, raw):
        """Any absolute path with < 3 components normalizes to None."""
        result = normalize_project_tag(raw)
        assert result is None, (
            f"Expected None for short absolute path {raw!r}, got {result!r}"
        )


# ---------------------------------------------------------------------------
# Strategies for Property 3
# ---------------------------------------------------------------------------

from src.parsers.ide_chat import extract_project_context

# Strategy: non-empty path segments for building expandedPaths entries
_path_segment = st.from_regex(r"[a-zA-Z0-9_\-\.]{1,30}", fullmatch=True)

# Strategy: a single expandedPaths entry like "ProjectDir/src/main.py"
_expanded_path = st.tuples(
    _path_segment,
    st.lists(_path_segment, min_size=0, max_size=3),
).map(lambda t: "/".join([t[0]] + t[1]))

# Strategy: a fileTree context entry with non-empty expandedPaths
_file_tree_context = st.lists(_expanded_path, min_size=1, max_size=5).map(
    lambda paths: {"type": "fileTree", "expandedPaths": paths}
)

# Strategy: a non-fileTree context entry (noise)
_other_context = st.fixed_dictionaries({
    "type": st.sampled_from(["selection", "openFiles", "terminal"]),
})

# Strategy: metadata block
_metadata = st.fixed_dictionaries({
    "modelId": st.text(min_size=0, max_size=30),
    "workflow": st.sampled_from(["chat", "task", "spec", ""]),
    "startTime": st.integers(min_value=1_000_000_000_000, max_value=2_000_000_000_000),
    "endTime": st.integers(min_value=1_000_000_000_000, max_value=2_000_000_000_000),
})

# Strategy: .chat data WITH a fileTree context (project should be derived)
_chat_data_with_filetree = st.fixed_dictionaries({
    "context": st.tuples(
        st.lists(_other_context, min_size=0, max_size=2),
        _file_tree_context,
        st.lists(_other_context, min_size=0, max_size=2),
    ).map(lambda t: t[0] + [t[1]] + t[2]),
    "metadata": _metadata,
})

# Strategy: .chat data WITHOUT a fileTree context (project should be None)
_chat_data_without_filetree = st.one_of(
    # No context key at all
    st.fixed_dictionaries({"metadata": _metadata}),
    # Empty context list
    st.fixed_dictionaries({
        "context": st.just([]),
        "metadata": _metadata,
    }),
    # Context with only non-fileTree entries
    st.fixed_dictionaries({
        "context": st.lists(_other_context, min_size=1, max_size=3),
        "metadata": _metadata,
    }),
    # fileTree with empty expandedPaths
    st.fixed_dictionaries({
        "context": st.just([{"type": "fileTree", "expandedPaths": []}]),
        "metadata": _metadata,
    }),
)


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------


class TestIDEParserExtractionPreservesNormalizedProject:
    """Feature: project-auto-tagging, Property 3: IDE parser extraction preserves normalized project

    **Validates: Requirements 1.1, 1.2, 1.3**
    """

    @given(data=_chat_data_with_filetree)
    @settings(max_examples=100)
    def test_project_hint_matches_normalized_first_path(self, data):
        """When fileTree context has non-empty expandedPaths, project_hint
        equals normalize_project_tag applied to the first path's top-level dir."""
        result = extract_project_context(data)

        # Find the fileTree entry (our strategy guarantees exactly one)
        file_tree = next(c for c in data["context"] if c.get("type") == "fileTree")
        first_path = file_tree["expandedPaths"][0]
        raw_top_dir = first_path.split("/")[0]
        expected = normalize_project_tag(raw_top_dir)

        assert result["project_hint"] == expected, (
            f"project_hint={result['project_hint']!r} != "
            f"normalize({raw_top_dir!r})={expected!r} "
            f"(first_path={first_path!r})"
        )

    @given(data=_chat_data_without_filetree)
    @settings(max_examples=100)
    def test_project_hint_none_without_filetree(self, data):
        """When no fileTree context or expandedPaths is empty, project_hint is None."""
        result = extract_project_context(data)
        assert result["project_hint"] is None, (
            f"Expected project_hint=None, got {result['project_hint']!r} "
            f"for context={data.get('context', 'MISSING')!r}"
        )


# ---------------------------------------------------------------------------
# Strategies for Property 4
# ---------------------------------------------------------------------------

from src.parsers.ide_chat import format_as_markdown as ide_format_as_markdown
from src.parsers.cli_chat import format_as_markdown as cli_format_as_markdown

# Non-empty project strings (valid, already-normalized project names)
_non_empty_project = st.from_regex(r"[a-z][a-z0-9_\-]{0,29}", fullmatch=True)

# Minimal messages list — just enough to produce valid markdown
_minimal_messages = st.just([("human", "hello"), ("bot", "hi there")])

# Timestamp in milliseconds (must be > 1e12 for IDE formatter's ms-detection)
_timestamp_ms = st.integers(min_value=1_600_000_000_000, max_value=1_800_000_000_000)

# IDE meta dict WITH a truthy project_hint
_ide_meta_with_project = st.fixed_dictionaries({
    "model": st.just("test-model"),
    "workflow": st.just("chat"),
    "project_hint": _non_empty_project,
})

# IDE meta dict WITHOUT project_hint (None)
_ide_meta_without_project = st.fixed_dictionaries({
    "model": st.just("test-model"),
    "workflow": st.just("chat"),
    "project_hint": st.none(),
})

# CLI conversation_id (just a simple identifier)
_conversation_id = st.from_regex(r"[a-zA-Z0-9_/\-]{1,50}", fullmatch=True)


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------


class TestMarkdownIncludesProjectHeaderIffNonNull:
    """Feature: project-auto-tagging, Property 4: Markdown formatting includes Project header iff project is non-NULL

    **Validates: Requirements 1.4, 2.5**
    """

    # --- IDE formatter ---

    @given(
        meta=_ide_meta_with_project,
        messages=_minimal_messages,
        timestamp=_timestamp_ms,
    )
    @settings(max_examples=100)
    def test_ide_includes_project_header_when_truthy(self, meta, messages, timestamp):
        """IDE formatter emits 'Project: <value>' when project_hint is truthy."""
        md = ide_format_as_markdown("test-file", messages, meta, timestamp)
        assert any(
            line.startswith("Project: ") for line in md.splitlines()
        ), f"Expected 'Project: ' header in IDE output for meta={meta!r}"

    @given(
        meta=_ide_meta_without_project,
        messages=_minimal_messages,
        timestamp=_timestamp_ms,
    )
    @settings(max_examples=100)
    def test_ide_excludes_project_header_when_none(self, meta, messages, timestamp):
        """IDE formatter does NOT emit 'Project:' when project_hint is None."""
        md = ide_format_as_markdown("test-file", messages, meta, timestamp)
        assert not any(
            line.startswith("Project:") for line in md.splitlines()
        ), f"Unexpected 'Project:' header in IDE output for meta={meta!r}"

    # --- CLI formatter ---

    @given(
        project=_non_empty_project,
        conv_id=_conversation_id,
        messages=_minimal_messages,
        timestamp=_timestamp_ms,
    )
    @settings(max_examples=100)
    def test_cli_includes_project_header_when_non_none(self, project, conv_id, messages, timestamp):
        """CLI formatter emits 'Project: <value>' when project is not None."""
        md = cli_format_as_markdown(conv_id, messages, timestamp, project=project)
        assert any(
            line.startswith("Project: ") for line in md.splitlines()
        ), f"Expected 'Project: ' header in CLI output for project={project!r}"

    @given(
        conv_id=_conversation_id,
        messages=_minimal_messages,
        timestamp=_timestamp_ms,
    )
    @settings(max_examples=100)
    def test_cli_excludes_project_header_when_none(self, conv_id, messages, timestamp):
        """CLI formatter does NOT emit 'Project:' when project is None."""
        md = cli_format_as_markdown(conv_id, messages, timestamp, project=None)
        assert not any(
            line.startswith("Project:") for line in md.splitlines()
        ), f"Unexpected 'Project:' header in CLI output when project=None"


# ---------------------------------------------------------------------------
# Strategies for Property 5
# ---------------------------------------------------------------------------

from unittest.mock import patch, MagicMock
from src.ingest import ingest_content

# Strategy: valid project-like strings for headers and explicit params
_project_like = st.from_regex(r"[a-zA-Z][a-zA-Z0-9_\- ]{0,29}", fullmatch=True)

# Strategy: body text long enough to not be empty
_body_text = st.from_regex(r"[A-Za-z ]{20,80}", fullmatch=True)


def _build_markdown(title: str, header_project: str | None, body: str) -> str:
    """Build markdown content with optional Project: header."""
    lines = [f"# {title}", "", "Source: test", "Type: source"]
    if header_project is not None:
        lines.append(f"Project: {header_project}")
    lines += ["", "---", "", body]
    return "\n".join(lines)


# Patch targets for ingest dependencies
_INGEST_PATCHES = {
    "src.ingest.generate_embedding": lambda text: [0.0] * 1024,
    "src.ingest.classify_memory": lambda *a, **kw: "source",
    "src.ingest.compute_depth_score": lambda *a, **kw: 0.5,
    "src.ingest.search_similar": lambda *a, **kw: [],
    "src.ingest.get_memory": lambda *a, **kw: None,
}


# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------


class TestIngestionProjectResolutionExplicitOverridesHeader:
    """Feature: project-auto-tagging, Property 5: Ingestion project resolution — explicit overrides header

    **Validates: Requirements 4.1, 4.2, 4.3**
    """

    @given(
        header_project=_project_like,
        explicit_project=_project_like,
        body=_body_text,
    )
    @settings(max_examples=100)
    def test_explicit_param_overrides_header(self, header_project, explicit_project, body):
        """When both explicit project param AND Project: header are present,
        stored project should be normalize_project_tag(explicit_param)."""
        content = _build_markdown("Test Title", header_project, body)
        captured_projects = []

        def fake_create_memory(**kwargs):
            captured_projects.append(kwargs.get("project"))
            return "fake-uuid-1234"

        with patch("src.ingest.create_memory", side_effect=fake_create_memory), \
             patch("src.ingest.generate_embedding", _INGEST_PATCHES["src.ingest.generate_embedding"]), \
             patch("src.ingest.classify_memory", _INGEST_PATCHES["src.ingest.classify_memory"]), \
             patch("src.ingest.compute_depth_score", _INGEST_PATCHES["src.ingest.compute_depth_score"]), \
             patch("src.ingest.search_similar", _INGEST_PATCHES["src.ingest.search_similar"]), \
             patch("src.ingest.get_memory", _INGEST_PATCHES["src.ingest.get_memory"]):
            ingest_content(content, source_type="test", project=explicit_project)

        expected = normalize_project_tag(explicit_project)
        # Parent record is the first call
        assert len(captured_projects) >= 1, "Expected at least one create_memory call"
        assert captured_projects[0] == expected, (
            f"Parent project={captured_projects[0]!r} != "
            f"normalize({explicit_project!r})={expected!r}"
        )

    @given(
        header_project=_project_like,
        body=_body_text,
    )
    @settings(max_examples=100)
    def test_header_used_when_no_explicit_param(self, header_project, body):
        """When no explicit project param (None), but Project: header present,
        stored project should be normalize_project_tag(header_value)."""
        content = _build_markdown("Test Title", header_project, body)
        captured_projects = []

        def fake_create_memory(**kwargs):
            captured_projects.append(kwargs.get("project"))
            return "fake-uuid-1234"

        with patch("src.ingest.create_memory", side_effect=fake_create_memory), \
             patch("src.ingest.generate_embedding", _INGEST_PATCHES["src.ingest.generate_embedding"]), \
             patch("src.ingest.classify_memory", _INGEST_PATCHES["src.ingest.classify_memory"]), \
             patch("src.ingest.compute_depth_score", _INGEST_PATCHES["src.ingest.compute_depth_score"]), \
             patch("src.ingest.search_similar", _INGEST_PATCHES["src.ingest.search_similar"]), \
             patch("src.ingest.get_memory", _INGEST_PATCHES["src.ingest.get_memory"]):
            ingest_content(content, source_type="test", project=None)

        expected = normalize_project_tag(header_project)
        assert len(captured_projects) >= 1, "Expected at least one create_memory call"
        assert captured_projects[0] == expected, (
            f"Parent project={captured_projects[0]!r} != "
            f"normalize({header_project!r})={expected!r}"
        )

    @given(body=_body_text)
    @settings(max_examples=100)
    def test_none_when_neither_param_nor_header(self, body):
        """When neither explicit param nor Project: header, stored project should be None."""
        content = _build_markdown("Test Title", None, body)
        captured_projects = []

        def fake_create_memory(**kwargs):
            captured_projects.append(kwargs.get("project"))
            return "fake-uuid-1234"

        with patch("src.ingest.create_memory", side_effect=fake_create_memory), \
             patch("src.ingest.generate_embedding", _INGEST_PATCHES["src.ingest.generate_embedding"]), \
             patch("src.ingest.classify_memory", _INGEST_PATCHES["src.ingest.classify_memory"]), \
             patch("src.ingest.compute_depth_score", _INGEST_PATCHES["src.ingest.compute_depth_score"]), \
             patch("src.ingest.search_similar", _INGEST_PATCHES["src.ingest.search_similar"]), \
             patch("src.ingest.get_memory", _INGEST_PATCHES["src.ingest.get_memory"]):
            ingest_content(content, source_type="test", project=None)

        assert len(captured_projects) >= 1, "Expected at least one create_memory call"
        assert captured_projects[0] is None, (
            f"Parent project={captured_projects[0]!r}, expected None"
        )


# ---------------------------------------------------------------------------
# Strategies for Property 6
# ---------------------------------------------------------------------------

# Strategy: long paragraph text (> 100 chars to make sections substantial)
_long_paragraph = st.text(
    alphabet=string.ascii_letters + " ", min_size=200, max_size=400
)

# Strategy: number of sections (at least 3 to guarantee multiple chunks)
_num_sections = st.integers(min_value=3, max_value=6)


def _build_multi_section_markdown(
    title: str,
    project: str | None,
    section_bodies: list[str],
) -> str:
    """Build markdown with multiple ## sections to force chunking."""
    lines = [f"# {title}", "", "Source: test", "Type: source"]
    if project is not None:
        lines.append(f"Project: {project}")
    lines += ["", "---", ""]
    for i, body in enumerate(section_bodies, 1):
        lines += [f"## Section {i}", "", body, ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------


class TestParentChildProjectConsistency:
    """Feature: project-auto-tagging, Property 6: Parent-child project consistency in ingestion

    **Validates: Requirements 4.4, 7.1**
    """

    @given(
        project=_project_like,
        section_bodies=st.lists(_long_paragraph, min_size=3, max_size=6),
    )
    @settings(max_examples=100)
    def test_all_create_memory_calls_have_same_project(self, project, section_bodies):
        """When ingesting content that produces multiple chunks, every
        create_memory call (parent + chunks) receives the same project value."""
        content = _build_multi_section_markdown("Multi Section Doc", project, section_bodies)
        captured_projects = []

        def fake_create_memory(**kwargs):
            captured_projects.append(kwargs.get("project"))
            return "fake-uuid-1234"

        with patch("src.ingest.create_memory", side_effect=fake_create_memory), \
             patch("src.ingest.generate_embedding", _INGEST_PATCHES["src.ingest.generate_embedding"]), \
             patch("src.ingest.classify_memory", _INGEST_PATCHES["src.ingest.classify_memory"]), \
             patch("src.ingest.compute_depth_score", _INGEST_PATCHES["src.ingest.compute_depth_score"]), \
             patch("src.ingest.search_similar", _INGEST_PATCHES["src.ingest.search_similar"]), \
             patch("src.ingest.get_memory", _INGEST_PATCHES["src.ingest.get_memory"]):
            ingest_content(content, source_type="test", project=project)

        expected = normalize_project_tag(project)

        # Must have parent + at least 1 chunk
        assert len(captured_projects) >= 2, (
            f"Expected at least 2 create_memory calls (parent + chunks), "
            f"got {len(captured_projects)}"
        )

        # Every call must have the same project value
        for i, proj in enumerate(captured_projects):
            assert proj == expected, (
                f"create_memory call {i} has project={proj!r}, "
                f"expected {expected!r} (normalized from {project!r})"
            )

    @given(
        section_bodies=st.lists(_long_paragraph, min_size=3, max_size=6),
    )
    @settings(max_examples=100)
    def test_all_calls_none_when_no_project(self, section_bodies):
        """When no project is provided, all create_memory calls get project=None."""
        content = _build_multi_section_markdown("No Project Doc", None, section_bodies)
        captured_projects = []

        def fake_create_memory(**kwargs):
            captured_projects.append(kwargs.get("project"))
            return "fake-uuid-1234"

        with patch("src.ingest.create_memory", side_effect=fake_create_memory), \
             patch("src.ingest.generate_embedding", _INGEST_PATCHES["src.ingest.generate_embedding"]), \
             patch("src.ingest.classify_memory", _INGEST_PATCHES["src.ingest.classify_memory"]), \
             patch("src.ingest.compute_depth_score", _INGEST_PATCHES["src.ingest.compute_depth_score"]), \
             patch("src.ingest.search_similar", _INGEST_PATCHES["src.ingest.search_similar"]), \
             patch("src.ingest.get_memory", _INGEST_PATCHES["src.ingest.get_memory"]):
            ingest_content(content, source_type="test", project=None)

        assert len(captured_projects) >= 2, (
            f"Expected at least 2 create_memory calls (parent + chunks), "
            f"got {len(captured_projects)}"
        )

        for i, proj in enumerate(captured_projects):
            assert proj is None, (
                f"create_memory call {i} has project={proj!r}, expected None"
            )


# ---------------------------------------------------------------------------
# Backfill test imports and helpers
# ---------------------------------------------------------------------------

import json
import logging
from unittest.mock import patch

import src.db as db

# Reuse the deterministic embedding from conftest
from tests.conftest import _deterministic_embedding


def _insert_test_memory(conn, source_type, source_url=None, content="Test content",
                        metadata=None, parent_id=None, project=None):
    """Insert a memory directly via SQL for backfill testing. Returns UUID string."""
    meta_json = json.dumps(metadata or {})
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO memories (type, title, content, source_url, source_type,
                                  metadata, parent_id, project)
            VALUES ('source', 'Test', %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (content, source_url, source_type, meta_json, parent_id, project))
        return str(cur.fetchone()[0])


def _get_project(conn, mem_id):
    """Read the project column for a memory."""
    with conn.cursor() as cur:
        cur.execute("SELECT project FROM memories WHERE id = %s", (mem_id,))
        row = cur.fetchone()
        return row[0] if row else None


# Quiet logger for backfill functions
_backfill_log = logging.getLogger("backfill_test")
_backfill_log.addHandler(logging.NullHandler())


def _clean_memories(conn):
    """Truncate test memories between Hypothesis examples."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM memory_relationships")
        cur.execute("DELETE FROM memories WHERE parent_id IS NOT NULL")
        cur.execute("DELETE FROM memories")
    conn.commit()


# ---------------------------------------------------------------------------
# Property 7: Backfill idempotency
# ---------------------------------------------------------------------------


class TestBackfillIdempotency:
    """Feature: project-auto-tagging, Property 7: Backfill idempotency

    Running the backfill twice produces the same project column values as
    running it once. Formally: backfill(backfill(state)) == backfill(state).

    **Validates: Requirements 6.12**
    """

    @given(
        project_name=st.sampled_from([
            "retailstore", "myapp", "dashboard", "api-service",
        ]),
        workspace_path=st.sampled_from([
            "/Users/dev/Projects/RetailStore",
            "/Users/dev/Projects/MyApp",
            "/Users/dev/Projects/Dashboard",
            "/Users/dev/Projects/ApiService",
        ]),
    )
    @settings(max_examples=25, deadline=None)
    def test_ide_backfill_idempotent(self, test_db, project_name, workspace_path):
        """IDE backfill produces identical results when run twice."""
        content = f"# Chat: test\n\nProject: {project_name}\n\n---\n\nSome content here."

        with db.get_connection() as conn:
            _clean_memories(conn)

            parent_id = _insert_test_memory(
                conn, "kiro_ide_chat", source_url="ide_testchat.md", content=content,
            )
            child_id = _insert_test_memory(
                conn, "kiro_ide_chat", source_url="ide_testchat.md",
                content="chunk content", parent_id=parent_id,
            )
            conn.commit()

            with patch("scripts.backfill_projects._build_chat_file_index", return_value={}):
                from scripts.backfill_projects import backfill_ide_chats

                # Run 1
                backfill_ide_chats(conn, dry_run=False, log=_backfill_log)
                conn.commit()
                project_after_run1_parent = _get_project(conn, parent_id)
                project_after_run1_child = _get_project(conn, child_id)

                # Run 2
                backfill_ide_chats(conn, dry_run=False, log=_backfill_log)
                conn.commit()
                project_after_run2_parent = _get_project(conn, parent_id)
                project_after_run2_child = _get_project(conn, child_id)

            assert project_after_run1_parent == project_after_run2_parent, (
                f"Parent project changed between runs: "
                f"{project_after_run1_parent!r} → {project_after_run2_parent!r}"
            )
            assert project_after_run1_child == project_after_run2_child, (
                f"Child project changed between runs: "
                f"{project_after_run1_child!r} → {project_after_run2_child!r}"
            )

    @given(
        workspace_path=st.sampled_from([
            "/Users/dev/Projects/RetailStore",
            "/Users/dev/Projects/MyApp",
            "/Users/dev/Code/Dashboard",
            "/home/user/workspace/ApiService",
        ]),
    )
    @settings(max_examples=25, deadline=None)
    def test_cli_backfill_idempotent(self, test_db, workspace_path):
        """CLI backfill produces identical results when run twice."""
        metadata = {"source_id": workspace_path}

        with db.get_connection() as conn:
            _clean_memories(conn)

            parent_id = _insert_test_memory(
                conn, "kiro_cli_chat", metadata=metadata,
            )
            child_id = _insert_test_memory(
                conn, "kiro_cli_chat", parent_id=parent_id,
            )
            conn.commit()

            from scripts.backfill_projects import backfill_cli_chats

            # Run 1
            backfill_cli_chats(conn, dry_run=False, log=_backfill_log)
            conn.commit()
            project_after_run1_parent = _get_project(conn, parent_id)
            project_after_run1_child = _get_project(conn, child_id)

            # Run 2
            backfill_cli_chats(conn, dry_run=False, log=_backfill_log)
            conn.commit()
            project_after_run2_parent = _get_project(conn, parent_id)
            project_after_run2_child = _get_project(conn, child_id)

            assert project_after_run1_parent == project_after_run2_parent, (
                f"Parent project changed between runs: "
                f"{project_after_run1_parent!r} → {project_after_run2_parent!r}"
            )
            assert project_after_run1_child == project_after_run2_child, (
                f"Child project changed between runs: "
                f"{project_after_run1_child!r} → {project_after_run2_child!r}"
            )


# ---------------------------------------------------------------------------
# Property 8: Non-chat memories remain NULL after backfill
# ---------------------------------------------------------------------------


class TestNonChatMemoriesRemainNull:
    """Feature: project-auto-tagging, Property 8: Non-chat memories remain NULL after backfill

    For any memory with source_type not in ('kiro_ide_chat', 'kiro_cli_chat'),
    the backfill script does not modify its project column — it remains NULL.

    **Validates: Requirements 6.9**
    """

    @given(
        source_type=st.sampled_from([
            "youtube", "manual", "article", "pdf", "notes", "course",
        ]),
    )
    @settings(max_examples=25, deadline=None)
    def test_non_chat_memories_stay_null(self, test_db, source_type):
        """Non-chat memories retain project=NULL after both IDE and CLI backfill phases."""
        with db.get_connection() as conn:
            _clean_memories(conn)

            mem_id = _insert_test_memory(
                conn, source_type, content="Some non-chat content",
            )
            conn.commit()

            with patch("scripts.backfill_projects._build_chat_file_index", return_value={}):
                from scripts.backfill_projects import backfill_ide_chats, backfill_cli_chats

                backfill_ide_chats(conn, dry_run=False, log=_backfill_log)
                backfill_cli_chats(conn, dry_run=False, log=_backfill_log)
                conn.commit()

            project = _get_project(conn, mem_id)
            assert project is None, (
                f"Non-chat memory (source_type={source_type!r}) should have "
                f"project=NULL after backfill, got {project!r}"
            )

    @given(
        source_type=st.sampled_from([
            "youtube", "manual", "article", "pdf", "notes", "course",
        ]),
    )
    @settings(max_examples=25, deadline=None)
    def test_non_chat_with_project_header_still_null(self, test_db, source_type):
        """Even if non-chat content has a Project: header, backfill ignores it."""
        content = "# Test\n\nProject: retailstore\n\n---\n\nContent with project header."
        with db.get_connection() as conn:
            _clean_memories(conn)

            mem_id = _insert_test_memory(
                conn, source_type, content=content,
            )
            conn.commit()

            with patch("scripts.backfill_projects._build_chat_file_index", return_value={}):
                from scripts.backfill_projects import backfill_ide_chats, backfill_cli_chats

                backfill_ide_chats(conn, dry_run=False, log=_backfill_log)
                backfill_cli_chats(conn, dry_run=False, log=_backfill_log)
                conn.commit()

            project = _get_project(conn, mem_id)
            assert project is None, (
                f"Non-chat memory (source_type={source_type!r}) with Project: header "
                f"should still have project=NULL after backfill, got {project!r}"
            )
