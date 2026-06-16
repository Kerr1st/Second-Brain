---
title: "Documentation Style Guide"
type: reference
---

# Documentation Style Guide

How to write and maintain the Second Brain user guide. Follow it for every new or edited page so the documentation stays consistent in voice, structure, and terminology. Amend it as conventions evolve — it is a living document.

> [!NOTE]
> This guide is adapted from the Diátaxis framework, the Google developer documentation style guide, and Write the Docs. See [Sources](#sources).

## Writing principles

| # | Principle | What it means |
|---|-----------|---------------|
| 1 | **Audience-first** | Write for a technical user who self-hosts: comfortable with a terminal, PostgreSQL, and JSON, but new to *this* system. Never assume prior knowledge of Second Brain. |
| 2 | **Task-oriented** | Every page answers "what can I do after reading this?" Lead with the task or outcome, not the internals. |
| 3 | **Clear and concise** | Short sentences (≤25 words). One idea per sentence. Cut filler ("basically", "simply", "just", "obviously"). |
| 4 | **Second person, present tense** | Address the reader as "you". "The server starts…", not "The server will start…". |
| 5 | **Active voice** | Make the actor clear. "You configure the profile", not "The profile is configured". |
| 6 | **Show, don't tell** | Pair every instruction with a copy-pasteable command **and** its expected output. The reader should never wonder "did it work?" |
| 7 | **Prerequisites up front** | List what the reader needs (versions, env vars, access) before any procedure, in a `> [!NOTE]` callout. |
| 8 | **No marketing fluff** | No superlatives or hype. State facts and measured outcomes. |
| 9 | **Define jargon on first use** | On first mention of a project term (e.g., *dream cycle*, *Express*), give a one-sentence definition or link to the glossary. |
| 10 | **Consistent terminology** | Use the [Terminology](#terminology) table everywhere. Never alternate synonyms for the same concept. |
| 11 | **Scannable** | Use headings, bullets, tables, and callouts so a reader finds an answer in under 30 seconds. Front-load each paragraph. |
| 12 | **Progressive disclosure** | Start simple; link to depth. How-to pages link to reference; reference does not re-teach. Don't overload one page. |

## Information architecture

The guide lives in `docs/user-guide/` and follows the [Diátaxis](https://diataxis.fr) model — four content types, kept separate:

- **Tutorial** — learning-oriented; a guided lesson that succeeds end to end.
- **How-to** — task-oriented; steps to accomplish a specific goal.
- **Reference** — information-oriented; terse, complete, factual.
- **Explanation** — understanding-oriented; the "why" behind the design.

### Current pages (v1)

| Page | Type | Purpose |
|------|------|---------|
| `index.md` | Landing | Orientation + navigation map |
| `overview.md` | Explanation | What Second Brain is and how the five-stage flow works |
| `getting-started.md` | Tutorial / How-to | Install, configure, connect an agent, verify |
| `using-second-brain.md` | How-to | Day-to-day: write memories, search, link, brief |
| `reference.md` | Reference | MCP tools, CLI commands, memory/relationship types, config |
| `operations.md` | How-to / Reference | Scheduled jobs, backup/restore, monitoring, troubleshooting |
| `STYLE.md` | Reference | This guide |

> [!NOTE]
> v1 is a focused, flat set. As the guide grows (see the documentation roadmap), group pages by Diátaxis type into `tutorials/`, `how-to/`, `reference/`, and `explanation/` subfolders, keeping `index.md` as the hub. Consolidate rather than split until a page mixes content types or exceeds ~1,500 words.

## Page template

Use this skeleton for a new page. Omit sections that don't apply (reference pages skip "Steps" and "Verification").

````markdown
---
title: "<Page Title>"
type: tutorial | how-to | reference | explanation
---

# <Page Title>

<One sentence on what this page helps you do or understand.>

## Prerequisites

> [!NOTE]
> Before you begin, ensure you have:
> - Requirement 1 (e.g., PostgreSQL 17 running — see [Getting started](getting-started.md))
> - Requirement 2

## <Step or section heading>

Describe what the reader does.

```bash
pg_isready -h 127.0.0.1 -p 5432 -U memory_bank
```

Expected output:

```text
127.0.0.1:5432 - accepting connections
```

## Verification

Confirm the outcome with a command and its expected result.

## Related

- [Related page](reference.md)
````

## Formatting conventions

### Headings
- `#` — page title (one per page, matches front-matter `title`)
- `##` — top-level sections (Prerequisites, Steps, Verification, Related)
- `###` — sub-steps within a section
- Avoid `####`; prefer restructuring or a new page.

### Code blocks
- Always tag the language: ` ```bash `, ` ```sql `, ` ```json `, ` ```text `.
- Use `bash` for commands the reader types; `text` for output.
- Don't prefix commands with `$`.
- Show the command **and** its expected output whenever practical.

### Callouts
Use GitHub-flavored alerts:

```markdown
> [!NOTE]      Supplementary information.
> [!TIP]       Optional way to improve the experience.
> [!WARNING]   Risk of data loss or misconfiguration.
> [!CAUTION]   Irreversible or dangerous action.
```

### Links
- Use relative links between guide pages: `[Operations](operations.md)`.
- Use descriptive link text — never "click here".

### Tables
Use tables for structured reference (tool args, env vars, flags). Keep them ≤5 columns.

### Terminology formatting
| Item | Format |
|------|--------|
| Command, tool, or script name | `code` (e.g., `memory_search`, `scripts/brief.py`) |
| File path | `code` (e.g., `~/second-brain/.backup-key`) |
| Environment variable | `code`, all caps (e.g., `SECOND_BRAIN_PROFILE`) |
| First use of a project term | *italic* with an inline definition |
| Placeholder value | `<angle-brackets>` (e.g., `<your-app-password>`) — never a real secret |

## Terminology

Use these canonical terms consistently.

| Canonical term | Definition | Don't use |
|----------------|------------|-----------|
| Second Brain | The whole system (PostgreSQL store + MCP server + scripts + jobs) | "the app", "the platform" |
| MCP server | The Model Context Protocol server exposing the 9 tools | "the API", "the endpoint" |
| memory | A single stored item (content + embedding + metadata + relationships) | "record", "entry", "note" |
| memory type | One of the 10 classifications (e.g., `insight`, `decision`) | "category", "kind" |
| relationship | A typed, directed edge between two memories | "link", "association" |
| dream cycle | The daily autonomous synthesis pipeline | "dream mode", "night job" |
| Express | The briefing and delivery layer | "Express mode", "the briefer" |
| tool | One of the 9 MCP operations an agent invokes | "function", "endpoint" |
| agent | An AI assistant (Kiro CLI, Claude Code) connected to the MCP server | "client", "the LLM" |
| launchd job | A macOS scheduled task defined by a plist | "cron job", "daemon" |

## Definition of done

Before marking a page complete:

- [ ] Front-matter present (`title`, `type`)
- [ ] One-sentence purpose right after the title
- [ ] Prerequisites listed (or stated as "None")
- [ ] Every command in a language-tagged code block
- [ ] Expected output shown for commands that produce it
- [ ] A verification step the reader can run (tutorials/how-tos)
- [ ] A "Related" or "Next steps" section linking ≥1 page
- [ ] No broken links (relative or external)
- [ ] Terminology matches the table above
- [ ] Jargon defined on first use
- [ ] No real secrets — placeholders only
- [ ] Reflects current reality: native PostgreSQL (not Docker), Google Drive + local backups (not S3), and no deprecated features (e.g., the HTTP capture API)
- [ ] ≤ ~1,500 words for tutorial/how-to pages (split if longer)

## Sources

- [Diátaxis Framework](https://diataxis.fr/start-here/)
- [Google Developer Documentation Style Guide](https://developers.google.com/style/highlights)
- [Write the Docs — Documentation Principles](https://www.writethedocs.org/guide/writing/docs-principles/)
- [Write the Docs — Docs as Code](https://www.writethedocs.org/guide/docs-as-code/)
- [Information Architecture for Docs (Fern)](https://buildwithfern.com/post/information-architecture-best-practices-documentation)
