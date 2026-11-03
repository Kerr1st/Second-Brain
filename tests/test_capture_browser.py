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
        sent, timeouts = [], []
        fake_sb = type("SB", (), {"BATCH_SCRAPE_ENV": {},
                                  "load_indexed_urls": staticmethod(lambda: {"https://example.org/background-tab"}),
                                  "scrape_with_crawlee": staticmethod(lambda urls, **kw: sent.extend(urls) or timeouts.append(kw.get("timeout")) or {"processed": len(urls), "failed": 0})})
        monkeypatch.setitem(__import__("sys").modules, "scripts.scrape_bookmarks", fake_sb)
        monkeypatch.setattr(b, "completed_days", lambda n: [date(2026, 9, 14), date(2026, 9, 15)])
        stats = b.capture(days_back=2, history_path=history_db)
        # reading_failed / reading_too_short are now always reported (advisory, not in "failed").
        assert stats == {"processed": 1, "skipped": 0, "failed": 0, "reading": 1,
                         "reading_failed": 0, "reading_too_short": 0}
        assert [u for u, _ in ingested[b.SOURCE_TYPE]] == ["browser://history/2026-09-15"]
        assert sent == [("Deep article", "https://example.com/article")]   # indexed URL not re-sent
        assert not os.path.exists(history_db + ".tmp")
        assert timeouts and all(t is not None and t < 3600 for t in timeouts)  # bounded below the job deadline

    def test_reading_retries_for_already_logged_days(self, history_db, monkeypatch):
        """A failed scrape must be retried while the day is in window, even though its log is ingested."""
        import src.db, src.ingest
        monkeypatch.setattr(src.db, "get_processed_source_urls", lambda st: {"browser://history/2026-09-15"})
        monkeypatch.setattr(src.ingest, "ingest_batch", lambda items, st: {"processed": 0, "skipped": 0, "failed": 0})
        sent, timeouts = [], []
        fake_sb = type("SB", (), {"BATCH_SCRAPE_ENV": {}, "load_indexed_urls": staticmethod(lambda: set()),
                                  "scrape_with_crawlee": staticmethod(lambda urls, **kw: sent.extend(urls) or timeouts.append(kw.get("timeout")) or {"processed": len(urls), "failed": 0})})
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


#: 2026-10-14: 7 reading URLs sent, 2 queue.acm.org pages 403 "Request blocked"
#: (classed auth), 5 too short, the one batch exited 0.
_ACM = ["https://queue.acm.org/doi/10.1145/3454122.3454124", "https://queue.acm.org/doi/10.1145/3595878"]
_SHORT = ["https://culturecloud.com/x"] + [f"https://apg-library.amazonaws.com/p/{i}" for i in range(4)]
_PAGE_TEXT = "PAGE-BODY-SENTINEL never logged"


def _scrape_stats(**over):
    stats = {"processed": 0, "skipped": 0, "failed": 0, "too_short": 0, "timed_out_batches": 0,
             "deferred": 0, "crawlee_broken": 0, "batches_run": 1, "batches_with_output": 1,
             "failed_batches": 0}
    stats.update(over)
    return stats


