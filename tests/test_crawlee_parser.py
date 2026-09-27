"""Hermetic tests for the Crawlee knowledge-store parser (src.parsers.crawlee).

Synthetic index/sources in tmp_path only — no live store, no DB, no Bedrock.

Guards the 2026-06..09 duplicate-ingestion bug: index rows whose "URL" parsed
to "" matched the first source file by substring and were re-ingested nightly
because `already_processed` (keyed by URL) can never skip an empty URL.
"""

import pytest

from src.parsers import crawlee

HEADER = "| Title | Source | Type | Summary | Date |\n|-------|--------|------|---------|------|\n"

URL_A = "https://example.org/a"
URL_B = "https://www.youtube.com/watch?v=bbbbbbbbbbb"
URL_E = "https://example.org/e"
URL_ORPHAN = "https://example.org/not-in-index"


def _link(url):
    return f"[{url}]({url})"


def _row(title, source_cell, type_="web-page", date="2026-01-01"):
    return f"| {title} | {source_cell} | {type_} | [summary](summaries/x.summary.md) | {date} |\n"


def _source_file(url, title="Doc", body="Body text."):
    return f"# {title}\n\nSource: {url}\nType: web-page\nDate: 2026-01-01\n\n---\n\n{body}\n"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A synthetic Crawlee tree; the parser's module paths point at it."""
    knowledge = tmp_path / "knowledge"
    sources = knowledge / "sources"
    output = tmp_path / "output"
    sources.mkdir(parents=True)
    output.mkdir()
    monkeypatch.setattr(crawlee, "CRAWLEE_DIR", str(tmp_path))
    monkeypatch.setattr(crawlee, "KNOWLEDGE_DIR", str(knowledge))
    monkeypatch.setattr(crawlee, "INDEX_PATH", str(knowledge / "index.md"))
    monkeypatch.setattr(crawlee, "OUTPUT_DIR", str(output))
    return {"root": tmp_path, "index": knowledge / "index.md", "sources": sources, "output": output}


@pytest.fixture
def populated(store):
    """Index with the five row shapes plus source files (incl. orphan and empty Source)."""
    rows = [
        _row("Plain title", _link(URL_A)),                                          # (a) normal
        _row("Kiro | Docs | Getting started", _link(URL_B), "youtube-transcript",    # (b) `|` in title
             "2026-01-02"),
        _row("Nothing here", "", date="2026-01-03"),                                 # (c) empty source cell
        _row("Plain title again", _link(URL_A), date="2026-01-04"),                  # (d) duplicate URL
        _row("Bare", URL_E, date="2026-01-05"),                                      # (e) bare URL
    ]
    store["index"].write_text(HEADER + "".join(rows))
    (store["sources"] / "a.md").write_text(_source_file(URL_A, "A"))
    (store["sources"] / "b.md").write_text(_source_file(URL_B, "B", "Transcript."))
    (store["sources"] / "orphan.md").write_text(_source_file(URL_ORPHAN, "Orphan"))
    (store["sources"] / "empty-source.md").write_text(_source_file("", "Empty"))
    # no file for URL_E on purpose
    return store


# --- parse_index --------------------------------------------------------------

def test_parse_index_unique_http_entries(populated):
    entries = crawlee.parse_index()
    assert [e["url"] for e in entries] == [URL_A, URL_B, URL_E]
    assert all(crawlee.is_http_url(e["url"]) for e in entries)
    assert crawlee.last_parse_stats == {"rows": 5, "entries": 3, "skipped_no_url": 1, "duplicate_urls": 1}


def test_parse_index_title_with_pipe_kept_intact(populated):
    by_url = {e["url"]: e for e in crawlee.parse_index()}
    assert by_url[URL_B]["title"] == "Kiro | Docs | Getting started"
    assert by_url[URL_B]["type"] == "youtube-transcript"
    assert by_url[URL_B]["date"] == "2026-01-02"


def test_parse_index_normal_and_bare_rows(populated):
    by_url = {e["url"]: e for e in crawlee.parse_index()}
    assert by_url[URL_A] == {"title": "Plain title", "url": URL_A, "type": "web-page", "date": "2026-01-01"}
    assert by_url[URL_E] == {"title": "Bare", "url": URL_E, "type": "web-page", "date": "2026-01-05"}


