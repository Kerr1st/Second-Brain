---
title: "Reference"
type: reference
---

# Reference

Complete specifications for MCP tools, CLI commands, memory types, relationship types, and configuration.

## MCP Tools

The MCP server exposes 9 tools. All are invoked by an agent (Kiro CLI, Claude Code) through the Model Context Protocol.

| Tool | Purpose | Key arguments | Returns |
|------|---------|---------------|---------|
| `memory_create` | Create a memory and generate its embedding | `type`\*, `title`\*, `content`\*, `tags`, `source_type`, `source_url`, `metadata`, `project`, `encoding_context` | `str` — created ID + depth warnings |
| `memory_search` | Hybrid semantic + keyword search with reranking | `query`\*, `type`, `limit` (default 10), `project`, `source_type`, `since_days`, `status` | `dict` — `{results, temporal_context, schema_context}` |
| `memory_read` | Read full content of a memory by ID | `memory_id`\* | `dict` — all fields (embedding excluded) |
| `memory_update` | Update fields on a memory (pass only changed fields) | `memory_id`\*, `title`, `content`, `status`, `tags`, `summary`, `type` | `str` — confirmation |
| `memory_relate` | Create a typed relationship between two memories | `source_id`\*, `target_id`\*, `relation_type`\*, `note` | `str` — confirmation |
| `memory_list` | Browse recent memories (newest first, no ranking) | `type`, `source_type`, `status`, `limit` (default 20) | `list[dict]` — `[{id, title, type, source_type, created_at, tags}]` |
| `memory_graph` | Get a memory and its outgoing relationships | `memory_id`\* | `dict` — `{memory: {id, title, type}, relationships: [{target_id, relation_type, note}]}` |
| `memory_learn` | Internalize external content (step 1 of a 2-step process) | `content`\*, `topics`\* (comma-separated), `source` | `str` — prompt with related existing memories |
| `memory_brief` | Surface recent synthesis (Express in-session) | `window_days` (default 14), `use_llm` (default false) | `str` — Markdown briefing |

\* = required

### Depth warnings

When you create a memory of type `idea`, `synthesis`, `insight`, or `decision`:

- If `depth_score < 0.3` → warning: "add 'because…' or 'when X, then Y'"
- If content lacks a `Questions this answers:` section → warning

### `memory_graph` scope

`memory_graph` operates on **memory-to-memory relationships** (the `memory_relationships` table). It does not query the dormant entity knowledge graph.

## `brief` CLI

**Invocation:** `.venv/bin/python scripts/brief.py [options]`

### View flags

| Flag | Default | Description |
|------|---------|-------------|
| `--window-days N` | 14 | How far back to pull dream-cycle insights |
| `--no-llm` | off | Skip LLM editor pass; use deterministic ranking |
| `--json` | off | Emit raw composed items as JSON |

### Feedback flags

| Flag | Effect |
|------|--------|
| `--useful <target>` | Boost — rank higher in future briefings |
| `--less <target>` | Soft down-weight — show less prominently |
| `--mute <target>` | Hard hide — never surface |
| `--unmute <target>` | Clear any preference for target |
| `--prefs` | List current preferences |

### What `<target>` means

| Form | Example | Matches |
|------|---------|---------|
| Item id | `#1a2b3c4d` or `1a2b3c4d` | A specific briefing item |
| Kind | `insight`, `contradiction`, `resurface`, `digest`, `question` | All items of that kind |
| Topic/project | `kiro`, `second-brain` | Items tagged with that topic or project |

## `express_push.py`

**Invocation:** `.venv/bin/python scripts/express_push.py [--dry-run]`

Composes and sends a gated Gmail push. Sends **only** when a new cross-project synthesis or contradiction is detected. Chained after the noon dream cycle.

| Flag | Description |
|------|-------------|
| `--dry-run` | Compose and print; never send |

## Memory types

| Type | Purpose |
|------|---------|
| `research` | Raw information gathered |
| `synthesis` | Your analysis of information |
| `idea` | Hypotheses, project concepts |
| `connection` | Links between concepts |
| `priority` | What to work on and why |
| `question` | Open threads to explore |
| `insight` | Aha moments, realizations |
| `decision` | Choices made and rationale |
| `project` | Active project status |
| `source` | Ingested external content |

## Relationship types

| Type | Meaning |
|------|---------|
| `supports` | Provides evidence for the target |
| `contradicts` | Conflicts with the target |
| `extends` | Builds on the target |
| `inspires` | Sparked the target idea |
| `blocks` | Blocker for the target |
| `requires` | Depends on the target |
| `derived_from` | Synthesized from the target source |
| `related_to` | Auto-discovered semantic/temporal neighbor |
| `superseded_by` | Replaced by the target (dream cycle) |

## Configuration & environment variables

| Variable | Purpose | Example |
|----------|---------|---------|
| `SECOND_BRAIN_PROFILE` | Select model backend profile from `config/backends.toml` | `laptop` (default), `mini` |
| `EXPRESS_EMAIL_TO` | Recipient for Express Gmail push | `<your-email@example.com>` |
| `EXPRESS_EMAIL_FROM` | Sender address for Express Gmail push | `<your-sender@gmail.com>` |
| `GMAIL_APP_PASSWORD` | Gmail app password for Express push | `<your-app-password>` |
| AWS profile (`default`) | SSO credentials for Bedrock (embeddings + LLM) | Configured via `aws sso login` |

> [!NOTE]
> Email variables are set on the LaunchAgent that runs `express_push.py`. Until set, the push composes but skips sending (benign exit 0).

## Related

- [Database schema](database-schema.md) — every table, column, and index
- [Glossary](glossary.md) — definitions of project terms
- [Using Second Brain](using-second-brain.md) — day-to-day tasks and examples
- [Operations](operations.md) — scheduled jobs, backup, database
- [Overview](overview.md) — system architecture at a glance
