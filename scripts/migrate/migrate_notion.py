#!/usr/bin/env python3
"""Import Notion exports into the Second Brain.

Recursively reads markdown and CSV files from a Notion export directory,
strips Notion-specific artifacts, filters and formats content, and
ingests via the existing pipeline.

Usage: python scripts/migrate/migrate_notion.py <export_path> [--dry-run] [--limit N]
"""

import argparse
import csv
import logging
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.migrate.migration_utils import (
    format_markdown_header,
    passes_page_filter,
    run_migration,
    MigrationError,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

# Notion appends a 32-char hex ID to filenames: "My Page abc123def456789012345678.md"
NOTION_ID_RE = re.compile(r'\s+[0-9a-f]{32}$')

# Notion inline database references and URL fragments containing block IDs
NOTION_DB_REF_RE = re.compile(r'\[.*?\]\(https://www\.notion\.so/[^)]*[0-9a-f]{32}[^)]*\)')
NOTION_URL_FRAGMENT_RE = re.compile(r'https://www\.notion\.so/\S*[0-9a-f]{32}\S*')

def strip_notion_id(filename):
    """Remove the 32-char hex hash Notion appends to filenames.

    'My Page abcdef01234567890abcdef012345678.md' → 'My Page'
    'Simple.md' → 'Simple'
    """
    # Strip known extensions first, then splitext for others
    for ext in ('.md', '.csv'):
        if filename.endswith(ext):
            name = filename[:-len(ext)]
            if not name:
                return ""
            stripped = NOTION_ID_RE.sub('', name).strip()
            return stripped if stripped else name.strip()
    name, _ = os.path.splitext(filename)
    if not name:
        return ""
    stripped = NOTION_ID_RE.sub('', name).strip()
    return stripped if stripped else name.strip()


def strip_notion_artifacts(content):
    """Remove Notion-specific inline database references and URL fragments."""
    content = NOTION_DB_REF_RE.sub('', content)
    content = NOTION_URL_FRAGMENT_RE.sub('', content)
    return content


def find_title_column(headers):
    """Return 'Name' or 'Title' column (case-insensitive) if present, else first column."""
    for h in headers:
        if h.strip().lower() in ("name", "title"):
            return h
    return headers[0] if headers else None


def _slugify(text):
    """Convert text to a URL-safe slug."""
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def _path_slug(rel_path):
    """Create a unique slug from the relative file path to avoid collisions.

    Uses the full relative path (not just the filename) so that pages with
    the same name in different directories get distinct source_urls.
    E.g., 'Projects/Work/Notes.md' → 'projects-work-notes'
    """
    # Strip extension and Notion hash from the path components
    parts = rel_path.replace(os.sep, '/').split('/')
    cleaned = []
    for part in parts:
        # Strip extension from the last part
        if part == parts[-1]:
            for ext in ('.md', '.csv'):
                if part.endswith(ext):
                    part = part[:-len(ext)]
                    break
            # Strip Notion hash from filename
            part = NOTION_ID_RE.sub('', part).strip()
        cleaned.append(part)
    return _slugify('/'.join(cleaned))


def parse_notion_export(export_path):
    """Yield (source_url, markdown) for each page/row passing filters."""
    for dirpath, _, filenames in os.walk(export_path):
        for filename in sorted(filenames):
            filepath = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(filepath, export_path)
            parent_path = os.path.dirname(rel_path) or ""

            if filename.endswith('.md'):
                yield from _parse_md_file(filepath, filename, rel_path, parent_path)
            elif filename.endswith('.csv'):
                yield from _parse_csv_file(filepath, filename, rel_path, parent_path)


def _parse_md_file(filepath, filename, rel_path, parent_path):
    """Parse a single Notion markdown file."""
    title = strip_notion_id(filename)
    slug = _path_slug(rel_path)

    try:
        with open(filepath) as f:
            body = f.read()
    except (OSError, UnicodeDecodeError):
        return

    body = strip_notion_artifacts(body)

    if not passes_page_filter(body):
        return

    mtime = os.path.getmtime(filepath)
    date_str = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d")
    mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

    source_url = f"notion://{slug}"

    extra_fields = {}
    if parent_path:
        extra_fields["Parent-Page-Path"] = parent_path

    header = format_markdown_header(title, "notion_page", source_url, date_str,
                                    extra_fields=extra_fields)
    markdown = header + body

    yield source_url, markdown


def _parse_csv_file(filepath, filename, rel_path, parent_path):
    """Parse a Notion CSV file, yielding one item per row."""
    db_name = strip_notion_id(filename)
    slug = _path_slug(rel_path)

    try:
        with open(filepath, newline='') as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return
            title_col = find_title_column(list(reader.fieldnames))
            other_cols = [c for c in reader.fieldnames if c != title_col]

            mtime = os.path.getmtime(filepath)
            date_str = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d")
            mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

            for i, row in enumerate(reader):
                row_title = (row.get(title_col) or "").strip() or f"{db_name} row {i}"
                body_parts = []
                for col in other_cols:
                    val = (row.get(col) or "").strip()
                    if val:
                        body_parts.append(f"**{col}:** {val}")
                body = "\n\n".join(body_parts)

                if not passes_page_filter(body):
                    continue

                source_url = f"notion://{slug}#{i}"

                extra_fields = {}
                if parent_path:
                    extra_fields["Parent-Page-Path"] = parent_path

                header = format_markdown_header(
                    row_title, "notion_page", source_url, date_str,
                    extra_fields=extra_fields,
                )
                markdown = header + body

                yield source_url, markdown
    except (OSError, csv.Error):
        return


def main():
    parser = argparse.ArgumentParser(description="Import Notion pages into Second Brain")
    parser.add_argument("export_path", help="Path to Notion export directory")
    parser.add_argument("--dry-run", action="store_true", help="Parse and filter without writing to DB")
    parser.add_argument("--limit", type=int, default=None, help="Max pages to process")
    args = parser.parse_args()

    try:
        run_migration(
            name="Notion",
            export_path=args.export_path,
            parse_fn=parse_notion_export,
            source_type="notion_page",
            expected_file=None,
            dry_run=args.dry_run,
            limit=args.limit,
        )
    except MigrationError as e:
        logging.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