def test_parse_index_duplicate_first_row_wins(populated):
    by_url = {e["url"]: e for e in crawlee.parse_index()}
    assert by_url[URL_A]["title"] == "Plain title"
    assert by_url[URL_A]["date"] == "2026-01-01"


def test_parse_index_empty_source_cell_skipped(populated):
    entries = crawlee.parse_index()
    assert all(e["url"] != "" for e in entries)
    assert not any(e["title"] == "Nothing here" for e in entries)
    assert crawlee.last_parse_stats["skipped_no_url"] == 1


def test_parse_index_missing_file_returns_empty(store):
    assert crawlee.parse_index() == []
    assert crawlee.last_parse_stats["entries"] == 0


def test_parse_index_shifted_row_with_empty_cell_and_later_link(store):
    """The real bug shape: a title with a trailing ` |` empties the 2nd cell; the
    link sits in the 4th. Old parser: url == "". New parser: recovers the link."""
    store["index"].write_text(HEADER + f"| Show | | Episode 3 | {_link(URL_A)} | web-page | [summary](s.md) | 2026-02-02 |\n")
    entries = crawlee.parse_index()
    assert len(entries) == 1
    assert entries[0]["url"] == URL_A
    assert entries[0]["title"] == "Show | | Episode 3"
    assert entries[0]["type"] == "web-page"
    assert entries[0]["date"] == "2026-02-02"


def test_parse_index_non_http_link_is_not_a_source(store):
    store["index"].write_text(HEADER + _row("Relative", "[summary](summaries/x.md)") + _row("Ftp", "ftp://example.org/f"))
    assert crawlee.parse_index() == []
    assert crawlee.last_parse_stats["skipped_no_url"] == 2


# --- source-file resolution ---------------------------------------------------

def test_build_source_map_exact_source_header(populated):
    source_map = crawlee.build_source_map()
    assert set(source_map) == {URL_A, URL_B, URL_ORPHAN}
    assert source_map[URL_A].endswith("a.md")
    assert "" not in source_map
    assert crawlee.last_parse_stats["source_files"] == 4
    assert crawlee.last_parse_stats["source_files_no_url"] == 1


def test_build_source_map_reads_header_only(populated):
    """A URL appearing in the body must not resolve a file — only the Source: line does."""
    (populated["sources"] / "mentions.md").write_text(_source_file("https://example.org/real", "M", f"See {URL_E} for more."))
    source_map = crawlee.build_source_map()
    assert URL_E not in source_map
    assert source_map["https://example.org/real"].endswith("mentions.md")


def test_build_source_map_duplicate_source_first_sorted_wins(populated):
    (populated["sources"] / "a-copy.md").write_text(_source_file(URL_A, "A copy"))
    assert crawlee.build_source_map()[URL_A].endswith("a-copy.md")  # "a-copy.md" < "a.md"


# --- parse_all ----------------------------------------------------------------

def test_parse_all_yields_only_entries_with_source_file(populated):
    items = list(crawlee.parse_all())
    assert [(u, t) for u, t, _ in items] == [(URL_A, "article"), (URL_B, "youtube"), (URL_ORPHAN, "article")]
    contents = {u: c for u, _, c in items}
    assert contents[URL_A].startswith("# A\n\nSource: https://example.org/a\n")
    assert "Transcript." in contents[URL_B]
    assert crawlee.last_parse_stats["unresolved"] == 1  # URL_E has no file
    assert crawlee.last_parse_stats["unindexed_output"] == 0


def test_parse_all_never_yields_empty_or_non_http_url(populated):
    for url, _, _ in crawlee.parse_all():
        assert crawlee.is_http_url(url)


def test_parse_all_honours_already_processed(populated):
    items = list(crawlee.parse_all(already_processed={URL_A}))
    assert [u for u, _, _ in items] == [URL_B, URL_ORPHAN]
    assert list(crawlee.parse_all(already_processed={URL_A, URL_B, URL_ORPHAN})) == []


