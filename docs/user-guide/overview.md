---
title: "Overview"
type: explanation
---

# Overview

This page explains what Second Brain is, the problem it solves, and how its five-stage pipeline turns raw conversations into persistent, retrievable, synthesized knowledge.

## The problem: AI agents forget

Every time you start a new session with an AI agent, it begins with a blank slate. Decisions you made last week, research you discussed yesterday, patterns that emerged across projects — all gone. You repeat yourself, re-explain context, and lose compounding value. Second Brain solves this by giving agents a persistent memory they can write to and read from across every session.

## How it works: the five-stage flow

### 1. Capture

Content enters the system from multiple channels: Kiro CLI chats, Kiro IDE chats, Quick Desktop documents and chats, feed events, Slack threads, YouTube transcripts, and web articles. You don't manually file anything — capture happens automatically through scheduled sync jobs and agent interactions.

### 2. Ingest

Captured content is parsed, classified by *memory class* (semantic, episodic, or procedural), assigned a depth score, chunked, embedded using Amazon Bedrock Titan v2 (1024 dimensions), and stored in PostgreSQL with pgvector. The pipeline also auto-discovers typed relationships between the new memory and existing ones.

### 3. Retrieve

When an agent searches, Second Brain runs hybrid retrieval: BM25 keyword search and vector similarity search, fused with Reciprocal Rank Fusion (RRF). Results then pass through a cognitive-science-grounded reranker that factors in retrieval reinforcement (memories accessed more often surface more easily) and temporal context. The result: you find knowledge by meaning, not by where you filed it.

### 4. Synthesize

Once a day, the *dream cycle* runs autonomously. An Explorer agent uses 11 strategies to find promising memory clusters. A Thinker agent drafts candidate insights from those clusters. A four-evaluator consensus panel then votes on each candidate — an insight is accepted only if it receives ≥3 of 4 votes. This produces non-obvious cross-project connections you never explicitly stored.

### 5. Deliver

*Express* surfaces synthesized knowledge back to you. It has four channels: an on-demand `brief` CLI command, a gated Gmail push (chained after the daily dream cycle), the `memory_brief` MCP tool available in-session, and a feedback loop that shapes future delivery to your preferences.

## Mental model

Think of Second Brain as a loop, not a filing cabinet:

> **Capture → Store → Retrieve → Synthesize → Deliver → (you act, generating new captures)**

Knowledge compounds. The dream cycle finds connections you missed. Express tells you what it found. Your responses feed back in. Over time the system becomes a thinking partner, not just a store.

## Key concepts

*Memory*
: A single stored item — text content, a vector embedding, metadata (type, tags, source, confidence), and relationships to other memories. The atomic unit of the system.

*Memory type*
: A classification that describes what a memory represents. The documented types are `research`,
  `synthesis`, `idea`, `connection`, `priority`, `question`, `insight`, `decision`,
  `correction_episode`, `steering_candidate`, `steering_rule`, `project`, and `source`. The database
  intentionally leaves the type column extensible for capture-specific records.

*Relationship*
: A typed, directed edge between two memories (e.g., memory A `supports` memory B). The 9 relationship types are: `supports`, `contradicts`, `extends`, `inspires`, `blocks`, `requires`, `derived_from`, `related_to`, and `superseded_by`.

*Dream cycle*
: The autonomous nightly synthesis pipeline. It runs Explorer → Thinker → Consensus Panel to produce accepted insights from existing memories without human intervention.

*Express*
: The delivery subsystem that surfaces synthesized knowledge to you — via CLI briefing, email push, in-session MCP tool, and feedback.

*MCP server*
: The Model Context Protocol server that exposes 11 tools, including bounded `memory_context` recall and `memory_context_outcome` receipts, so agents can use knowledge and report whether it helped.

## Next steps

- [Getting started](getting-started.md) — install and store your first memory.
- [Using Second Brain](using-second-brain.md) — day-to-day workflows for capture, search, and synthesis.