class TestReadingExitRule:
    """Per-URL reading outcomes are advisory; only the day log or a broken scrape fails the job."""

    @pytest.fixture
    def run(self, history_db, monkeypatch):
        import sys
        import src.db, src.ingest
        from src.capture.common import exit_code
        calls = []
        env = {"BATCH_SCRAPE_MAX_REQUEST_RETRIES": "1"}   # identity-checked: imported, not duplicated
        # The exit rule is tested on its own: the reading ledger (which would bot-wall the
        # queue.acm.org fixtures) is covered by TestReadingLedgerWiring below.
        monkeypatch.setattr(b, "_select_reading", lambda fresh: fresh)
        monkeypatch.setattr(b, "_record_reading", lambda outcomes: None)

        def go(scrape_stats, outcomes, ingest_failed=0, urls=None):
            monkeypatch.setattr(src.db, "get_processed_source_urls", lambda st: set())
            monkeypatch.setattr(src.ingest, "ingest_batch", lambda items, st: {
                "processed": len(items) - ingest_failed, "skipped": 0, "failed": ingest_failed})
            monkeypatch.setattr(b, "completed_days", lambda n: [date(2026, 9, 15)])
            read = urls if urls is not None else _ACM + _SHORT
            monkeypatch.setattr(b, "reading_urls", lambda s: [("t", u) for u in read])

            def scrape(urls, **kw):
                calls.append((list(urls), kw))
                kw["outcomes"].update(outcomes)      # what scrape_with_crawlee fills in
                return scrape_stats
            fake = type("SB", (), {"BATCH_SCRAPE_ENV": env, "load_indexed_urls": staticmethod(set),
                                   "scrape_with_crawlee": staticmethod(scrape)})
            monkeypatch.setitem(sys.modules, "scripts.scrape_bookmarks", fake)
            stats = b.capture(days_back=1, history_path=history_db)
            return stats, exit_code(stats)
        go.calls, go.env = calls, env
        return go

    @staticmethod
    def _todays_outcomes():
        return {**{u: "auth" for u in _ACM}, **{u: "too_short" for u in _SHORT}}

    def test_todays_auth_and_too_short_exit_zero_with_summary_and_per_url_lines(self, run, caplog):
        caplog.set_level("INFO", logger=b.__name__)
        stats, rc = run(_scrape_stats(failed=2, too_short=5), self._todays_outcomes())
        assert rc == 0
        assert stats["failed"] == 0 and stats["reading_failed"] == 2 and stats["reading_too_short"] == 5
        msgs = [(r.levelname, r.getMessage()) for r in caplog.records]
        for url in _ACM:
            assert ("WARNING", f"reading auth: {url}") in msgs
        for url in _SHORT:
            assert ("INFO", f"reading too_short: {url}") in msgs
        summary = [m for lvl, m in msgs if lvl == "WARNING" and m.startswith("reading summary")]
        assert len(summary) == 1
        assert "auth=2" in summary[0] and "too_short=5" in summary[0] and "failed=2" in summary[0]
        assert "advisory" in summary[0]

    def test_scrape_is_called_with_outcomes_log_name_and_batch_env(self, run):
        run(_scrape_stats(failed=2, too_short=5), self._todays_outcomes())
        (urls, kw), = run.calls
        assert {u for _, u in urls} == set(_ACM + _SHORT)
        assert isinstance(kw["outcomes"], dict)
        assert kw["log_name"] == b.CRAWLEE_LOG_NAME == "capture-browser-crawlee"
        assert kw["env"] is run.env                       # the bookmarks constant itself
        assert kw["timeout"] is not None and kw["timeout"] < 3600

    def test_batch_env_is_imported_not_duplicated(self):
        import inspect
        assert "BATCH_SCRAPE_MAX_REQUEST_RETRIES" not in inspect.getsource(b)

    def test_kept_log_name_does_not_collide_with_bookmarks(self):
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location("sb_names", pathlib.Path("scripts/scrape_bookmarks.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Each job clears "<name>-*.log": neither name may be a prefix of the other.
        assert not b.CRAWLEE_LOG_NAME.startswith(mod.CRAWLEE_LOG_NAME + "-")
        assert not mod.CRAWLEE_LOG_NAME.startswith(b.CRAWLEE_LOG_NAME + "-")
        assert b.CRAWLEE_LOG_NAME != mod.CRAWLEE_LOG_NAME
        assert mod.BATCH_SCRAPE_ENV["BATCH_SCRAPE_MAX_REQUEST_RETRIES"] == "1"

    def test_day_log_ingest_failure_exits_one(self, run):
        stats, rc = run(_scrape_stats(processed=7), {u: "ok" for u in _ACM + _SHORT}, ingest_failed=1)
        assert rc == 1 and stats["failed"] == 1 and stats["reading_failed"] == 0

    def test_crawlee_broken_exits_one(self, run, caplog):
        caplog.set_level("INFO", logger=b.__name__)
        stats, rc = run(_scrape_stats(crawlee_broken=1, deferred=7, batches_with_output=0), {})
        assert rc == 1 and stats["failed"] >= 1 and stats["reading_failed"] == 0
        assert any("crawlee_broken=1" in r.getMessage() and "reading scrape failed" in r.getMessage()
                   for r in caplog.records if r.levelname == "WARNING")

    def test_failed_batch_exits_one(self, run):
        stats, rc = run(_scrape_stats(failed_batches=1, deferred=7), {})
        assert rc == 1 and stats["failed"] == 2          # + systemic (c): 0 of 7 attempted

    def test_timed_out_batch_exits_one(self, run):
        stats, rc = run(_scrape_stats(timed_out_batches=1, deferred=7), {})
        assert rc == 1 and stats["failed"] == 2          # + systemic (c): 0 of 7 attempted

    # Systemic outage: per-URL outcomes stay advisory unless the whole scrape is down.
    @staticmethod
    def _summary(caplog):
        lines = [r.getMessage() for r in caplog.records
                 if r.levelname == "WARNING" and r.getMessage().startswith("reading summary")]
        assert len(lines) == 1
        return lines[0]

    def test_systemic_a_batches_ran_without_output_exits_one(self, run, caplog):
        caplog.set_level("INFO", logger=b.__name__)
        stats, rc = run(_scrape_stats(failed=2, deferred=5, batches_with_output=0), {u: "error" for u in _ACM})
        assert rc == 1 and stats["failed"] == 1 and stats["reading_failed"] == 2
        assert "reading scrape failed (systemic: 1 batch(es) ran, none produced output)" in self._summary(caplog)

    def test_systemic_b_no_page_reached_across_hosts_exits_one(self, run, caplog):
        caplog.set_level("INFO", logger=b.__name__)
        outcomes = {**{u: "auth" for u in _ACM}, _SHORT[0]: "timeout", **{u: "error" for u in _SHORT[1:]}}
        stats, rc = run(_scrape_stats(failed=7), outcomes)
        assert rc == 1 and stats["failed"] == 1
        assert "systemic: 0 pages reached, 7 URL(s) auth/timeout/error across 3 hosts" in self._summary(caplog)

    def test_systemic_c_nothing_attempted_exits_one(self, run, caplog):
        caplog.set_level("INFO", logger=b.__name__)
        stats, rc = run(_scrape_stats(deferred=7, batches_run=0, batches_with_output=0), {})
        assert rc == 1 and stats["failed"] == 1
        assert "systemic: 7 URL(s) sent, 0 attempted (deferred=7)" in self._summary(caplog)

    def test_three_auth_urls_on_one_host_exit_zero(self, run, caplog):
        caplog.set_level("INFO", logger=b.__name__)
        acm = _ACM + ["https://queue.acm.org/doi/10.1145/3611111"]
        stats, rc = run(_scrape_stats(failed=3), {u: "auth" for u in acm}, urls=acm)
        assert rc == 0 and stats["failed"] == 0 and stats["reading_failed"] == 3
        assert "advisory" in self._summary(caplog) and "systemic" not in self._summary(caplog)

    def test_todays_case_is_not_systemic(self, run, caplog):
        # 2 queue.acm.org auth + 5 too_short elsewhere: too_short means the page was reached.
        caplog.set_level("INFO", logger=b.__name__)
        stats, rc = run(_scrape_stats(failed=2, too_short=5), self._todays_outcomes())
        assert rc == 0 and stats["failed"] == 0
        assert "systemic" not in self._summary(caplog)

    def test_mixed_run_with_one_ok_exits_zero(self, run):
        outcomes = {_SHORT[0]: "ok", **{u: "auth" for u in _ACM}, **{u: "error" for u in _SHORT[1:]}}
        stats, rc = run(_scrape_stats(processed=1, failed=6), outcomes)
        assert rc == 0 and stats["failed"] == 0 and stats["reading_failed"] == 6

    def test_real_scrape_with_every_url_failed_exits_one(self, history_db, tmp_path, monkeypatch, caplog):
        """The real scrape_with_crawlee drives a fake ``node`` (first on PATH) that prints
        batch-scrape.js's per-URL "✗ <url>" line for every URL and exits 0, as a Chromium
        that fails every page does: systemic rule (b) must fail the job."""
        import stat
        import src.db, src.ingest
        import scripts.scrape_bookmarks as sb
        from src.capture.common import exit_code
        caplog.set_level("INFO", logger=b.__name__)
        bindir, crawlee = tmp_path / "bin", tmp_path / "crawlee"
        bindir.mkdir(); crawlee.mkdir()
        node = bindir / "node"
        # argv: node batch-scrape.js <url-file>
        node.write_text('#!/bin/sh\nwhile IFS= read -r url; do printf \'✗ %s\\n\' "$url"; done < "$2"\nexit 0\n')
        node.chmod(node.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setenv("SECOND_BRAIN_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path / "status"))
        monkeypatch.setattr(sb, "CRAWLEE_DIR", str(crawlee))
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set())
        urls = ["https://news.example.com/a", "https://news.example.com/b", "https://blog.example.org/c"]
        monkeypatch.setattr(b, "reading_urls", lambda s: [("t", u) for u in urls])
        monkeypatch.setattr(b, "completed_days", lambda n: [date(2026, 9, 15)])
        monkeypatch.setattr(src.db, "get_processed_source_urls", lambda st: set())
        monkeypatch.setattr(src.ingest, "ingest_batch", lambda items, st: {
            "processed": len(items), "skipped": 0, "failed": 0})
        stats = b.capture(days_back=1, history_path=history_db)
        assert exit_code(stats) == 1 and stats["failed"] == 1 and stats["reading_failed"] == 3
        for url in urls:
            assert ("WARNING", f"reading error: {url}") in [(r.levelname, r.getMessage()) for r in caplog.records]
        assert "systemic: 0 pages reached, 3 URL(s) auth/timeout/error across 2 hosts" in self._summary(caplog)
        log = tmp_path / "logs" / f"{b.CRAWLEE_LOG_NAME}-1.log"
        assert log.read_text().splitlines() == [f"\u2717 {u}" for u in urls]
        assert stat.S_IMODE(log.stat().st_mode) == 0o600

    def test_no_page_text_is_logged(self, run, caplog, capsys):
        caplog.set_level("DEBUG")
        stats = _scrape_stats(failed=2, too_short=5)
        stats["page_text"] = _PAGE_TEXT                  # even if the scraper returned text
        run(stats, self._todays_outcomes())
        reading_lines = [r.getMessage() for r in caplog.records
                         if r.name == b.__name__ and r.getMessage().startswith("reading ")]
        urls = set(_ACM + _SHORT)
        for line in reading_lines:
            if not line.startswith("reading summary"):
                cls, _, url = line[len("reading "):].partition(": ")
                assert cls in ("auth", "timeout", "error", "too_short") and url in urls
        assert _PAGE_TEXT not in caplog.text
        assert _PAGE_TEXT not in "".join(capsys.readouterr())

    def test_select_and_record_hooks_are_the_integration_points(self, run, monkeypatch):
        seen = {}
        monkeypatch.setattr(b, "_select_reading", lambda fresh: seen.setdefault("select", list(fresh))[:1])
        monkeypatch.setattr(b, "_record_reading", lambda outcomes: seen.setdefault("record", dict(outcomes)))
        run(_scrape_stats(failed=1), {_ACM[0]: "auth"})
        assert {u for _, u in seen["select"]} == set(_ACM + _SHORT)
        (urls, _), = run.calls
        assert urls == seen["select"][:1]                 # what _select_reading returns is what is sent
        assert seen["record"] == {_ACM[0]: "auth"}

class TestReadingLedgerWiring:
    """_select_reading / _record_reading go through src.capture.reading_ledger."""

    def test_select_drops_bot_walled_and_cooling_urls_and_keeps_order(self, tmp_path, monkeypatch):
        from src.capture import reading_ledger
        monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path))
        reading_ledger.record_outcomes({"https://blog.example.org/cooling": "too_short"}, status_dir=tmp_path)
        fresh = [("a", "https://news.example.com/1"), ("b", "https://queue.acm.org/doi/10.1145/1"),
                 ("c", "https://blog.example.org/cooling"), ("d", "https://news.example.com/2")]
        assert b._select_reading(fresh) == [fresh[0], fresh[3]]

    def test_record_writes_the_reading_ledger_not_the_bookmarks_one(self, tmp_path, monkeypatch):
        import json
        from src.capture import reading_ledger, unreachable
        monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path))
        b._record_reading({"https://blocked.example.net/x": "auth"})
        data = json.loads((tmp_path / reading_ledger.FILENAME).read_text())
        assert len(data["urls"]) == 1
        assert not (tmp_path / unreachable.FILENAME).exists()

    def test_a_broken_ledger_keeps_everything_but_the_seed(self, tmp_path, monkeypatch):
        from src.capture import reading_ledger
        monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path))
        (tmp_path / reading_ledger.FILENAME).write_text("{not json")
        fresh = [("a", "https://news.example.com/1")]
        assert b._select_reading(fresh) == fresh


