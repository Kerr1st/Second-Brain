---
title: "Contributing to the User Guide"
type: reference
---

# Contributing to the User Guide

How to add or edit pages in the Second Brain user guide. The [Style Guide](STYLE.md) is the authority on voice, formatting, and structure — this page covers workflow and accuracy rules.

## Goal

Produce clear, concise, accurate documentation for a technical user who self-hosts Second Brain. Every page should answer "what can I do after reading this?" in under five minutes.

## Information architecture (Diátaxis)

Each page belongs to exactly one content type:

| Type | Purpose | When to use |
|------|---------|-------------|
| **Tutorial** | Learning-oriented guided lesson that succeeds end to end | First-time tasks (e.g., [Your first memory](first-memory.md)) |
| **How-to** | Task-oriented steps for a specific goal | Operational tasks (e.g., [Upgrade](upgrade.md)) |
| **Reference** | Terse, complete, factual information | Tool signatures, schema, config (e.g., [Reference](reference.md)) |
| **Explanation** | Understanding-oriented "why" behind the design | Design rationale (e.g., [Dream cycle design](dream-cycle-design.md)) |

Don't mix types on a single page. Link from how-to pages to reference for details; don't re-teach in reference.

## Style

Every page follows [STYLE.md](STYLE.md): second person, present tense, active voice, short sentences, language-tagged code blocks paired with expected output, prerequisites in a `> [!NOTE]` callout, and consistent [terminology](STYLE.md#terminology). Read it before writing.

## Adding a new page

1. **Create the file** in `docs/user-guide/` (flat layout — no subfolders).
2. **Add front-matter** with `title` and `type`:

   ```markdown
   ---
   title: "Your Page Title"
   type: how-to
   ---
   ```

3. **Use the page template** from [STYLE.md § Page template](STYLE.md#page-template).
4. **Add the page to `index.md`** — insert a row in the Navigation table.
5. **Link from related pages** — add it to the `## Related` section of sibling pages that share context.

## Definition of done

Complete the checklist in [STYLE.md § Definition of done](STYLE.md#definition-of-done) before considering a page finished. Key items: front-matter, one-sentence purpose, prerequisites, commands with output, verification step, Related section, no broken links, correct terminology, no secrets.

## Accuracy rules

| Rule | Detail |
|------|--------|
| **Source of truth** | Verify claims against code and `migrations/` — not other docs. |
| **No secrets** | Use placeholders like `<your-app-password>`, `<aws-account-id>`. Never commit real credentials. |
| **Current reality** | Native PostgreSQL on `127.0.0.1:5432` (not Docker). Google Drive + local backups via rclone (not S3). Omit deprecated features (e.g., the HTTP capture API). |
| **Test commands** | Run every command you document and confirm output before committing. |

## Previewing locally

Render Markdown with any GFM-compatible viewer. A quick option:

```bash
# Using grip (GitHub Readme Instant Preview)
pip install grip
grip docs/user-guide/CONTRIBUTING.md
```

Expected output:

```text
 * Serving Flask app 'grip.app'
 * Running on http://localhost:6419
```

Open the URL to see rendered output with callouts and tables.

> [!TIP]
> VS Code's built-in Markdown preview (`⌘⇧V`) also renders GFM callouts with extensions like *Markdown Preview GitHub Styling*.

## Related

- [Style Guide](STYLE.md) — the authority on voice, formatting, and conventions
- [User Guide home](index.md) — navigation map for all pages
- [Glossary](glossary.md) — canonical definitions of project terms
