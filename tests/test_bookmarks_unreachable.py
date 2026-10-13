"""Hermetic tests for the bookmarks unreachable ledger, the batch-scrape.js output
parser and the auth-required classification. No node, network or Chrome."""
import importlib.util
import json
import os
import pathlib
import stat
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from src.capture import unreachable
from src.capture.filters import AUTH_REQUIRED, is_private_host, needs_cookies
from src.capture.unreachable import COOL_OFF, THRESHOLD, Ledger, url_key

T0 = datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc)
URL = "https://example.org/some/page?q=1"


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "jobs" / unreachable.FILENAME)


class TestLedger:
    @pytest.mark.parametrize("cls", ["auth", "timeout", "error"])
    def test_skipped_only_after_threshold_consecutive_failures(self, ledger, cls):
        for attempt in range(1, THRESHOLD):
            ledger.record(URL, cls, now=T0 + timedelta(days=7 * attempt))
            assert not ledger.should_skip(URL, now=T0 + timedelta(days=7 * attempt + 1))
        third = T0 + timedelta(days=7 * THRESHOLD)
        entry = ledger.record(URL, cls, now=third)
        assert entry["consecutive_failures"] == THRESHOLD and entry["status"] == cls
        assert ledger.should_skip(URL, now=third + timedelta(days=1))

    def test_cool_off_expires_after_30_days_and_a_new_failure_restarts_it(self, ledger):
        for i in range(THRESHOLD):
            ledger.record(URL, "auth", now=T0 + timedelta(days=i))
        last = T0 + timedelta(days=THRESHOLD - 1)
        assert ledger.should_skip(URL, now=last + COOL_OFF - timedelta(seconds=1))
        assert not ledger.should_skip(URL, now=last + COOL_OFF + timedelta(seconds=1))
        retry = last + COOL_OFF + timedelta(days=1)
        ledger.record(URL, "error", now=retry)                 # the one retry fails again
        assert ledger.should_skip(URL, now=retry + timedelta(days=29))

    def test_success_clears_the_streak(self, ledger):
        ledger.record(URL, "error", now=T0)
        ledger.record(URL, "error", now=T0)
        ledger.record(URL, "ok", now=T0)
        assert url_key(URL) not in ledger.entries
        ledger.record(URL, "error", now=T0)
        assert not ledger.should_skip(URL, now=T0)

    def test_too_short_is_attempted_but_empty_for_30_days(self, ledger):
        entry = ledger.record(URL, "too_short", now=T0)
        assert entry["status"] == "too_short" and entry["consecutive_failures"] == 0
        assert ledger.should_skip(URL, now=T0 + timedelta(days=29))
        assert not ledger.should_skip(URL, now=T0 + timedelta(days=31))

    def test_unknown_class_is_an_error(self, ledger):
        assert ledger.record(URL, "weird", now=T0)["status"] == "error"

    def test_saved_atomically_0600_and_holds_hash_and_domain_only(self, ledger, monkeypatch):
        ledger.record(URL, "auth", now=T0)
        ledger.save()
        mode = stat.S_IMODE(os.stat(ledger.path).st_mode)
        assert mode == 0o600
        text = ledger.path.read_text()
        assert URL not in text and "/some/page" not in text
        data = json.loads(text)
        assert data["urls"][url_key(URL)]["domain"] == "example.org"
        assert not [p for p in ledger.path.parent.iterdir() if p.name.startswith(".unreachable-")]
        reloaded = Ledger.load(ledger.path)
        assert reloaded.entries == ledger.entries

        # A failed write leaves the previous file intact and no temp file behind.
        before = ledger.path.read_text()
        ledger.record("https://other.example/x", "error", now=T0)

        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(unreachable.os, "replace", boom)
        with pytest.raises(OSError):
            ledger.save()
        assert ledger.path.read_text() == before
        assert not [p for p in ledger.path.parent.iterdir() if p.name.startswith(".unreachable-")]

    def test_missing_or_corrupt_file_is_an_empty_ledger(self, tmp_path):
        assert Ledger.load(tmp_path / "nope.json").entries == {}
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert Ledger.load(bad).entries == {}
        bad.write_text(json.dumps({"urls": {"k": "not-a-dict", "j": {"skip_until": "garbage"}}}))
        loaded = Ledger.load(bad)
        assert list(loaded.entries) == ["j"]

    def test_deferred_runs_count_then_one_timeout_strike_leaving_strikes_untouched(self, ledger):
        url = "https://example.org/stuck"
        for run in range(1, unreachable.DEFERRAL_LIMIT):
            assert ledger.record_deferred(url) is None
            assert ledger.deferred_runs(url) == run
            assert "consecutive_failures" not in ledger.entries[url_key(url)]
            assert not ledger.should_skip(url)
        entry = ledger.record_deferred(url)
        assert entry["status"] == "timeout" and entry["consecutive_failures"] == 1
        assert ledger.deferred_runs(url) == 0

    def test_deferral_keeps_an_existing_entrys_strikes_and_an_attempt_resets_it(self, ledger):
        url = "https://example.org/x"
        ledger.record(url, "error")
        ledger.record(url, "error")
        before = dict(ledger.entries[url_key(url)])
        ledger.record_deferred(url)
        after = ledger.entries[url_key(url)]
        assert {k: v for k, v in after.items() if k != "deferred_runs"} == before
        assert after["deferred_runs"] == 1
        ledger.record(url, "timeout")                       # an attempt ends the deferral streak
        assert "deferred_runs" not in ledger.entries[url_key(url)]
        ledger.record_deferred(url)
        ledger.record(url, "ok")
        assert url_key(url) not in ledger.entries

    def test_old_ledger_json_without_deferred_runs_loads_unchanged(self, tmp_path):
        path = tmp_path / unreachable.FILENAME
        old = {"schema_version": 1, "threshold": 3, "cool_off_days": 30, "urls": {
            url_key("https://a.example/"): {"domain": "a.example", "status": "auth", "attempts": 2,
                                            "consecutive_failures": 2, "skip_until": None,
                                            "first_failed_at": "2026-10-03T01:00:00+00:00",
                                            "last_attempt_at": "2026-10-10T01:00:00+00:00"}}}
        path.write_text(json.dumps(old))
        ledger = Ledger.load(path)
        assert ledger.deferred_runs("https://a.example/") == 0 and not ledger.should_skip("https://a.example/")
        ledger.save()
        assert json.loads(path.read_text())["urls"] == old["urls"]
        # A deferral-only entry (no status / skip_until) is not a cool-off.
        ledger.record_deferred("https://b.example/")
        ledger.save()
        reloaded = Ledger.load(path)
        assert reloaded.deferred_runs("https://b.example/") == 1 and not reloaded.should_skip("https://b.example/")

    def test_default_path_follows_the_status_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path / "s"))
        assert unreachable.default_path() == tmp_path / "s" / "bookmarks.unreachable.json"


