"""Hermetic tests for capture-browser's reading browser route (src/capture/browser.py
``browser_route`` + its wiring in ``capture``) and browser_fetch's sign-in hops.

No browser, no MCP server, no network: ``browser_route`` takes a fake ``fetch``
returning ``browser_fetch.PageResult``s; the hop test reuses the fake MCP client
of tests/test_bookmarks_browser.py.
"""
import asyncio
import sys
from datetime import date

import pytest

from src.capture import browser as b
from src.capture import browser_fetch as bf
from tests.test_bookmarks_browser import LONG, FakeBrowser, FakeClock, page

APG = [f"https://apg-library.amazonaws.com/view/{i}?mode=publication" for i in range(3)]
APG_DL = "https://apg-library.amazonaws.com/get-attachments/1/2"
WS = "https://catalog.workshops.aws/abc/en-US"
PUB = "https://news.example.com/a"
CC = "https://www.culturecloud.com/"
HOP = bf.SIGN_IN_HOPS["apg-library.amazonaws.com"]
SENTINEL = "PAGE-BODY-SENTINEL " * 20
HOURS = 19 * 3600.0


def ok(url, text=SENTINEL, **kw):
    return bf.PageResult(url, "ok", text=text, final_url=url, title="APG Library", status=200, **kw)


def signin(url):
    return bf.PageResult(url, "too_short", text="To continue, please log in.",
                         final_url="https://accelerate.amazonaws.com/portal/signin?appId=mpl",
                         title="AWS Accelerate", status=200)


def route(cands, results=None, *, saved=None, session_left=HOURS, deadline_s=3600.0, fetch=None, **kw):
    calls = []

    def fake_fetch(urls, budget, max_pages):
        calls.append((list(urls), budget, max_pages))
        return {u: (results or {}).get(u, bf.PageResult(u, "deferred", detail="budget")) for u in urls}

    def save(title, url, text):
        if saved is not None:
            saved.append((title, url, text))
    out = b.browser_route([("t", u) for u in cands], started=0.0, fetch=fetch or fake_fetch, save=save,
                          session_left=session_left, deadline_s=deadline_s, clock=lambda: 0.0, **kw)
    return out, calls


class TestHostRules:
    @pytest.mark.parametrize("url", [*APG, WS, "https://sage.amazon.dev/x"])
    def test_amazon_owned_hosts_may_take_the_browser(self, url):
        assert b.browser_route_allowed(url)

    @pytest.mark.parametrize("url", [
        PUB, CC, APG_DL,
        "https://apg-library.amazonaws.com.evil.io/view/1",
        "https://mybucket.s3.amazonaws.com/page.html",          # customer content under amazonaws.com
        "https://evil.io/?r=apg-library.amazonaws.com",
    ])
    def test_third_party_customer_and_download_urls_never_do(self, url):
        assert not b.browser_route_allowed(url)

    def test_direct_only_for_hosts_known_to_need_the_browser(self):
        assert all(b.browser_direct(u) for u in APG)
        assert not b.browser_direct(WS) and not b.browser_direct(PUB) and not b.browser_direct(APG_DL)

    def test_culturecloud_is_an_app_never_scraped(self):
        assert b.classify(CC) == "app" and b.classify("https://core.octanner.io/sso/idp-lookup") == "skip"
        assert b.reading_urls({"pages": [{"kind": b.classify(CC), "dwell": 999, "url": CC, "title": "C"}]}) == []

    def test_route_is_off_without_the_bookmarks_rules(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "scripts.scrape_bookmarks", type("SB", (), {})())
        assert not b.browser_route_allowed(APG[0]) and not b.browser_direct(APG[0])


