# Corpus routing: design

resolve_stage selects the specification/invoker; invoke_metered repeats route checks at the spending boundary. Base adapters independently deny forbidden corpus destinations even for direct callers.

Claude Code receives the configured model id, `--strict-mcp-config` and, for the Explorer, a temporary MCP config that starts `python -m src.mcp_server` from the repository root; tool-less calls add `--tools ""`. A `failed to set model` report on stderr marks `model_honored = false` and raises `CorpusRoutingError` for corpus stages after usage is billed. The read-only registry exists but is not yet the Explorer's server.

Code authority: [config/backends.toml](../../../config/backends.toml), [src/backends/resolver.py](../../../src/backends/resolver.py), [src/backends/base.py](../../../src/backends/base.py), [src/backends/claude_code.py](../../../src/backends/claude_code.py), [src/mcp_readonly.py](../../../src/mcp_readonly.py).

Regression authority: [tests/test_resolver.py](../../../tests/test_resolver.py), [tests/test_corpus_routing_guard.py](../../../tests/test_corpus_routing_guard.py), [tests/test_consumer_routing.py](../../../tests/test_consumer_routing.py), [tests/test_claude_code.py](../../../tests/test_claude_code.py), [tests/test_readonly_discovery.py](../../../tests/test_readonly_discovery.py). Test presence is not a passing result.