def test_parse_all_regression_empty_url_rows_do_not_duplicate_valid_source(store):
    """Old bug: 42 rows with an empty source cell parsed to url "" and every one
    of them matched the first source file by substring ('' in text)."""
    rows = "".join(_row(f"Row {i}", "", date="2026-06-02") for i in range(42))
    store["index"].write_text(HEADER + rows)
    (store["sources"] / "first.md").write_text(_source_file("https://example.org/first", "First"))
    (store["sources"] / "empty-source.md").write_text(_source_file("", "Empty"))

    assert crawlee.parse_index() == []
    assert crawlee.last_parse_stats["skipped_no_url"] == 42
    assert [(url, kind) for url, kind, _ in crawlee.parse_all()] == [("https://example.org/first", "article")]
    assert len(list(crawlee.parse_all(already_processed={""}))) == 1
    assert list(crawlee.parse_all(already_processed={"https://example.org/first"})) == []


def test_parse_all_empty_source_header_never_matches(store):
    """A source file with `Source:` (empty) must not be picked up by anything."""
    store["index"].write_text(HEADER + _row("Only", "") + _row("Real", _link(URL_A)))
    (store["sources"] / "empty-source.md").write_text(_source_file("", "Empty"))
    assert list(crawlee.parse_all()) == []
    assert crawlee.last_parse_stats["unresolved"] == 1


# --- unindexed output/ files --------------------------------------------------

def test_find_unindexed_output_files_ignores_indexed_and_bad_urls(populated):
    out = populated["output"]
    (out / "indexed.md").write_text(f"# Indexed\n\nSource: {URL_A}\n\n---\n\nbody\n")
    (out / "new.md").write_text("# New page\n\nSource: https://example.org/new\n\n---\n\nnew body\n")
    (out / "empty.md").write_text("# Empty\n\nSource:\n\n---\n\nbody\n")
    (out / "nourl.md").write_text("# No source\n\n---\n\nbody\n")
    (out / "notes.txt").write_text("Source: https://example.org/txt\n")

    indexed_urls = {e["url"] for e in crawlee.parse_index()}
    found = crawlee.find_unindexed_output_files(indexed_urls)
    assert [(f["url"], f["title"], f["filename"]) for f in found] == [
        ("https://example.org/new", "New page", "new.md")]


def test_parse_all_wraps_unindexed_output_file(populated):
    (populated["output"] / "new.md").write_text("# New page\n\nSource: https://example.org/new\n\n---\n\nnew body\n")
    items = list(crawlee.parse_all(already_processed={URL_A, URL_B, URL_ORPHAN}))
    assert [(u, t) for u, t, _ in items] == [("https://example.org/new", "article")]
    content = items[0][2]
    assert content.startswith("# New page\n\nSource: https://example.org/new\nType: web-page\nDate: ")
    assert content.rstrip().endswith("---\n\nnew body")
    assert crawlee.last_parse_stats["unindexed_output"] == 1


# --- is_http_url --------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", None, "https://", "http://", "ftp://x", "example.org", "https://a b", 42])
def test_is_http_url_rejects(bad):
    assert crawlee.is_http_url(bad) is False


@pytest.mark.parametrize("good", ["https://example.org", "http://x.y/z?q=1|2", URL_B])
def test_is_http_url_accepts(good):
    assert crawlee.is_http_url(good) is True


def test_unindexed_cookie_source_is_captured_once_and_shared_filter_knows_it(store):
    from src.capture.filters import load_indexed_urls
    from src.capture.crawlee import capture
    content = _source_file(URL_ORPHAN, "Cookie page", "Saved internal page.")
    (store["sources"] / "cookie.md").write_text(content)
    (store["output"] / "duplicate.md").write_text(content)
    assert list(crawlee.parse_all()) == [(URL_ORPHAN, "article", content)]
    assert crawlee.last_parse_stats["unindexed_sources"] == 1
    assert crawlee.last_parse_stats["unindexed_output"] == 0
    assert load_indexed_urls(store["index"], sources_dir=store["sources"]) == {URL_ORPHAN}
    assert list(crawlee.parse_all(already_processed={URL_ORPHAN})) == []
    result = capture(dry_run=True)
    assert result["article"]["planned"] == 1
    assert result["source_files"]["unindexed_sources"] == 1
