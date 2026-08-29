---
title: "Connect an AI Agent"
type: tutorial
---

# Connect an AI Agent

Connect an MCP client (Kiro CLI or Claude Code) to the Second Brain MCP server so your agent can invoke the 9 memory tools.

## Prerequisites

> [!NOTE]
> Before you begin, ensure you have:
> - Second Brain installed and the MCP server startable per [Getting started](getting-started.md) steps 1–4
> - PostgreSQL running: `pg_isready -h 127.0.0.1 -p 5432 -U memory_bank`
> - A valid AWS SSO session: `aws sts get-caller-identity`
> - Either [Kiro CLI](https://kiro.dev) or [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed

## The MCP server config

Both clients use the same stdio server definition. The three values you need:

| Key | Value |
|-----|-------|
| `command` | Path to the Python binary in your venv |
| `args` | `["-m", "src.mcp_server"]` |
| `cwd` | Absolute path to your `second-brain` repo clone |

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "/Users/<you>/second-brain/.venv/bin/python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/Users/<you>/second-brain"
    }
  }
}
```

Replace `/Users/<you>/second-brain` with the actual path to your clone.

> [!WARNING]
> Do not use a system Python — always point `command` at the `.venv/bin/python` inside the repo so dependencies resolve correctly.

## Kiro CLI

Add the server to your Kiro CLI MCP configuration. Place the stdio block from above into your MCP settings file:

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "/Users/<you>/second-brain/.venv/bin/python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/Users/<you>/second-brain"
    }
  }
}
```

> [!NOTE]
> The exact config file location depends on your Kiro CLI version. Check `~/.kiro/settings.json` or a project-level `.kiro/mcp.json`. Consult the [Kiro CLI documentation](https://kiro.dev) for the current path.

### Verify (Kiro CLI)

Start a Kiro CLI session and ask:

> "List the available MCP tools."

Expected result — the agent lists all 9 Second Brain tools:

```text
memory_create, memory_search, memory_read, memory_update,
memory_relate, memory_list, memory_graph, memory_learn, memory_brief
```

Then confirm a round-trip:

> "Use memory_list to show recent memories."

The agent calls `memory_list` and returns results (or an empty list if this is a fresh install).

## Claude Code

Claude Code registers MCP servers via an MCP config file or the `claude mcp add` CLI. The exact location depends on your version — commonly a project-root `.mcp.json` or `~/.claude.json`. Check the [Claude Code MCP documentation](https://docs.anthropic.com/en/docs/claude-code) for the current path, then add the same stdio block shown above (adjust the repo path to your clone).

> [!TIP]
> If your Claude Code version provides the `claude mcp add` command, it writes the config to the correct location for you.

### Verify (Claude Code)

Start Claude Code and ask:

> "What MCP tools are available?"

Expected result — Claude Code lists the 11 tools. Then confirm a tool call:

> "Call memory_search with query 'test'."

The agent executes the search and returns results (or an empty result set).

## If it doesn't connect

Common issues and fixes:

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| "Server not found" or tool list is empty | Config file in wrong location or malformed JSON | Validate JSON syntax; confirm the file path matches your client version |
| "No module named src" | `cwd` is wrong | Set `cwd` to the repo root (the directory containing `src/`) |
| "ModuleNotFoundError: No module named 'mcp'" | `command` points to system Python, not the venv | Use the full path: `/Users/<you>/second-brain/.venv/bin/python` |
| "connection refused" on port 5432 | PostgreSQL not running | Run `brew services start postgresql@17` |
| Bedrock / embedding errors | Expired AWS SSO session | Run `aws sso login --profile default` |

For additional diagnostics, see [Troubleshooting](troubleshoot.md).

## Related

- [Getting started](getting-started.md) — full install walkthrough (this page expands step 5)
- [Your first memory](first-memory.md) — guided lifecycle of a memory after connecting
- [Reference](reference.md) — complete MCP tool specifications
- [Troubleshooting](troubleshoot.md) — extended diagnostics
