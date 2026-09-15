#!/bin/bash
# Second Brain MCP server launcher (stdio), for Kiro CLI / Claude Code clients.
# Uses the project venv: system python3 does not have the pinned mcp/psycopg2.
cd /Users/kerrfir/second-brain
exec .venv/bin/python -m src.mcp_server
