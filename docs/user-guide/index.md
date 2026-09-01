---
title: "Second Brain User Guide"
type: landing
---

# Second Brain User Guide

Second Brain is a self-hosted persistent-memory and autonomous-synthesis layer for AI agents. It captures knowledge from your conversations, documents, and feeds, retrieves it by meaning, and autonomously surfaces connections and insights — so your AI agents remember across sessions and think with you over time.

## Who this guide is for

You run your own infrastructure and are comfortable with a terminal, PostgreSQL, and Python. You want to understand, install, operate, or extend Second Brain.

## Start here

1. [Overview](overview.md) — understand what the system does and how it works.
2. [Getting started](getting-started.md) — install, configure, and verify.
3. [Connect an AI agent](connect-ai-agent.md) — wire up Kiro CLI or Claude Code.
4. [Your first memory](first-memory.md) — create, search, and relate a memory.

## All pages

### Tutorials — learning by doing

| Page | Description |
|------|-------------|
| [Getting started](getting-started.md) | Install PostgreSQL, pgvector, and local Ollama BGE-M3; start the MCP server; and verify. |
| [Connect an AI agent](connect-ai-agent.md) | Register the MCP server with Kiro CLI or Claude Code. |
| [Your first memory](first-memory.md) | A guided walkthrough: create, search, read, update, and relate a memory. |

### How-to guides — task-oriented

| Page | Description |
|------|-------------|
| [Using Second Brain](using-second-brain.md) | Day-to-day workflows: writing deep memories, searching, linking, briefings. |
| [Operations](operations.md) | Scheduled jobs, backups, SSO refresh, and monitoring. |
| [Upgrade](upgrade.md) | Safely upgrade a running instance. |
| [Troubleshooting](troubleshoot.md) | Diagnose and fix common issues. |

### Reference — look it up

| Page | Description |
|------|-------------|
| [Reference](reference.md) | MCP tools, CLI commands, memory & relationship types, and configuration. |
| [Database schema](database-schema.md) | Every table, column, and index, drawn from the migrations. |
| [Glossary](glossary.md) | Definitions of project-specific terms. |

### Explanation — how and why it works

| Page | Description |
|------|-------------|
| [Overview](overview.md) | What Second Brain is and the five-stage data flow. |
| [Architecture](architecture.md) | How the components fit together. |
| [How memory works](how-memory-works.md) | Embeddings, hybrid search, RRF, and the reranking formula. |
| [Dream cycle design](dream-cycle-design.md) | Why and how the autonomous nightly synthesis works. |
| [Security model](security-model.md) | Threat model, trust boundaries, encryption, and secret handling. |

### Contributing

| Page | Description |
|------|-------------|
| [Style guide](STYLE.md) | Voice, formatting, terminology, and structure conventions. |
| [Contributing](CONTRIBUTING.md) | How to add or edit guide pages. |

### Engineering architecture

| Page | Description |
|------|-------------|
| [Architecture Component Index](../components/index.md) | Canonical ownership, contracts, runtime flows, code entry points, tests, and operational status for every component. |
| [Architecture decisions](../adr/index.md) | Durable decisions governing capture, provenance, segmentation, testing, and Correction Episodes. |