class TestBrowserRoute:
    def test_ok_is_saved_and_other_classes_pass_through(self):
        saved = []
        results = {APG[0]: ok(APG[0]), APG[1]: signin(APG[1]),
                   WS: bf.PageResult(WS, "too_short", text="x", final_url=WS, status=200),
                   APG[2]: bf.PageResult(APG[2], "error", detail="timeout")}
        out, calls = route([APG[0], APG[1], WS, APG[2]], results, saved=saved)
        assert out == {APG[0]: "ok", APG[1]: "auth", WS: "too_short", APG[2]: "timeout"}
        assert saved == [("t", APG[0], SENTINEL)]
        (urls, budget, max_pages), = calls
        assert len(urls) == 4 and max_pages == 4 and 0 < budget <= b.READING_BROWSER_BUDGET_S

    def test_store_failure_is_an_error(self):
        def boom(*a):
            raise OSError("disk full")
        out = b.browser_route([("t", APG[0])], started=0.0, save=boom, session_left=HOURS, deadline_s=3600.0,
                              clock=lambda: 0.0, fetch=lambda urls, budget, n: {APG[0]: ok(APG[0])})
        assert out == {APG[0]: "error"}

    def test_dry_run_save_none_writes_nothing(self):
        out = b.browser_route([("t", APG[0])], started=0.0, save=None, session_left=HOURS, deadline_s=3600.0,
                              clock=lambda: 0.0, fetch=lambda urls, budget, n: {APG[0]: ok(APG[0])})
        assert out == {APG[0]: "ok"}

    def test_cap_defers_the_rest(self, monkeypatch):
        monkeypatch.setattr(b, "READING_BROWSER_MAX_URLS", 2)
        urls = [f"https://apg-library.amazonaws.com/view/{i}" for i in range(5)]
        out, calls = route(urls, {u: ok(u) for u in urls})
        assert calls[0][0] == urls[:2] and calls[0][2] == 2
        assert [out[u] for u in urls] == ["ok", "ok", "deferred", "deferred", "deferred"]

    @pytest.mark.parametrize("left", [None, 0.0, 29 * 60.0])
    def test_short_or_missing_midway_session_defers_without_launch(self, left, caplog):
        caplog.set_level("INFO")
        out, calls = route(APG, session_left=left)
        assert calls == [] and set(out.values()) == {"deferred"}
        warn = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert len(warn) == 1 and "mwinit" in warn[0] and "no ledger strike" in warn[0]

    def test_job_deadline_too_close_defers_without_launch(self, caplog):
        caplog.set_level("INFO")
        out, calls = route(APG, deadline_s=b.READING_DEADLINE_MARGIN_S + 30)
        assert calls == [] and set(out.values()) == {"deferred"}
        assert any("job deadline" in r.getMessage() for r in caplog.records if r.levelname == "WARNING")

    def test_budget_is_bounded_by_session_and_deadline(self):
        _, calls = route(APG, session_left=45 * 60.0)
        assert calls[0][1] == pytest.approx(35 * 60.0 if 35 * 60.0 < b.READING_BROWSER_BUDGET_S
                                            else b.READING_BROWSER_BUDGET_S)
        _, calls = route(APG, deadline_s=b.READING_DEADLINE_MARGIN_S + 200)
        assert calls[0][1] == pytest.approx(200)

    def test_mcp_launch_failure_defers_everything(self):
        results = {u: bf.PageResult(u, "deferred", detail="launch") for u in APG}
        out, _ = route(APG, results)
        assert set(out.values()) == {"deferred"}

    def test_every_page_on_midway_sign_in_is_a_session_problem_not_strikes(self, caplog):
        results = {u: bf.PageResult(u, "auth", final_url="https://midway-auth.amazon.com/login") for u in APG}
        out, _ = route(APG, results)
        assert set(out.values()) == {"deferred"}
        assert any("session problem" in r.getMessage() for r in caplog.records)

    def test_no_page_text_in_logs(self, caplog, capsys):
        caplog.set_level("DEBUG")
        route(APG, {u: ok(u) for u in APG})
        assert "PAGE-BODY-SENTINEL" not in caplog.text
        assert "PAGE-BODY-SENTINEL" not in "".join(capsys.readouterr())


class TestApply:
    def test_browser_classes_replace_crawlee_and_deferred_drops_the_strike(self):
        outcomes = {APG[0]: "too_short", WS: "too_short", PUB: "ok"}
        stats = {"processed": 1, "too_short": 2, "failed": 0}
        deferred = b._apply_browser({APG[0]: "ok", WS: "deferred", APG[1]: "auth"}, outcomes, stats)
        assert deferred == {WS}
        assert outcomes == {APG[0]: "ok", PUB: "ok", APG[1]: "auth"}
        assert stats == {"processed": 2, "too_short": 0, "failed": 1}


class TestSignInHop:
    def test_hop_is_navigated_once_per_session_before_the_first_page_of_its_host(self):
        pages = {u: page(u, [LONG]) for u in [*APG[:2], WS]}
        pages[HOP] = page("https://accelerate.amazonaws.com/", [LONG])
        browser, clock = FakeBrowser(pages), FakeClock()
        out = asyncio.run(bf.fetch_pages([WS, APG[0], APG[1]], client_factory=browser.factory, clock=clock,
                                         sleep=clock.sleep, log=lambda *a: None))
        assert browser.navigated == [WS, HOP, APG[0], APG[1]]
        assert all(out[u].outcome == "ok" for u in (WS, APG[0], APG[1]))

    def test_failed_hop_is_logged_and_the_page_still_runs(self):
        pages = {APG[0]: page(APG[0], [LONG]), HOP: {"nav_error": bf.McpToolError("boom")}}
        browser, clock, lines = FakeBrowser(pages), FakeClock(), []
        out = asyncio.run(bf.fetch_pages([APG[0]], client_factory=browser.factory, clock=clock,
                                         sleep=clock.sleep, log=lines.append))
        assert out[APG[0]].outcome == "ok"
        assert any("sign-in hop" in line and "failed" in line for line in lines)

    def test_no_hop_for_other_hosts(self):
        assert bf.sign_in_hop(WS) is None and bf.sign_in_hop("https://apg-library.amazonaws.com.evil.io/") is None


