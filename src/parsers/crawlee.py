"""Crawlee parser — reads from Crawlee's knowledge store.

Reads knowledge/index.md to discover content, loads markdown files
from knowledge/sources/, and yields them for the ingestion pipeline.

Also handles web pages in output/ that haven't been indexed yet
(backward compatibility until Crawlee's TODO is completed).

See docs/AGENTS.md → Crawlee Integration for file format details.
"""

import os
import re

CRAWLEE_DIR = os.path.expanduser("~/Work/Tools/Crawlee")
KNOWLEDGE_DIR = os.path.join(CRAWLEE_DIR, "knowledge")
OUTPUT_DIR = os.path.join(CRAWLEE_DIR, "output")
INDEX_PATH = os.path.join(KNOWLEDGE_DIR, "index.md")


def parse_index():
    """Parse knowledge/index.md and return list of entries."""
    if not os.path.exists(INDEX_PATH):
        return []

    entries = []
    with open(INDEX_PATH) as f:
        for line in f:
            line = line.strip()
            if not line.startswith("|") or line.startswith("| Title") or re.match(r'^\|[-\s|]+\|$', line):
                continue

            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) < 5:
                continue

            # Extract URL from markdown link
            url_match = re.search(r'\[.*?\]\((.*?)\)', cells[1])
            url = url_match.group(1) if url_match else cells[1]

            entries.append({
                "title": cells[0],
                "url": url,
                "type": cells[2],
                "date": cells[4],
            })

    return entries


def read_source_file(filename):
    """Read a source file from knowledge/sources/. Returns content or None."""
    path = os.path.join(KNOWLEDGE_DIR, "sources", filename)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return f.read()


def parse_metadata_header(content):
    """Extract metadata from a markdown file's header."""
    meta = {}
    for line in content.split("\n"):
        if line.startswith("---"):
            break
        if ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
    return meta


def find_unindexed_output_files(indexed_urls):
    """Find web pages in output/ not yet in the knowledge index.

    This handles backward compatibility until Crawlee's TODO is completed
    and all content types write to the knowledge store.
    """
    if not os.path.isdir(OUTPUT_DIR):
        return []

    unindexed = []
    for filename in os.listdir(OUTPUT_DIR):
        if not filename.endswith(".md"):
            continue

        filepath = os.path.join(OUTPUT_DIR, filename)
        with open(filepath) as f:
            first_lines = f.read(500)

        # Extract source URL from the file
        source_match = re.search(r'^Source:\s*(.+)$', first_lines, re.MULTILINE)
        if not source_match:
            continue

        url = source_match.group(1).strip()
        if url in indexed_urls:
            continue

        # Extract title
        title_match = re.search(r'^# (.+)$', first_lines, re.MULTILINE)
        title = title_match.group(1) if title_match else filename

        unindexed.append({
            "title": title,
            "url": url,
            "type": "web-page",
            "filename": filename,
            "filepath": filepath,
        })

    return unindexed


def parse_all(already_processed=None):
    """Yield all Crawlee content ready for ingestion.

    Args:
        already_processed: set of source URLs already in the database.

    Yields:
        (source_url, source_type, markdown_content) tuples.
    """
    already_processed = already_processed or set()

    # 1. Read indexed content from knowledge/sources/
    for entry in parse_index():
        if entry["url"] in already_processed:
            continue

        # Find the source file — try common filename patterns
        source_files = os.listdir(os.path.join(KNOWLEDGE_DIR, "sources")) if os.path.isdir(os.path.join(KNOWLEDGE_DIR, "sources")) else []
        content = None
        for sf in source_files:
            filepath = os.path.join(KNOWLEDGE_DIR, "sources", sf)
            with open(filepath) as f:
                text = f.read(500)
            if entry["url"] in text:
                with open(filepath) as f:
                    content = f.read()
                break

        if content:
            source_type = "youtube" if entry["type"] == "youtube-transcript" else "article"
            yield entry["url"], source_type, content

    # 2. Handle unindexed output/ files (backward compat)
    indexed_urls = {e["url"] for e in parse_index()}
    for item in find_unindexed_output_files(indexed_urls):
        if item["url"] in already_processed:
            continue

        with open(item["filepath"]) as f:
            raw_content = f.read()

        # Wrap in knowledge-store format with metadata header
        from datetime import datetime
        date = datetime.now().strftime("%Y-%m-%d")
        content = f"# {item['title']}\n\nSource: {item['url']}\nType: web-page\nDate: {date}\n\n---\n\n{raw_content.split('---', 1)[-1].strip()}"

        yield item["url"], "article", content


if __name__ == "__main__":
    """Quick test: show what would be ingested."""
    indexed = parse_index()
    print(f"Indexed entries: {len(indexed)}")
    for e in indexed:
        print(f"  [{e['type']}] {e['title'][:60]}... → {e['url'][:60]}")

    indexed_urls = {e["url"] for e in indexed}
    unindexed = find_unindexed_output_files(indexed_urls)
    print(f"\nUnindexed output/ files: {len(unindexed)}")
    if unindexed:
        for u in unindexed[:5]:
            print(f"  [{u['type']}] {u['title'][:60]}... → {u['url'][:60]}")
        if len(unindexed) > 5:
            print(f"  ... and {len(unindexed) - 5} more")

    print(f"\nTotal available for ingestion: {len(indexed) + len(unindexed)}")