@pytest.fixture
def sb(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scrape_bookmarks", pathlib.Path("scripts/scrape_bookmarks.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "CRAWLEE_DIR", str(tmp_path))
    monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("SECOND_BRAIN_LOG_DIR", str(tmp_path / "logs"))
    cookie = tmp_path / "midway-cookie"     # a valid Midway session (never the real ~/.midway/cookie)
    cookie.write_text("#HttpOnly_midway-auth.amazon.com\tFALSE\t/\tTRUE\t4102444800\tsession\tv\n")
    monkeypatch.setattr(mod, "MIDWAY_COOKIE_PATH", str(cookie))
    return mod


SAMPLE = """Loaded 6 URLs from /tmp/.sb-urls-x.txt
INFO  PlaywrightCrawler: Starting the crawler.
✓ Some page title
⚠ Skip (too short): https://a.example/short
WARN  PlaywrightCrawler: Reclaiming failed request back to the list or queue. Request blocked - received 403 status code. {"id":"1","url":"https://b.example/blocked","retryCount":1}
ERROR PlaywrightCrawler: Request failed and reached maximum retries. Error: Request blocked - received 403 status code. {"id":"1","url":"https://b.example/blocked","method":"GET"}
✗ https://b.example/blocked
WARN  PlaywrightCrawler: Reclaiming failed request back to the list or queue. page.goto: Timeout 30000ms exceeded. {"id":"2","url":"https://c.example/slow","retryCount":3}
✗ https://c.example/slow
ERROR PlaywrightCrawler: Request failed and reached maximum retries. page.evaluate: Execution context was destroyed {"id":"3","url":"https://d.example/nav"}
✗ https://d.example/nav
✗ Some title that is not a url
Done. Processed 6 URLs.
"""