class TestCaptureWiring:
    """capture() end to end with the real scrape_bookmarks rules; Crawlee, the browser,
    storage, the ledger and the DB are faked."""

    @pytest.fixture
    def run(self, tmp_path, monkeypatch):
        import scripts.scrape_bookmarks as sb
        import src.db, src.ingest
        from src.capture.common import exit_code
        hist = tmp_path / "History"
        hist.write_text("x")
        rec = {"crawlee": [], "fetch": [], "recorded": None, "saved": []}
        monkeypatch.setattr(b, "snapshot_history", lambda p: str(tmp_path / "snap" / "History"))
        monkeypatch.setattr(b, "discard_snapshot", lambda s: None)
        monkeypatch.setattr(b, "load_visits", lambda db, day: [])
        monkeypatch.setattr(b, "completed_days", lambda n: [date(2026, 10, 14)])
        monkeypatch.setattr(b, "_select_reading", lambda fresh: fresh)
        monkeypatch.setattr(b, "_record_reading", lambda o: rec.__setitem__("recorded", dict(o)))
        monkeypatch.setattr(src.db, "get_processed_source_urls", lambda st: set())
        monkeypatch.setattr(src.ingest, "ingest_batch", lambda items, st: {
            "processed": len(items), "skipped": 0, "failed": 0})
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set())
        monkeypatch.setattr(sb, "save_page", lambda name, url, text, fetched_via=None:
                            rec["saved"].append((url, fetched_via)))

        def go(read, crawlee_outcomes, browser_results, session_left=HOURS):
            monkeypatch.setattr(b, "reading_urls", lambda s: [("t", u) for u in read])
            monkeypatch.setattr(sb, "midway_session_seconds_left", lambda path=None: session_left)

            def scrape(urls, **kw):
                rec["crawlee"].append([u for _, u in urls])
                mine = {u: crawlee_outcomes[u] for _, u in urls}
                kw["outcomes"].update(mine)
                vals = list(mine.values())
                return {"processed": vals.count("ok"), "too_short": vals.count("too_short"),
                        "failed": sum(v in ("auth", "timeout", "error") for v in vals), "deferred": 0,
                        "batches_run": 1, "batches_with_output": 1, "failed_batches": 0,
                        "timed_out_batches": 0, "crawlee_broken": 0}
            monkeypatch.setattr(sb, "scrape_with_crawlee", scrape)

            def fetch(urls, budget, max_pages):
                rec["fetch"].append(list(urls))
                return {u: browser_results.get(u, bf.PageResult(u, "deferred", detail="launch")) for u in urls}
            monkeypatch.setattr(b, "_browser_fetch", fetch)
            stats = b.capture(days_back=1, history_path=str(hist))
            return stats, exit_code(stats), rec
        return go

    def test_direct_and_rescued_pages_replace_crawlee_and_are_stored(self, run):
        stats, rc, rec = run([*APG[:2], WS, PUB, APG_DL],
                             {WS: "too_short", PUB: "ok", APG_DL: "too_short"},
                             {APG[0]: ok(APG[0]), APG[1]: ok(APG[1]), WS: ok(WS)})
        assert rec["crawlee"] == [[WS, PUB, APG_DL]]               # apg-library views skip Crawlee
        assert rec["fetch"] == [[*APG[:2], WS]]                    # downloads never take the browser
        assert rec["recorded"] == {APG[0]: "ok", APG[1]: "ok", WS: "ok", PUB: "ok", APG_DL: "too_short"}
        assert sorted(rec["saved"]) == sorted([(APG[0], "browser"), (APG[1], "browser"), (WS, "browser")])
        assert rc == 0 and stats["reading"] == 4 and stats["reading_too_short"] == 1

    def test_short_session_defers_with_no_strike_and_exit_zero(self, run, caplog):
        caplog.set_level("INFO")
        stats, rc, rec = run([*APG, WS], {WS: "too_short"}, {}, session_left=10 * 60.0)
        assert rec["fetch"] == []
        assert rec["recorded"] == {}                                # neither the views nor WS struck
        assert rc == 0 and stats["failed"] == 0
        warns = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"
                 and r.getMessage().startswith("browser route")]
        assert len(warns) == 1

    def test_only_direct_urls_and_mcp_down_is_exit_zero_not_systemic(self, run):
        stats, rc, rec = run(APG, {}, {})                           # fetch -> deferred(launch) for all
        assert rec["crawlee"] == [] and rec["fetch"] == [APG]
        assert rc == 0 and stats["failed"] == 0 and rec["recorded"] == {}

    def test_browser_failures_strike_like_crawlee_failures(self, run):
        stats, rc, rec = run([APG[0], PUB], {PUB: "ok"}, {APG[0]: signin(APG[0])})
        assert rec["recorded"] == {APG[0]: "auth", PUB: "ok"} and rc == 0

    def test_third_party_failures_stay_advisory_and_never_reach_the_browser(self, run):
        stats, rc, rec = run([PUB, "https://blog.example.org/x"],
                             {PUB: "too_short", "https://blog.example.org/x": "too_short"}, {})
        assert rec["fetch"] == [] and rc == 0 and stats["reading_too_short"] == 2
