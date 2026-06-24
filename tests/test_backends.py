"""Tests for the backend abstraction contract (src/backends/base.py).

Validates the capability invariants and the Explorer-needs-tools guard.
See docs/MODEL-BACKENDS.md.
"""

from __future__ import annotations

import pytest

from src.backends import (
    BACKEND_CAPABILITIES,
    BackendCapabilities,
    Invoker,
    assert_backend_supports_role,
)


class TestBackendCapabilities:
    def test_metered_requires_reports_usage(self):
        """A metered backend that cannot report usage is rejected at construction
        — never run a metered seat you can't meter."""
        with pytest.raises(ValueError):
            BackendCapabilities(
                supports_mcp=False, metered=True, structured_output=False, reports_usage=False
            )

    def test_all_declared_backends_satisfy_invariant(self):
        assert BACKEND_CAPABILITIES  # non-empty
        for name, caps in BACKEND_CAPABILITIES.items():
            assert not (caps.metered and not caps.reports_usage), name

    def test_known_backends_present(self):
        assert {"kiro", "claude_code", "codex", "bedrock"} <= set(BACKEND_CAPABILITIES)

    def test_kiro_is_free_agentic_without_usage(self):
        kiro = BACKEND_CAPABILITIES["kiro"]
        assert kiro.supports_mcp is True
        assert kiro.metered is False
        assert kiro.reports_usage is False

    def test_bedrock_is_toolless_metered_with_usage(self):
        bed = BACKEND_CAPABILITIES["bedrock"]
        assert bed.supports_mcp is False
        assert bed.metered is True
        assert bed.reports_usage is True


class TestExplorerGuard:
    def test_explorer_rejected_on_toolless_backend(self):
        """Explorer requires live tools → a tool-less Direct-API backend is refused."""
        with pytest.raises(ValueError):
            assert_backend_supports_role("bedrock", "explorer")

    def test_explorer_allowed_on_agentic_backend(self):
        # Should not raise.
        assert_backend_supports_role("kiro", "explorer")
        assert_backend_supports_role("claude_code", "explorer")

    def test_toolless_stage_allowed_on_toolless_backend(self):
        # Evaluators / Express are tool-less → any backend is fine.
        assert_backend_supports_role("bedrock", "skeptic")
        assert_backend_supports_role("bedrock", "methodologist")

    def test_unknown_backend_raises(self):
        with pytest.raises(KeyError):
            assert_backend_supports_role("nonexistent", "skeptic")


class TestInvokerProtocol:
    def test_duck_typed_invoker_satisfies_protocol(self):
        """A class with a matching invoke() is a structural Invoker."""

        class Dummy:
            def invoke(self, system_prompt, user_message, *, tools=False,
                       timeout=300, effort=None, stage=None, run_id=None):
                return {"output": None, "raw": "", "usage": None, "usage_source": "estimate"}

        assert isinstance(Dummy(), Invoker)

    def test_non_invoker_fails_protocol(self):
        class NotAnInvoker:
            pass

        assert not isinstance(NotAnInvoker(), Invoker)
