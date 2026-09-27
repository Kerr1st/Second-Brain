"""Crawlee parser — reads from Crawlee's knowledge store.

Reads knowledge/index.md to discover content, loads markdown files
from knowledge/sources/, and yields them for the ingestion pipeline.

Also handles web pages in output/ that haven't been indexed yet
(backward compatibility until Crawlee's TODO is completed).

Index format (one markdown table row per document)::

    | Title | Source | Type | Summary | Date |
    | My title | [https://x](https://x) | web-page | [summary](summaries/x.md) | 2026-03-11 |

Titles are written into the table verbatim, so a title containing ``|``
adds cells and shifts everything after it. Rows are therefore parsed by
*locating the source cell* — the first markdown link whose target is an
http(s) URL (falling back to a cell that is a bare http(s) URL) — rather
than by fixed cell position: the title is everything before that cell,
the type is the cell after it, the date is the last cell.

Source files (``knowledge/sources/*.md``) start with a metadata header::

    # Title

    Source: https://x
    Type: web-page
    Date: 2026-03-10

    ---

An index entry is resolved to its file by an exact match of the header's
``Source:`` value, via a URL→file map built once per parse.

``last_parse_stats`` (module-level dict) holds the counters of the most
recent :func:`parse_index` / :func:`parse_all` call — see the docstrings.

See docs/CAPTURE-COMPONENTS.md for file format details.
"""

import itertools
import logging
import os
import re
from datetime import datetime

logger = logging.getLogger(__name__)

CRAWLEE_DIR = os.path.expanduser("~/Work/Tools/Crawlee")
KNOWLEDGE_DIR = os.path.join(CRAWLEE_DIR, "knowledge")
OUTPUT_DIR = os.path.join(CRAWLEE_DIR, "output")
INDEX_PATH = os.path.join(KNOWLEDGE_DIR, "index.md")

# Lines of a source file's metadata header to inspect (the header ends at `---`).
HEADER_MAX_LINES = 40

# A markdown link whose target is an http(s) URL, anywhere in a row.
_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
# A table cell that is exactly a bare http(s) URL (fallback when there is no link).
_BARE_URL_CELL_RE = re.compile(r"(?<=\|)\s*(https?://[^|\s]+)\s*(?=\|)")
_HTTP_URL_RE = re.compile(r"^https?://\S+$")
_TABLE_RULE_RE = re.compile(r"^\|[-\s|:]+\|$")

# Counters from the most recent parse_index() / parse_all() call.
#   rows              data rows examined in index.md
#   entries           unique http(s) entries returned by parse_index()
#   skipped_no_url    rows without an http(s) URL (skipped)
#   duplicate_urls    rows collapsed onto an earlier row with the same URL
# parse_all() adds:
#   source_files      files scanned under knowledge/sources/
#   source_files_no_url  files whose header has no usable http(s) `Source:`
#   unresolved        index entries with no source file matching their URL
#   unindexed_sources  source documents whose URL is absent from the index
#   unindexed_output  output/ files whose URL is absent from index and sources
last_parse_stats = {}


def is_http_url(url):
    """True for a non-empty http(s) URL without whitespace; False for '' or None."""
    return isinstance(url, str) and _HTTP_URL_RE.match(url) is not None


def _parse_index_row(line):
    """Parse one table row into an entry dict, or None when it has no http(s) URL."""
    if not line.endswith("|"):
        line = line + " |"

    m = _LINK_RE.search(line) or _BARE_URL_CELL_RE.search(line)
    if not m:
        return None
    url = m.group(1)
    if not is_http_url(url):
        return None

    # Title: everything before the cell holding the URL (a `|` in the title
    # merely adds cells, so the raw text is kept, including its pipes).
    before = line[: m.start()].rpartition("|")[0].strip()
    title = before[1:].strip() if before.startswith("|") else before

    # Cells after the URL cell: type first, date last (summary in between).
    after = [c.strip() for c in line[m.end():].split("|")][1:-1]
    source_type = after[0] if after else ""
    date = after[-1] if len(after) >= 2 else ""

    return {"title": title, "url": url, "type": source_type, "date": date}


