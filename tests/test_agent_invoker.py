"""Property-based and unit tests for AgentInvoker.parse_json_output.

**Validates: Requirement 13.3**
- WHEN an agent subprocess returns output, THE Agent_Invoker SHALL parse JSON
  from the output, handling markdown code fence wrapping.
"""

import json

import pytest
from hypothesis import given, strategies as st

from src.agent_invoker import AgentInvoker


# ---------------------------------------------------------------------------
# Hypothesis strategies for generating valid JSON values
# ---------------------------------------------------------------------------

json_primitives = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs",),  # exclude surrogates
        ),
        min_size=0,
        max_size=50,
    ),
)

json_values = st.recursive(
    json_primitives,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(
            st.text(
                alphabet=st.characters(blacklist_categories=("Cs",)),
                min_size=1,
                max_size=20,
            ),
            children,
            max_size=5,
        ),
    ),
    max_leaves=15,
)

json_objects = st.dictionaries(
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=1,
        max_size=20,
    ),
    json_values,
    min_size=0,
    max_size=5,
)

json_arrays = st.lists(json_values, min_size=0, max_size=5)

# Strategy that produces either a dict or a list (valid top-level JSON)
json_top_level = st.one_of(json_objects, json_arrays)


# ---------------------------------------------------------------------------
# Wrapping strategies — different formats the agent output might use
# ---------------------------------------------------------------------------

def bare_json(obj):
    """Just the JSON string."""
    return json.dumps(obj)


def markdown_fence_json(obj):
    """```json\n{json}\n```"""
    return f"```json\n{json.dumps(obj)}\n```"


def markdown_fence_no_lang(obj):
    """```\n{json}\n```"""
    return f"```\n{json.dumps(obj)}\n```"


def surrounding_text(obj):
    """JSON with surrounding prose."""
    return f"Here is the output:\n{json.dumps(obj)}\nDone."


def whitespace_padded(obj):
    """JSON with leading/trailing whitespace."""
    return f"  \n\t {json.dumps(obj)}  \n  "


WRAPPERS = [
    bare_json,
    markdown_fence_json,
    markdown_fence_no_lang,
    surrounding_text,
    whitespace_padded,
]

wrapper_strategy = st.sampled_from(WRAPPERS)


# ---------------------------------------------------------------------------
# Property 13: JSON Output Parsing
# **Validates: Requirement 13.3**
# ---------------------------------------------------------------------------

class TestParseJsonOutputProperty:
    """Property-based tests for parse_json_output."""

    @given(obj=json_objects, wrapper=wrapper_strategy)
    def test_extracts_correct_json_object(self, obj, wrapper):
        """For any valid JSON object in any wrapper format,
        parse_json_output returns the original object.

        **Validates: Requirements 13.3**
        """
        raw = wrapper(obj)
        result = AgentInvoker.parse_json_output(raw)
        assert result == obj

    @given(arr=json_arrays, wrapper=wrapper_strategy)
    def test_extracts_correct_json_array(self, arr, wrapper):
        """For any valid JSON array in any wrapper format,
        parse_json_output returns the original array.

        **Validates: Requirements 13.3**
        """
        raw = wrapper(arr)
        result = AgentInvoker.parse_json_output(raw)
        assert result == arr


# ---------------------------------------------------------------------------
# Explicit edge-case tests
# ---------------------------------------------------------------------------

class TestParseJsonOutputEdgeCases:
    """Explicit unit tests for edge cases."""

    def test_empty_object(self):
        assert AgentInvoker.parse_json_output("{}") == {}

    def test_empty_array(self):
        assert AgentInvoker.parse_json_output("[]") == []

    def test_nested_objects(self):
        nested = {"a": {"b": {"c": [1, 2, {"d": True}]}}}
        raw = json.dumps(nested)
        assert AgentInvoker.parse_json_output(raw) == nested

    def test_special_characters_in_strings(self):
        obj = {
            "quote": 'He said "hello"',
            "newline": "line1\nline2",
            "tab": "col1\tcol2",
            "backslash": "path\\to\\file",
            "unicode": "café ☕ 日本語",
        }
        raw = json.dumps(obj)
        assert AgentInvoker.parse_json_output(raw) == obj

    def test_markdown_fence_with_special_chars(self):
        obj = {"key": "value with\nnewlines and \"quotes\""}
        raw = f"```json\n{json.dumps(obj)}\n```"
        assert AgentInvoker.parse_json_output(raw) == obj

    def test_no_valid_json_raises(self):
        with pytest.raises(ValueError, match="No valid JSON found"):
            AgentInvoker.parse_json_output("this is not json at all")


