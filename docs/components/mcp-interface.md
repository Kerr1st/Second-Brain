# MCP Interface Component

> **Status: canonical component contract.** Last reviewed: 2026-07-23.

The MCP Interface exposes Second Brain capabilities to connected AI agents
through one stdio Model Context Protocol server.

## Boundary

The interface owns:

- MCP tool names, parameters, return shapes, and descriptions;
- stdio server startup;
- orchestration of interactive memory, retrieval, graph, learning, and
  briefing operations; and
- the trust boundary presented to connected agents.

It does not own background source capture, scheduled Dream Cycles, provider
selection, or the internal implementation of search and delivery algorithms.

## Contract

`python -m src.mcp_server` exposes nine tools:

| Tool | Capability |
|---|---|
| `memory_create` | Create and embed a memory |
| `memory_search` | Run hybrid retrieval and return ranked previews |
| `memory_read` | Read one complete memory |
| `memory_update` | Update an existing memory |
| `memory_relate` | Create a typed relationship |
| `memory_list` | List memories with filters |
| `memory_graph` | Read relationships around a memory |
| `memory_learn` | Elaborate and deepen a memory |
| `memory_brief` | Return an Express briefing |

The transport is stdio. The MCP server does not open a listening network port.

## Runtime flow

```text
connected agent
  → MCP tool request
  → validate and normalize arguments
  → call Ingestion & Storage, Retrieval, or Delivery
  → serialize structured result
  → return over stdio
```

Agentic model backends may start this MCP server as a required subprocess. The
Model Execution component owns that attachment and sandbox configuration.

## Current physical seam

`memory_create` currently composes embedding, classification, depth scoring,
project normalization, and database creation directly. The componentization
roadmap tracks its convergence with the generic ingestion storage primitive.
Until that refactor lands, this direct path is documented rather than hidden.

## Entry points and configuration

| Purpose | Entry point |
|---|---|
| MCP server and tool definitions | `src/mcp_server.py` |
| Standalone server wrapper | `scripts/jobs/mcp_serve.sh` |
| Kiro agent configuration | `.kiro/agents/` |
| Backend-driven MCP attachment | `src/backends/kiro.py`, `src/backends/claude_code.py`, `src/backends/codex.py` |

## Tests

- `tests/test_mcp_server.py`
- `tests/test_mcp_probe.py`
- `tests/test_integration.py`
- backend-specific tool attachment and fail-loud tests

## Related

- [Architecture Component Index](index.md)
- [Connect an AI agent](../user-guide/connect-ai-agent.md)
- [MCP tool reference](../user-guide/reference.md)
- [Security model](../user-guide/security-model.md)
- [Model Execution](model-execution.md)
- [Componentization roadmap](../COMPONENTIZATION-PLAN.md)