def parse_index(index_path=None):
    """Parse knowledge/index.md and return the list of unique entries.

    Each entry is ``{"title", "url", "type", "date"}``. Rows without an
    http(s) URL are skipped; a URL that appears in several rows is returned
    once (the first row wins). Counters go to ``last_parse_stats``.
    """
    stats = {"rows": 0, "entries": 0, "skipped_no_url": 0, "duplicate_urls": 0}
    last_parse_stats.clear()
    last_parse_stats.update(stats)

    index_path = index_path or INDEX_PATH
    if not os.path.exists(index_path):
        return []

    entries = []
    seen = set()
    with open(index_path) as f:
        for line in f:
            line = line.strip()
            if not line.startswith("|") or line.startswith("| Title") or _TABLE_RULE_RE.match(line):
                continue
            stats["rows"] += 1

            entry = _parse_index_row(line)
            if entry is None:
                stats["skipped_no_url"] += 1
                logger.debug("index row without http(s) URL skipped: %.80s", line)
                continue
            if entry["url"] in seen:
                stats["duplicate_urls"] += 1
                continue
            seen.add(entry["url"])
            entries.append(entry)

    stats["entries"] = len(entries)
    last_parse_stats.update(stats)
    return entries


def _sources_dir():
    return os.path.join(KNOWLEDGE_DIR, "sources")


def read_source_file(filename):
    """Read a source file from knowledge/sources/. Returns content or None."""
    path = os.path.join(_sources_dir(), filename)
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


def _read_header(filepath, max_lines=HEADER_MAX_LINES):
    """Read a file's metadata header (up to `---` or max_lines).

    Returns a dict of lower-cased keys → stripped values, plus ``"title"``
    from the ``# `` line. Never reads the body.
    """
    meta = {}
    with open(filepath, errors="replace") as f:
        for line in itertools.islice(f, max_lines):
            stripped = line.strip()
            if stripped == "---":
                break
            if line.startswith("# "):
                meta.setdefault("title", line[2:].strip())
            elif ":" in line and not line.startswith("#"):
                key, _, value = line.partition(":")
                meta.setdefault(key.strip().lower(), value.strip())
    return meta


def build_source_map(sources_dir=None):
    """Map ``Source:`` URL → file path for every file in knowledge/sources/.

    Only non-empty http(s) values are mapped, so an empty header can never
    match anything. When two files claim the same URL the first in sorted
    filename order wins. Counters ``source_files`` / ``source_files_no_url``
    are recorded in ``last_parse_stats``.
    """
    sources_dir = sources_dir or _sources_dir()
    source_map = {}
    scanned = no_url = 0
    if os.path.isdir(sources_dir):
        for filename in sorted(os.listdir(sources_dir)):
            if not filename.endswith(".md"):
                continue
            filepath = os.path.join(sources_dir, filename)
            if not os.path.isfile(filepath):
                continue
            scanned += 1
            url = _read_header(filepath).get("source", "")
            if not is_http_url(url):
                no_url += 1
                logger.debug("source file without http(s) Source header: %s", filename)
                continue
            if url in source_map:
                logger.debug("duplicate Source %s in %s (keeping %s)", url, filename, source_map[url])
                continue
            source_map[url] = filepath
    last_parse_stats["source_files"] = scanned
    last_parse_stats["source_files_no_url"] = no_url
    return source_map