class TestCrawleeOutputParser:
    def test_lines_map_to_url_classes(self, sb):
        assert sb.parse_crawlee_output(SAMPLE) == {
            "https://a.example/short": "too_short",
            "https://b.example/blocked": "auth",
            "https://c.example/slow": "timeout",
            "https://d.example/nav": "error",
        }

    def test_empty_or_missing_output(self, sb):
        assert sb.parse_crawlee_output("") == {}
        assert sb.parse_crawlee_output(None) == {}

    def test_batch_outcomes_combine_index_and_output_and_survive_a_timeout(self, sb, monkeypatch):
        urls = [("t", u) for u in ("https://ok.example/1", "https://a.example/short",
                                   "https://b.example/blocked", "https://never.example/started")]
        index = set()

        def run(cmd, timeout=None, stdout=None, **kw):
            stdout.write("✓ Fine\n⚠ Skip (too short): https://a.example/short\n✗ https://b.example/blocked\n")
            stdout.flush()
            index.add("https://ok.example/1")
            raise subprocess.TimeoutExpired(cmd, timeout)

        monkeypatch.setattr(sb.subprocess, "run", run)
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set(index))
        outcomes = {}
        stats = sb.scrape_with_crawlee(urls, timeout=5000, outcomes=outcomes)
        # Deliberately changed (triage 2026-10-13): a URL the killed batch never reached is
        # deferred — no ledger outcome/strike, retried next run — not a "timeout" failure.
        assert outcomes == {"https://ok.example/1": "ok", "https://a.example/short": "too_short",
                            "https://b.example/blocked": "error"}
        assert stats["processed"] == 1 and stats["too_short"] == 1 and stats["failed"] == 1
        assert stats["deferred"] == 1 and stats["timed_out_batches"] == 1

    def test_unlisted_url_after_a_normal_exit_is_an_error(self, sb, monkeypatch):
        monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set())
        outcomes = {}
        sb.scrape_with_crawlee([("t", "https://x.example/")], timeout=5000, outcomes=outcomes)
        assert outcomes == {"https://x.example/": "error"}


class TestMainWithLedger:
    def _bookmarks(self, sb, tmp_path, monkeypatch, urls):
        bookmarks = tmp_path / "Bookmarks"
        bookmarks.write_text(json.dumps({"roots": {"bar": {"type": "folder", "children": [
            {"type": "url", "name": f"B{i}", "url": u} for i, u in enumerate(urls)]}}}))
        monkeypatch.setattr(sb, "BOOKMARKS_PATH", str(bookmarks))
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set())

    def test_three_failed_runs_then_skipped_unreachable_neither_failed_nor_deferred(self, sb, tmp_path, monkeypatch, capsys):
        internal, public, short = ("https://w.amazon.com/bin/view/A", "https://dead.example/x",
                                   "https://empty.example/y")
        self._bookmarks(sb, tmp_path, monkeypatch, [internal, public, short])
        monkeypatch.setattr(sb, "scrape_with_cookies", lambda name, url: "auth")
        seen = []

        def crawlee(urls, timeout=None, outcomes=None, **kw):
            seen.append(sorted(u for _, u in urls))
            for _, u in urls:
                outcomes[u] = "too_short" if u == short else "error"
            failed = sum(1 for _, u in urls if u != short)
            return {"processed": 0, "skipped": 0, "failed": failed, "too_short": len(urls) - failed,
                    "timed_out_batches": 0, "deferred": 0}
        monkeypatch.setattr(sb, "scrape_with_crawlee", crawlee)

        # Deliberately changed (triage 2026-10-13): one failed public URL next to a reached
        # (too_short) page and a skipped_auth one is a per-URL outcome -> exit 0 (was 1).
        assert sb.main() == 0                       # run 1: the public URL failed
        assert sb.main() == 0                       # run 2
        assert sb.main() == 0                       # run 3: third consecutive failure
        assert seen == [[public, short], [public], [public]]   # too-short not re-scraped
        capsys.readouterr()
        assert sb.main() == 0                       # run 4: everything cooling off
        out = capsys.readouterr().out
        assert "skipped_unreachable (ledger cool-off): 3" in out
        assert "Unreachable: 3 skipped_unreachable" in out
        assert len(seen) == 3                       # Crawlee not called at all
        assert json.loads((tmp_path / "jobs" / "bookmarks.backlog.json").read_text())["deferred"] == 0
        assert "dead.example/x" not in (tmp_path / "jobs" / "bookmarks.unreachable.json").read_text()

    def test_unreadable_ledger_never_changes_the_exit(self, sb, tmp_path, monkeypatch, capsys):
        self._bookmarks(sb, tmp_path, monkeypatch, ["https://w.amazon.com/bin/view/A"])
        monkeypatch.setattr(sb, "scrape_with_cookies", lambda name, url: "auth")
        (tmp_path / "jobs").write_text("not a directory")
        assert sb.main() == 0
        assert "could not record the bookmarks unreachable ledger" in capsys.readouterr().out


