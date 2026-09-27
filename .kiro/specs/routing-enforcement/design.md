# Corpus routing: design

resolve_stage selects the specification/invoker; invoke_metered repeats route checks at the spending boundary. Base adapters independently deny forbidden corpus destinations even for direct callers.

Claude receives canonical version IDs, isolation flags and strict MCP configuration. Exact permissions match the read-only server. Native served IDs are checked before accepting output, while reported usage survives route rejection.

Code authority: [config/backends.toml](../../../config/backends.toml), [src/backends/resolver.py](../../../src/backends/resolver.py), [src/backends/base.py](../../../src/backends/base.py), [src/backends/claude_code.py](../../../src/backends/claude_code.py), [src/mcp_readonly.py](../../../src/mcp_readonly.py).

Regression authority: [tests/test_resolver.py](../../../tests/test_resolver.py), [tests/test_corpus_routing_guard.py](../../../tests/test_corpus_routing_guard.py), [tests/test_consumer_routing.py](../../../tests/test_consumer_routing.py), [tests/test_claude_code.py](../../../tests/test_claude_code.py), [tests/test_readonly_discovery.py](../../../tests/test_readonly_discovery.py). Test presence is not a passing result.