def find_unindexed_output_files(indexed_urls):
    """Find web pages in output/ not yet in the knowledge index.

    This handles backward compatibility until Crawlee's TODO is completed
    and all content types write to the knowledge store. Files whose header
    has no http(s) ``Source:`` are ignored.
    """
    if not os.path.isdir(OUTPUT_DIR):
        return []

    unindexed = []
    for filename in sorted(os.listdir(OUTPUT_DIR)):
        if not filename.endswith(".md"):
            continue
        filepath = os.path.join(OUTPUT_DIR, filename)
        if not os.path.isfile(filepath):
            continue

        header = _read_header(filepath)
        url = header.get("source", "")
        if not is_http_url(url) or url in indexed_urls:
            continue

        unindexed.append({
            "title": header.get("title") or filename,
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
        (source_url, source_type, markdown_content) tuples. ``source_url`` is
        always a non-empty http(s) URL; entries without a matching source
        file are dropped (counted as ``unresolved`` in ``last_parse_stats``).
    """
    already_processed = already_processed or set()

    # 1. Indexed content from knowledge/sources/, resolved by exact Source: match
    entries = parse_index()
    source_map = build_source_map()
    unresolved = 0
    for entry in entries:
        url = entry["url"]
        if not is_http_url(url) or url in already_processed:
            continue

        filepath = source_map.get(url)
        if filepath is None:
            unresolved += 1
            logger.debug("no source file for indexed URL %s", url)
            continue

        with open(filepath) as f:
            content = f.read()
        if not content.strip():
            continue

        source_type = "youtube" if entry["type"] == "youtube-transcript" else "article"
        yield url, source_type, content
    last_parse_stats["unresolved"] = unresolved

    # Cookie capture writes source documents before an optional index entry.
    # The exact Source header is the identity; never match body URL mentions.
    indexed_urls = {e["url"] for e in entries}
    unindexed_sources = set(source_map) - indexed_urls
    last_parse_stats["unindexed_sources"] = len(unindexed_sources)
    for url in sorted(unindexed_sources):
        if url in already_processed:
            continue
        filepath = source_map[url]
        with open(filepath) as handle:
            content = handle.read()
        if content.strip():
            source_type = "youtube" if _read_header(filepath).get("type") == "youtube-transcript" else "article"
            yield url, source_type, content

    # Legacy output documents cannot duplicate a knowledge source identity.
    unindexed = find_unindexed_output_files(indexed_urls | set(source_map))
    last_parse_stats["unindexed_output"] = len(unindexed)
    for item in unindexed:
        if not is_http_url(item["url"]) or item["url"] in already_processed:
            continue

        with open(item["filepath"]) as f:
            raw_content = f.read()

        # Wrap in knowledge-store format with metadata header
        date = datetime.now().strftime("%Y-%m-%d")
        content = (
            f"# {item['title']}\n\nSource: {item['url']}\nType: web-page\nDate: {date}\n\n---\n\n"
            f"{raw_content.split('---', 1)[-1].strip()}"
        )

        yield item["url"], "article", content


if __name__ == "__main__":
    """Quick test: show what would be ingested (counts and URLs only)."""
    indexed = parse_index()
    print(f"Indexed entries: {len(indexed)}  stats: {last_parse_stats}")
    for e in indexed[:10]:
        print(f"  [{e['type']}] {e['title'][:60]} → {e['url'][:60]}")
    if len(indexed) > 10:
        print(f"  ... and {len(indexed) - 10} more")

    source_map = build_source_map()
    resolved = sum(1 for e in indexed if e["url"] in source_map)
    print(f"\nSource files: {len(source_map)} mapped; {resolved}/{len(indexed)} entries resolve")

    indexed_urls = {e["url"] for e in indexed}
    unindexed = find_unindexed_output_files(indexed_urls)
    print(f"\nUnindexed output/ files: {len(unindexed)}")
    for u in unindexed[:5]:
        print(f"  [{u['type']}] {u['title'][:60]} → {u['url'][:60]}")
    if len(unindexed) > 5:
        print(f"  ... and {len(unindexed) - 5} more")

    print(f"\nTotal available for ingestion: {resolved + len(unindexed)}")