class TestClassification:
    @pytest.mark.parametrize("url", [
        "https://amazon.sharepoint.com/sites/x/Shared%20Documents/a.docx",
        "https://awsinsights.aws.dev/dashboard",
        "https://help.awsentral.aws.dev/",
        "https://studio.us-east-1.prod.workshops.aws/workshops/abc",
        "https://w.amazon.com/bin/view/X",            # existing internal list still applies
        "https://hyperbadge.amazon.dev/",
        "https://my.day1.amazon.dev/x",
        "https://gather.a2z.com/events",
        "https://collaborate-corp.amazon.com/x",
        "https://corp.amazon.com/",
        "https://amazon.coupahost.com/requisition",
    ])
    def test_auth_only_sites_take_the_cookie_path(self, url):
        assert needs_cookies(url)

    @pytest.mark.parametrize("url", [
        "http://192.168.1.133/", "http://192.168.1.225:18789/ui", "http://10.0.0.5/x",
        "http://172.16.0.1/", "http://172.31.255.1/", "http://localhost:3000/", "http://127.0.0.1/",
        "http://printer.local/", "http://[::1]:8080/",
        # review finding 10
        "http://router.lan/", "http://nas.home.arpa/", "http://grafana.internal:3000/",
        "http://100.64.0.1/", "http://100.127.255.254/", "http://100.101.102.103/",   # CGNAT / Tailscale
        "http://240.0.0.1/",                                                       # reserved
        "http://3232235777/", "http://0x7f000001/", "http://0xC0A80101/", "http://127.1/",
        "http://2130706433:8080/x", "http://[::ffff:192.168.1.1]/",
    ])
    def test_private_lan_hosts_are_skipped(self, url):
        assert is_private_host(url)

    @pytest.mark.parametrize("url", [
        "http://172.32.0.1/", "https://8.8.8.8/", "https://example.org/10.0.0.1",
        "https://local.example.com/", "https://www.ebay.com/itm/1", "not a url",
        "http://10.example.com/", "http://172.32.10.1/", "http://100.128.0.1/", "http://100.63.255.255/",
        "http://134744072/",                      # 8.8.8.8 as an integer: public
        "https://lan.example.com/", "https://internal.example.org/", "https://example.internal.com/",
        "http://0xdeadbeef.example.com/", "http://123abc/",
    ])
    def test_public_hosts_are_not_private(self, url):
        assert not is_private_host(url)

    def test_shared_lists_used_by_browser_history_are_unchanged(self):
        """browser.py imports SKIP_PATTERNS and INTERNAL_DOMAINS; the bookmark routing
        changes live in AUTH_REQUIRED / is_private_host, which only bookmarks use."""
        from src.capture import filters
        assert not filters.SKIP_PATTERNS.search("http://192.168.1.249/")
        assert not filters.INTERNAL_DOMAINS.search("https://hyperbadge.amazon.dev/")

    @pytest.mark.parametrize("url", [
        "https://catalog.us-east-1.prod.workshops.aws/workshops/abc",   # public catalog
        "https://notcorp.amazon.com.example.org/",
        "https://evilamazon.dev/",
        "https://catalog.workshops.aws/x",
        "https://d3abc.cloudfront.net/page",                             # generic CDN: ledger decides
        "https://prod.d1234abcd.amplifyapp.com/",                         # generic host: ledger decides
        "https://example.org/?next=https://amazon.sharepoint.com/",      # only the host counts
        "https://notaws.dev/",
        "https://www.ebay.com/itm/1",
    ])
    def test_public_and_generic_hosts_stay_on_crawlee(self, url):
        assert not needs_cookies(url)
        assert not AUTH_REQUIRED.search(url)


