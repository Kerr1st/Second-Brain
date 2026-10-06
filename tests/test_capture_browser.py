"""Hermetic tests for src.capture.browser: synthetic Chrome History DB, no live DB, no Crawlee."""
import os
import sqlite3
from datetime import date, datetime, time, timedelta

import pytest

from src.capture import browser as b


# --------------------------------------------------------------------------- #
# Fixtures: build a minimal Chrome History database                            #
# --------------------------------------------------------------------------- #
def _local(day: date, hh: int, mm: int = 0) -> datetime:
    tz = datetime.now().astimezone().tzinfo
    return datetime.combine(day, time(hh, mm), tzinfo=tz)


@pytest.fixture
def history_db(tmp_path):
    path = tmp_path / "History"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER,
                           typed_count INTEGER, last_visit_time INTEGER, hidden INTEGER);
        CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, from_visit INTEGER,
                             transition INTEGER, visit_duration INTEGER);
    """)
    day = date(2026, 9, 15)
    rows = [
        # url, title, local time, dwell secs, transition (core)
        ("https://example.com/article?utm_source=x#top", "Deep article", _local(day, 9), 300, 0),
        ("https://example.com/article", "Deep article", _local(day, 9, 30), 120, 0),      # revisit, collapses
        ("https://example.com/glance", "Glanced", _local(day, 10), 3, 0),                  # below attention
        ("https://example.com/typed", "Typed in", _local(day, 10, 5), 2, 1),               # deliberate, kept
        ("https://www.google.com/search?q=chrome+history+schema&ved=abc", "search", _local(day, 10, 10), 5, 0),
        ("https://chatgpt.com/c/123", "ChatGPT", _local(day, 11), 400, 0),
        ("https://app.slack.com/client/T1/C1", "Slack", _local(day, 12), 3000, 0),         # noise
        ("https://midway-auth.amazon.com/SSO/redirect", "Midway", _local(day, 12, 1), 1, 0),
        ("https://w.amazon.com/bin/view/Team/Page", "Wiki page", _local(day, 13), 200, 0),  # internal
        ("https://amazon.sharepoint.com/:w:/r/sites/X/Doc.aspx?sourcedoc=1", "Plan.docx", _local(day, 14), 900, 0),  # app
        ("https://oidc.us-east-1.amazonaws.com/authorize", "Authorize", _local(day, 14, 5), 500, 0),  # auth path
        ("https://example.org/background-tab", "Left open", _local(day, 15), 20000, 0),    # capped dwell
        ("https://example.com/next-day", "Tomorrow", _local(day + timedelta(days=1), 9), 300, 0),
    ]
    for i, (url, title, when, dwell, tr) in enumerate(rows, start=1):
        conn.execute("INSERT INTO urls VALUES (?,?,?,?,?,?,?)", (i, url, title, 1, 0, 0, 0))
        conn.execute("INSERT INTO visits VALUES (?,?,?,?,?,?)",
                     (i, i, b.dt_to_chrome_time(when), 0, tr, int(dwell * 1_000_000)))
    conn.commit()
    conn.close()
    return str(path)


class TestHelpers:
    def test_chrome_time_roundtrip(self):
        dt = datetime(2026, 9, 15, 12, 0, tzinfo=datetime.now().astimezone().tzinfo)
        assert b.chrome_time_to_dt(b.dt_to_chrome_time(dt)) == dt

    def test_classify(self):
        assert b.classify("https://app.slack.com/client/x") == "skip"
        assert b.classify("https://midway-auth.amazon.com/x") == "skip"
        assert b.classify("https://oidc.us-east-1.amazonaws.com/authorize") == "skip"
        assert b.classify("https://flex.plusone.com/Home") == "skip"
        assert b.classify("https://www.google.com/search?q=x") == "search"
        assert b.classify("https://chatgpt.com/c/1") == "chat"
        assert b.classify("https://aws-crm.lightning.force.com/lightning/r/Account/1/view") == "app"
        assert b.classify("https://amazon.sharepoint.com/sites/x") == "app"
        assert b.classify("https://w.amazon.com/bin/view/x") == "internal"
        assert b.classify("https://atoz.amazon.work/learn") == "internal"
        assert b.classify("https://example.com/post") == "public"

    def test_canonical_url_drops_tracking_and_fragment(self):
        assert b.canonical_url("https://e.com/a?utm_source=x&id=7&ved=1#frag") == "https://e.com/a?id=7"
        assert b.canonical_url("https://e.com/a") == "https://e.com/a"

    def test_credential_bearing_parameters_never_reach_storage(self):
        url = "https://driver.example.com/connections?partnerToken=AMZS123&access_token=abc&code=xyz&page=2"
        assert b.canonical_url(url) == "https://driver.example.com/connections?page=2"
        assert b.canonical_url("https://e.com/x?session_id=1&sig=2&api_key=3&q=ok") == "https://e.com/x?q=ok"

    def test_extension_account_and_auth_walled_pages(self):
        assert b.classify("chrome-extension://abc/ask.html") == "skip"
        assert b.classify("https://www.levels.fyi/settings/account") == "skip"
        assert b.classify("https://github.com/Org/Repo/settings/rules/new") == "skip"
        assert b.classify("https://activate.amazon-corp.com/success") == "internal"
        assert b.classify("https://www.linkedin.com/search/results/content/?keywords=x") == "app"
        assert b.classify("https://www.allthingsdistributed.com/2026/09/post.html") == "public"

    def test_completed_days(self):
        assert b.completed_days(2, today=date(2026, 9, 16)) == [date(2026, 9, 14), date(2026, 9, 15)]


class TestSummarizeAndRender:
    def test_summary_filters_collapses_and_caps(self, history_db):
        visits = b.load_visits(history_db, date(2026, 9, 15))
        assert len(visits) == 12                                  # next-day visit excluded
        s = b.summarize_day(visits)
        urls = {p["url"]: p for p in s["pages"]}
        assert "https://example.com/article" in urls and urls["https://example.com/article"]["dwell"] == 420
        assert urls["https://example.com/article"]["visits"] == 2  # utm/fragment variant collapsed
        assert "https://example.com/glance" not in urls           # below attention threshold
        assert "https://example.com/typed" in urls                 # deliberate navigation kept
        assert not any("slack.com" in u or "midway" in u or "authorize" in u for u in urls)
        assert urls["https://example.org/background-tab"]["dwell"] == b.MAX_VISIT_DWELL_SECS
        assert s["searches"] == [(visits[4]["time"], "chrome history schema")]
        assert s["chats"] == {"chatgpt.com": 1}
        assert {p["kind"] for p in s["pages"]} == {"public", "internal", "app"}

    def test_render_day(self, history_db):
        day = date(2026, 9, 15)
        s = b.summarize_day(b.load_visits(history_db, day))
        url, md = b.render_day(day, s)
        assert url == "browser://history/2026-09-15"
        head, _, body = md.partition("\n---\n")
        assert head.startswith("# Browsing: 2026-09-15\n") and "Type: browser-history" in head
        assert "Pages: 5" in head and "Searches: 1" in head
        assert "## Searches\n- 10:10 chrome history schema" in body
        assert "chatgpt.com: 1 page loads" in body
        assert "### example.org — 20m" in body                    # capped, not 333m
        assert "Wiki page (internal)" in body and "Plan.docx (app)" in body
        assert "slack" not in body.lower()

    def test_render_empty_day_is_none(self):
        assert b.render_day(date(2026, 9, 13), {"pages": [], "searches": [], "chats": {}, "raw_visits": 0,
                                                 "distinct_pages": 0}) is None

    def test_reading_urls_are_public_and_read(self, history_db):
        s = b.summarize_day(b.load_visits(history_db, date(2026, 9, 15)))
        reads = b.reading_urls(s)
        assert ("Deep article", "https://example.com/article") in reads
        assert ("Left open", "https://example.org/background-tab") in reads
        assert all(b.classify(u) == "public" for _, u in reads)
        assert not any("sharepoint" in u or "w.amazon.com" in u or "/typed" in u for _, u in reads)


class TestCapture:
    def test_capture_dedups_days_and_routes_reading(self, history_db, monkeypatch):
        import src.db, src.ingest
        ingested = {}
        monkeypatch.setattr(src.db, "get_processed_source_urls", lambda st: {"browser://history/2026-09-14"})
        monkeypatch.setattr(src.ingest, "ingest_batch", lambda items, st: ingested.setdefault(st, list(items)) and
                            {"processed": len(items), "skipped": 0, "failed": 0})
        sent = []
        fake_sb = type("SB", (), {"load_indexed_urls": staticmethod(lambda: {"https://example.org/background-tab"}),
                                  "scrape_with_crawlee": staticmethod(lambda urls: sent.extend(urls) or {"processed": len(urls), "failed": 0})})
        monkeypatch.setitem(__import__("sys").modules, "scripts.scrape_bookmarks", fake_sb)
        monkeypatch.setattr(b, "completed_days", lambda n: [date(2026, 9, 14), date(2026, 9, 15)])
        stats = b.capture(days_back=2, history_path=history_db)
        assert stats == {"processed": 1, "skipped": 0, "failed": 0, "reading": 1}
        assert [u for u, _ in ingested[b.SOURCE_TYPE]] == ["browser://history/2026-09-15"]
        assert sent == [("Deep article", "https://example.com/article")]   # indexed URL not re-sent
        assert not os.path.exists(history_db + ".tmp")

    def test_reading_retries_for_already_logged_days(self, history_db, monkeypatch):
        """A failed scrape must be retried while the day is in window, even though its log is ingested."""
        import src.db, src.ingest
        monkeypatch.setattr(src.db, "get_processed_source_urls", lambda st: {"browser://history/2026-09-15"})
        monkeypatch.setattr(src.ingest, "ingest_batch", lambda items, st: {"processed": 0, "skipped": 0, "failed": 0})
        sent = []
        fake_sb = type("SB", (), {"load_indexed_urls": staticmethod(lambda: set()),
                                  "scrape_with_crawlee": staticmethod(lambda urls: sent.extend(urls) or {"processed": len(urls), "failed": 0})})
        monkeypatch.setitem(__import__("sys").modules, "scripts.scrape_bookmarks", fake_sb)
        monkeypatch.setattr(b, "completed_days", lambda n: [date(2026, 9, 15)])
        stats = b.capture(days_back=1, history_path=history_db)
        assert stats["processed"] == 0 and stats["reading"] == 2
        assert {u for _, u in sent} == {"https://example.com/article", "https://example.org/background-tab"}

    def test_missing_history_is_a_failure_not_an_empty_success(self, tmp_path, monkeypatch):
        import src.db
        monkeypatch.setattr(src.db, "get_processed_source_urls", lambda st: set())
        stats = b.capture(history_path=str(tmp_path / "nope"))
        assert stats["processed"] == 0 and stats["failed"] == 1
        from src.capture.common import exit_code
        assert exit_code(stats) == 1