# ---------------------------------------------------------------------------
# Unit tests for AgentInvoker.invoke
# **Validates: Requirements 13.1, 13.2, 13.4, 13.5**
# ---------------------------------------------------------------------------

import subprocess
from unittest.mock import patch, MagicMock


class TestInvokeSuccess:
    """Test invoke with mock subprocess returning valid JSON."""

    @patch("src.agent_invoker.subprocess.run")
    def test_invoke_returns_dict_with_output_and_raw(self, mock_run):
        """Successful invocation returns dict with 'output' (parsed) and 'raw' keys.

        **Validates: Requirements 13.1, 13.3**
        """
        payload = {"candidates": [{"title": "test insight"}]}
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

        invoker = AgentInvoker()
        result = invoker.invoke(
            system_prompt="You are a test agent.",
            user_message="Analyze these memories.",
        )

        assert isinstance(result, dict)
        assert "output" in result
        assert "raw" in result
        assert result["output"] == payload
        assert result["raw"] == json.dumps(payload)


class TestInvokeTimeout:
    """Test invoke timeout raises TimeoutError with stderr content."""

    @patch("src.agent_invoker.subprocess.run")
    def test_timeout_raises_timeout_error_with_stderr(self, mock_run):
        """When subprocess times out, TimeoutError is raised containing stderr.

        **Validates: Requirements 13.2, 13.5**
        """
        exc = subprocess.TimeoutExpired(cmd=["kiro"], timeout=60)
        exc.stderr = "process exceeded time limit"
        mock_run.side_effect = exc

        invoker = AgentInvoker()
        with pytest.raises(TimeoutError, match="process exceeded time limit"):
            invoker.invoke(
                system_prompt="You are a test agent.",
                user_message="input",
                timeout=60,
            )


class TestInvokeCrash:
    """Test invoke crash (non-zero exit code) raises RuntimeError with stderr."""

    @patch("src.agent_invoker.subprocess.run")
    def test_nonzero_exit_raises_runtime_error_with_stderr(self, mock_run):
        """When subprocess exits non-zero, RuntimeError is raised containing stderr.

        **Validates: Requirements 13.5**
        """
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="segfault in agent process",
        )

        invoker = AgentInvoker()
        with pytest.raises(RuntimeError, match="segfault in agent process"):
            invoker.invoke(
                system_prompt="You are a test agent.",
                user_message="input",
            )


class TestMcpConfigPassed:
    """Test MCP config passed to Explorer/Thinker (mcp_config provided)."""

    @patch("src.agent_invoker.subprocess.run")
    def test_mcp_config_adds_flag_to_command(self, mock_run):
        """When mcp_config is provided, --mcp-config flag appears in the command.

        **Validates: Requirements 13.4**
        """
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"result": "ok"}',
            stderr="",
        )

        invoker = AgentInvoker()
        invoker.invoke(
            system_prompt="You are the Explorer.",
            user_message="explore",
            mcp_config="/path/to/mcp.json",
        )

        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert "--mcp-config" in cmd
        mcp_idx = cmd.index("--mcp-config")
        assert cmd[mcp_idx + 1] == "/path/to/mcp.json"


class TestMcpConfigNotPassed:
    """Test MCP config NOT passed to evaluators (mcp_config=None)."""

    @patch("src.agent_invoker.subprocess.run")
    def test_no_mcp_config_omits_flag_from_command(self, mock_run):
        """When mcp_config is None, --mcp-config flag must NOT appear in the command.

        **Validates: Requirements 13.4**
        """
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"verdict": "ACCEPT", "reasoning": "solid evidence"}',
            stderr="",
        )

        invoker = AgentInvoker()
        invoker.invoke(
            system_prompt="You are the Skeptic evaluator.",
            user_message="evaluate this candidate",
            mcp_config=None,
        )

        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert "--mcp-config" not in cmd
