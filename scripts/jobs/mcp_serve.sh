#!/bin/bash
# Second Brain MCP server launcher (stdio), for Kiro CLI / Claude Code clients.
# Uses the project venv: system python3 does not have the pinned mcp/psycopg2.
cd /Users/kerrfir/second-brain
# Memory search embeds the query on Bedrock; use the jobs profile so search keeps
# working when the interactive SSO session has lapsed.
export AWS_PROFILE="${AWS_PROFILE:-second-brain-jobs}"
exec .venv/bin/python -m src.mcp_server