class TestSnapshot:
    """Regression for 2026-10-10: Chrome holds History locked; the snapshot must never block."""

    @staticmethod
    def _snapshot_bounded(path, limit=10.0):
        import threading
        result = {}

        def run():
            try:
                result["snap"] = b.snapshot_history(path)
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
                result["error"] = exc

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(limit)
        assert not t.is_alive(), f"snapshot_history blocked for more than {limit}s on a locked source"
        if "error" in result:
            raise result["error"]
        return result["snap"]

    def test_snapshot_of_exclusively_locked_history_completes_quickly(self, history_db):
        import time as _time
        holder = sqlite3.connect(history_db, isolation_level=None, check_same_thread=False)
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("UPDATE urls SET title = 'uncommitted' WHERE id = 1")
        try:
            started = _time.monotonic()
            snap = self._snapshot_bounded(history_db)
            assert _time.monotonic() - started < 5
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        try:
            assert snap != history_db and os.path.dirname(snap) != os.path.dirname(history_db)
            visits = b.load_visits(snap, date(2026, 9, 15))
            assert len(visits) == 12                                   # committed data is all there
            assert all(v["title"] != "uncommitted" for v in visits)    # the open transaction is not
        finally:
            b.discard_snapshot(snap)
        assert not os.path.exists(os.path.dirname(snap))

    def test_snapshot_copies_wal_sidecar(self, tmp_path):
        path = str(tmp_path / "History")
        writer = sqlite3.connect(path, isolation_level=None)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE t (x)")
        writer.execute("INSERT INTO t VALUES (42)")
        assert os.path.getsize(path + "-wal") > 0                     # row lives only in the WAL
        try:
            snap = self._snapshot_bounded(path)
        finally:
            writer.close()
        try:
            conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
            assert conn.execute("SELECT x FROM t").fetchall() == [(42,)]
            conn.close()
        finally:
            b.discard_snapshot(snap)

    def test_missing_source_raises_and_leaves_no_temp_dir(self, tmp_path):
        import glob, tempfile
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "sb-history-*")))
        with pytest.raises(OSError):
            b.snapshot_history(str(tmp_path / "nope"))
        assert set(glob.glob(os.path.join(tempfile.gettempdir(), "sb-history-*"))) == before

    @staticmethod
    def _torn_copies(monkeypatch, torn):
        """The first ``torn`` copies of the main file land half-written (Chrome mid-write)."""
        real = b.shutil.copyfile
        calls = {"main": 0}

        def copyfile(src, dst, *a, **kw):
            result = real(src, dst, *a, **kw)
            if os.path.basename(dst) == "History":
                calls["main"] += 1
                if calls["main"] <= torn:
                    with open(dst, "r+b") as handle:
                        handle.truncate(os.path.getsize(dst) // 2)
            return result
        monkeypatch.setattr(b.shutil, "copyfile", copyfile)
        sleeps = []
        monkeypatch.setattr(b, "_sleep", sleeps.append)
        return calls, sleeps

    def test_torn_copy_is_retried_and_the_good_copy_is_used(self, history_db, monkeypatch):
        import glob, tempfile
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "sb-history-*")))
        calls, sleeps = self._torn_copies(monkeypatch, torn=1)
        snap = b.snapshot_history(history_db)
        try:
            assert calls["main"] == 2 and sleeps == [0.5]
            assert len(b.load_visits(snap, date(2026, 9, 15))) == 12
            # the torn attempt's private dir is gone; only the good snapshot remains
            assert set(glob.glob(os.path.join(tempfile.gettempdir(), "sb-history-*"))) - before == {os.path.dirname(snap)}
        finally:
            b.discard_snapshot(snap)

    def test_persistently_torn_copy_raises_after_three_attempts(self, history_db, monkeypatch):
        import glob, tempfile
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "sb-history-*")))
        calls, sleeps = self._torn_copies(monkeypatch, torn=99)
        with pytest.raises(sqlite3.DatabaseError):
            b.snapshot_history(history_db)
        assert calls["main"] == b.SNAPSHOT_ATTEMPTS == 3
        assert sleeps == [0.5, 1.0] and all(0.5 <= s <= 2.0 for s in sleeps)
        assert set(glob.glob(os.path.join(tempfile.gettempdir(), "sb-history-*"))) == before

    def test_quick_check_failure_without_an_open_error_is_also_retried(self, history_db, monkeypatch):
        real_connect = b.sqlite3.connect
        state = {"n": 0}

        class Probe:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, *a):
                if sql.startswith("PRAGMA quick_check"):
                    state["n"] += 1
                    if state["n"] == 1:
                        return self.conn.execute("SELECT '*** in database main ***'")
                return self.conn.execute(sql, *a)

            def close(self):
                self.conn.close()
        monkeypatch.setattr(b.sqlite3, "connect", lambda *a, **kw: Probe(real_connect(*a, **kw)))
        monkeypatch.setattr(b, "_sleep", lambda s: None)
        snap = b.snapshot_history(history_db)
        try:
            assert state["n"] == 2
        finally:
            b.discard_snapshot(snap)