class TestInternalDomainAnchoring:
    """Review finding 11: bookmarks judge INTERNAL_DOMAINS on the host only (needs_cookies);
    browser.py keeps the unanchored INTERNAL_DOMAINS.search(url), so its classify() is unchanged."""

    @pytest.mark.parametrize("url", [
        "https://w.amazon.com/bin/view/X", "https://quip-amazon.com/abc", "https://aws.highspot.com/items/1",
        "https://broadcast.amazon.com/videos/1", "https://atoz.amazon.work/learn",
        "https://amazon.awsapps.com/workdocs", "https://docs.hub.amazon.dev/brazil/",
        "https://talent.a2z.com/x", "https://console.harmony.a2z.com/x", "https://idp.federate.amazon.com/",
        "https://W.AMAZON.COM/x", "https://w.amazon.com:443/x",
    ])
    def test_internal_hosts_still_take_the_cookie_path(self, url):
        from src.capture.filters import internal_host
        assert internal_host(url) and needs_cookies(url)

    @pytest.mark.parametrize("url", [
        "https://evil.io/?r=w.amazon.com", "https://evil.io/w.amazon.com/x", "https://evil.io/#w.amazon.com",
        "https://aw.amazon.com/", "https://example.org/redirect?to=https://quip-amazon.com/x",
        "https://user:w.amazon.com@evil.io/",
    ])
    def test_internal_names_outside_the_host_do_not(self, url):
        from src.capture.filters import internal_host
        assert not internal_host(url) and not needs_cookies(url)

    def test_browser_classification_is_unchanged(self):
        from src.capture import browser, filters
        assert browser.INTERNAL_DOMAINS is filters.INTERNAL_DOMAINS
        assert filters.INTERNAL_DOMAINS.search("https://evil.io/?r=w.amazon.com")   # the shared regex is as before
        assert browser.classify("https://w.amazon.com/bin/view/x") == "internal"


def _load_revert():
    """The revert script, located relative to this file (never the cwd)."""
    import importlib.util
    import pathlib
    script = pathlib.Path(__file__).resolve().parents[1] / "scripts/maintenance/revert_bookmarks_auth_strikes_20261013.py"
    spec = importlib.util.spec_from_file_location("revert_bookmarks_auth_strikes_20261013", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revert_20261013_auth_strikes_is_scoped_and_idempotent(tmp_path, capsys):
    """scripts/maintenance/revert_bookmarks_auth_strikes_20261013.py: one strike off
    internal auth entries last attempted in the 10-13 run window; nothing else."""
    from datetime import datetime, timezone
    from src.capture.unreachable import Ledger, url_key
    mod = _load_revert()
    path = tmp_path / "bookmarks.unreachable.json"
    ledger = Ledger(path)
    d1010 = datetime(2026, 10, 10, 19, 58, tzinfo=timezone.utc)
    d1013 = datetime(2026, 10, 13, 15, 15, tzinfo=timezone.utc)
    later = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)
    wiki, new, public, other = ("https://w.amazon.com/bin/view/A", "https://broadcast.amazon.com/x",
                                "https://www.crunchbase.com/x", "https://w.amazon.com/bin/view/B")
    for url in (wiki, public):
        ledger.record(url, "auth", now=d1010)
        ledger.record(url, "auth", now=d1013)
    ledger.record(new, "auth", now=d1013)                       # first failure was in the run
    ledger.record(other, "auth", now=d1010)
    ledger.record(other, "auth", now=later)                     # outside the window
    ledger.save()

    assert mod.main(["--ledger", str(path)]) == 0               # dry run writes nothing
    assert Ledger.load(path).entries[url_key(wiki)]["attempts"] == 2
    assert mod.main(["--ledger", str(path), "--apply"]) == 0
    entries = Ledger.load(path).entries
    assert entries[url_key(wiki)]["attempts"] == 1 and entries[url_key(wiki)]["consecutive_failures"] == 1
    assert entries[url_key(wiki)]["last_attempt_at"] == d1010.isoformat()
    assert url_key(new) not in entries
    assert entries[url_key(public)]["consecutive_failures"] == 2   # public Crawlee auth is real
    assert entries[url_key(other)]["consecutive_failures"] == 2
    assert (tmp_path / "bookmarks.unreachable.json.pre-revert-20261013").exists()
    before = path.read_text()
    assert mod.main(["--ledger", str(path), "--apply"]) == 0
    assert path.read_text() == before                           # idempotent
    assert "matched 0: {}" in capsys.readouterr().out.splitlines()[-1]


def _revert_ledger(tmp_path, records):
    """A ledger at tmp_path with ``records`` = [(url, [(class, time), ...])]."""
    from src.capture.unreachable import Ledger
    path = tmp_path / "bookmarks.unreachable.json"
    ledger = Ledger(path)
    for url, attempts in records:
        for cls, when in attempts:
            ledger.record(url, cls, now=when)
    ledger.save()
    return path


def _utc(text):
    from datetime import datetime
    return datetime.fromisoformat(text)


def test_revert_refuses_to_run_while_the_bookmarks_job_holds_its_lock(tmp_path, capsys):
    """The job's flock(<status-dir>/bookmarks.lock) (jobs.run_job) is held: exit non-zero,
    nothing written, no backup; the same path the job locks is the one the script locks."""
    import fcntl
    mod = _load_revert()
    url = "https://w.amazon.com/bin/view/A"
    path = _revert_ledger(tmp_path, [(url, [("auth", _utc("2026-10-10T19:58:00+00:00")),
                                            ("auth", _utc("2026-10-13T15:22:30.704+00:00"))])])
    before = path.read_bytes()
    assert mod.LOCK_NAME == "bookmarks.lock"
    with (tmp_path / "bookmarks.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert mod.main(["--ledger", str(path), "--apply"]) == mod.EXIT_LOCKED != 0
        assert "is held" in capsys.readouterr().err
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.pre-revert-*"))
    assert mod.main(["--ledger", str(path), "--apply"]) == 0     # released: applies
    assert path.read_bytes() != before


def test_revert_lock_matches_the_job_wrappers_lock_path(tmp_path, monkeypatch):
    """jobs.run_job locks <SECOND_BRAIN_STATUS_DIR>/bookmarks.lock, the ledger's directory."""
    from src.capture.unreachable import default_path
    mod = _load_revert()
    monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path))
    assert default_path().parent / mod.LOCK_NAME == tmp_path / "bookmarks.lock"
    source = (__import__("pathlib").Path(__file__).resolve().parents[1] / "src/operations/jobs.py").read_text()
    assert '(status_dir / f"{name}.lock")' in source


def test_revert_never_overwrites_an_existing_backup(tmp_path):
    mod = _load_revert()
    url = "https://w.amazon.com/bin/view/A"
    path = _revert_ledger(tmp_path, [(url, [("auth", _utc("2026-10-10T19:58:00+00:00")),
                                            ("auth", _utc("2026-10-13T15:22:30.704+00:00"))])])
    original = path.read_bytes()
    existing = tmp_path / "bookmarks.unreachable.json.pre-revert-20261013"
    existing.write_text("an earlier backup")
    assert mod.main(["--ledger", str(path), "--apply"]) == 0
    assert existing.read_text() == "an earlier backup"
    fresh = tmp_path / "bookmarks.unreachable.json.pre-revert-20261013.1"
    assert fresh.read_bytes() == original and (fresh.stat().st_mode & 0o777) == 0o600


def test_revert_skips_entries_with_more_than_two_attempts(tmp_path, capsys):
    from src.capture.unreachable import Ledger, url_key
    mod = _load_revert()
    url = "https://w.amazon.com/bin/view/A"
    path = _revert_ledger(tmp_path, [(url, [("auth", _utc("2026-10-03T19:58:00+00:00")),
                                            ("auth", _utc("2026-10-10T19:58:00+00:00")),
                                            ("auth", _utc("2026-10-13T15:22:30.704+00:00"))])])
    before = path.read_bytes()
    assert mod.plan(Ledger.load(path)) == [(url_key(url), "skip")]
    assert mod.main(["--ledger", str(path), "--apply"]) == 0
    assert "matched 1: {'skip': 1}" in capsys.readouterr().out
    assert path.read_bytes() == before                           # only skips: nothing written
    assert not list(tmp_path.glob("*.pre-revert-*"))


def test_revert_window_edges(tmp_path):
    """15:11:12Z (run start) and 15:23:00Z (widened end: records are written at the end of
    the run) are inside; a second before / after is outside."""
    from src.capture.unreachable import Ledger, url_key
    mod = _load_revert()
    times = {"start": "2026-10-13T15:11:12+00:00", "before": "2026-10-13T15:11:11+00:00",
             "written": "2026-10-13T15:22:30.705+00:00", "end": "2026-10-13T15:23:00+00:00",
             "after": "2026-10-13T15:23:01+00:00"}
    records = [(f"https://w.amazon.com/bin/view/{name}",
                [("auth", _utc("2026-10-10T19:58:00+00:00")), ("auth", _utc(when))]) for name, when in times.items()]
    path = _revert_ledger(tmp_path, records)
    matched = {key for key, _ in mod.plan(Ledger.load(path))}
    expected = {url_key(f"https://w.amazon.com/bin/view/{n}") for n in ("start", "written", "end")}
    assert matched == expected
