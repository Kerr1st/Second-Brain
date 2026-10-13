"""Hermetic tests for scripts/scrape_bookmarks.py: Crawlee batching/timeouts and auth classification.

No node, no network, no Chrome: subprocess.run, the Crawlee index and requests are faked.
"""
import importlib.util
import json
import pathlib
import subprocess
import sys
import types

import pytest


@pytest.fixture
def sb(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scrape_bookmarks", pathlib.Path("scripts/scrape_bookmarks.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "CRAWLEE_DIR", str(tmp_path))
    monkeypatch.setattr(mod, "MIDWAY_COOKIE_PATH", str(tmp_path / "no-midway-cookie"))   # never the real one
    monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("SECOND_BRAIN_LOG_DIR", str(tmp_path / "logs"))
    return mod


FUTURE, PAST = 4102444800, 946684800      # 2100-01-01, 2000-01-01
FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _midway_file(path, expires=FUTURE, name="session", domain="midway-auth.amazon.com"):
    path.write_text(f"#HttpOnly_{domain}\tFALSE\t/\tTRUE\t{expires}\t{name}\tv\n")
    return path


class _FakeSession:
    def __init__(self, get):
        self.get = get

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def prepare_request(self, request):
        return request


def _fake_requests(get):
    """Stands in for `requests`: fetch_internal uses `requests.Session()` (cookie policy)."""
    return types.SimpleNamespace(Session=lambda: _FakeSession(get))


class FakeCrawlee:
    """Index that grows as batches 'scrape'; a batch can time out after indexing some URLs."""

    def __init__(self, indexed=(), timeout_batches=(), partial_on_timeout=1, rc=0, output=""):
        self.index = set(indexed)
        self.timeout_batches = set(timeout_batches)
        self.partial = partial_on_timeout
        self.rc = rc
        self.output = output
        self.calls = []
        self.envs = []

    def run(self, cmd, cwd=None, timeout=None, **kw):
        urls = pathlib.Path(cmd[-1]).read_text().split()
        self.calls.append((len(urls), timeout))
        self.envs.append(kw.get("env"))
        if self.output and kw.get("stdout") is not None:
            kw["stdout"].write(self.output)
            kw["stdout"].flush()
        n = len(self.calls)
        if n in self.timeout_batches:
            self.index.update(urls[: self.partial])     # some pages landed before the kill
            raise subprocess.TimeoutExpired(cmd, timeout)
        self.index.update(urls)
        return subprocess.CompletedProcess(cmd, self.rc, "", "")


def _urls(n, prefix="https://example.org/p"):
    return [(f"T{i}", f"{prefix}{i}") for i in range(n)]


class TestCrawleeBatches:
    def test_urls_are_split_into_batches_each_with_its_own_timeout(self, sb, monkeypatch):
        fake = FakeCrawlee()
        monkeypatch.setattr(sb.subprocess, "run", fake.run)
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set(fake.index))
        stats = sb.scrape_with_crawlee(_urls(120), timeout=5000, batch_size=50, batch_timeout=900)
        assert [n for n, _ in fake.calls] == [50, 50, 20]
        assert all(t <= 900 for _, t in fake.calls)
        assert stats["processed"] == 120 and stats["failed"] == 0 and stats["deferred"] == 0

    def test_timeout_prints_a_clear_line_and_keeps_partial_successes(self, sb, monkeypatch, capsys):
        fake = FakeCrawlee(indexed={"https://example.org/p0"}, timeout_batches={1}, partial_on_timeout=10)
        monkeypatch.setattr(sb.subprocess, "run", fake.run)
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set(fake.index))
        stats = sb.scrape_with_crawlee(_urls(60), timeout=5000, batch_size=50, batch_timeout=900)
        out = capsys.readouterr().out
        assert "TIMED OUT" in out and "batch 1/2" in out
        # batch 1: p0 already indexed (skipped), p0..p9 written before the kill -> p1..p9 processed;
        # p10..p49 never show up in the output, so they are deferred (retried next run, no
        # ledger strike) — deliberately no longer "failed" (triage 2026-10-13: the kill, not
        # the URLs, was the problem). Batch 2 completes all 10.
        assert stats == {"processed": 9 + 10, "skipped": 1, "failed": 0, "too_short": 0, "timed_out_batches": 1,
                         "deferred": 40, "crawlee_broken": 0, "batches_run": 2, "batches_with_output": 2,
                         "failed_batches": 0}
        assert "40 deferred" in out
        assert not list(pathlib.Path(sb.CRAWLEE_DIR).glob(".sb-urls-*"))

    def test_killed_batch_counts_only_urls_the_output_shows_attempted_as_failed(self, sb, monkeypatch):
        output = ("✗ https://example.org/p1\n"
                  'WARN PlaywrightCrawler: Reclaiming failed request back to the list or queue. '
                  'Request blocked - received 403 status code. {"id":"x","url":"https://example.org/p2","retryCount":2}\n'
                  'ERROR PlaywrightCrawler: navigation timed out {"id":"y","url":"https://example.org/p3"}\n')
        fake = FakeCrawlee(timeout_batches={1}, partial_on_timeout=1, output=output)
        monkeypatch.setattr(sb.subprocess, "run", fake.run)
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set(fake.index))
        outcomes = {}
        stats = sb.scrape_with_crawlee(_urls(6), timeout=5000, batch_size=50, outcomes=outcomes)
        assert stats["processed"] == 1 and stats["failed"] == 3 and stats["deferred"] == 2
        assert outcomes == {"https://example.org/p0": "ok", "https://example.org/p1": "error",
                            "https://example.org/p2": "auth", "https://example.org/p3": "timeout"}

    def test_batch_log_is_kept_under_logs_mode_600_and_stale_ones_removed(self, sb, monkeypatch, tmp_path):
        logs = tmp_path / "logs"
        monkeypatch.setenv("SECOND_BRAIN_LOG_DIR", str(logs))
        logs.mkdir()
        (logs / "bookmarks-crawlee-9.log").write_text("stale")
        fake = FakeCrawlee(output="Loaded 2 URLs\n✓ T0\n✓ T1\n")
        monkeypatch.setattr(sb.subprocess, "run", fake.run)
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set(fake.index))
        sb.scrape_with_crawlee(_urls(2), timeout=5000, log_name="bookmarks-crawlee",
                               env=sb.BATCH_SCRAPE_ENV)
        kept = logs / "bookmarks-crawlee-1.log"
        assert sorted(p.name for p in logs.iterdir()) == ["bookmarks-crawlee-1.log"]
        assert kept.read_text().startswith("Loaded 2 URLs")
        assert (kept.stat().st_mode & 0o777) == 0o600
        assert fake.envs[0]["BATCH_SCRAPE_MAX_REQUEST_RETRIES"] == "1"
        assert fake.envs[0]["BATCH_SCRAPE_MAX_SESSION_ROTATIONS"] == "0"

    def test_without_log_name_the_temp_log_is_deleted_and_env_untouched(self, sb, monkeypatch, tmp_path):
        monkeypatch.setenv("SECOND_BRAIN_LOG_DIR", str(tmp_path / "logs"))
        fake = FakeCrawlee()
        monkeypatch.setattr(sb.subprocess, "run", fake.run)
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set(fake.index))
        sb.scrape_with_crawlee(_urls(1), timeout=5000)        # the browser channel's call
        assert not (tmp_path / "logs").exists() and fake.envs == [None]

    @pytest.mark.parametrize("rc,output,broken", [
        (1, "node:internal/modules/esm/resolve\nError [ERR_MODULE_NOT_FOUND]: Cannot find package 'crawlee'\n", True),
        (1, "", True),                                        # non-zero, nothing parseable
        (1, "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'crawlee'\n", True),
        (1, "\x1b[31mERROR\x1b[39m Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'crawlee'\n", True),
        (0, "node: command not found\n", True),               # environment patterns need no exit code
        # Deliberately changed (review 2026-10-13, finding 3): the module/package patterns need a
        # non-zero exit, and mark lines (✓/⚠/✗ + a page title or URL) are never matched.
        (0, "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'crawlee'\n", False),
        (0, "✓ Error: Cannot find module 'x' - Stack Overflow\n", False),
        (1, "✓ Error: Cannot find module 'x' - Stack Overflow\n", False),
        (None, "\x1b[32m✓\x1b[39m Error: Cannot find module 'x' - Stack Overflow\n", False),
        (1, "⚠ Skip (too short): https://example.org/Cannot find module\n", False),
        (1, "✗ https://example.org/p0\n", False),             # a real per-URL failure
        (None, "", False),                                    # a kill is attribution, not breakage
        (0, "", False),
    ])
    def test_crawlee_broken_detection(self, sb, rc, output, broken):
        assert sb.crawlee_broken(rc, output) is broken

    def test_missing_node_is_a_broken_batch_not_a_crash(self, sb, monkeypatch):
        def no_node(*a, **k):
            raise FileNotFoundError("node")
        monkeypatch.setattr(sb.subprocess, "run", no_node)
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set())
        stats = sb.scrape_with_crawlee(_urls(2), timeout=5000)
        assert stats["crawlee_broken"] == 1 and stats["deferred"] == 2 and stats["processed"] == 0

    def test_total_budget_stays_under_the_deadline_and_defers_the_rest(self, sb, monkeypatch, capsys):
        clock = {"t": 0.0}
        fake = FakeCrawlee()

        def slow_run(cmd, **kw):
            clock["t"] += kw["timeout"]                  # each batch burns its whole timeout
            return fake.run(cmd, **kw)

        monkeypatch.setattr(sb.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(sb.subprocess, "run", slow_run)
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set(fake.index))
        stats = sb.scrape_with_crawlee(_urls(500), timeout=2500, batch_size=50, batch_timeout=1000)
        assert sum(t for _, t in fake.calls) <= 2500           # never past the budget
        assert [t for _, t in fake.calls] == [1000, 1000, 500]
        assert stats["processed"] == 150 and stats["deferred"] == 350 and stats["failed"] == 0
        assert "deferred" in capsys.readouterr().out

    def test_non_zero_exit_is_still_a_failure(self, sb, monkeypatch):
        fake = FakeCrawlee(rc=1)
        monkeypatch.setattr(sb.subprocess, "run", fake.run)
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set(fake.index))
        stats = sb.scrape_with_crawlee(_urls(3), timeout=5000)
        # Deliberately changed (review finding 12): a non-zero exit that names no failed URL is
        # counted on its own (failed_batches), no longer as max(1, failed) URL failures.
        assert stats["processed"] == 3 and stats["failed"] == 0 and stats["failed_batches"] == 1

    def test_default_budget_is_below_the_bookmarks_job_deadline(self, sb, monkeypatch):
        monkeypatch.setenv("SECOND_BRAIN_JOB_TIMEOUT_BOOKMARKS", "10800")
        assert sb.job_deadline_s() == 10800
        fake = FakeCrawlee()
        monkeypatch.setattr(sb.subprocess, "run", fake.run)
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set(fake.index))
        sb.scrape_with_crawlee(_urls(1))
        assert fake.calls[0][1] < 10800 - sb.DEADLINE_MARGIN_S + 1


def _resp(status=200, url="https://w.amazon.com/bin/view/X", history=(), text="<p>" + "word " * 100 + "</p>"):
    return types.SimpleNamespace(status_code=status, url=url, text=text,
                                 history=[types.SimpleNamespace(url=u) for u in history])


class TestInternalAuth:
    @pytest.fixture
    def fetch(self, sb, monkeypatch, tmp_path):
        monkeypatch.setattr(sb, "KNOWLEDGE_DIR", str(tmp_path / "sources"))
        monkeypatch.setattr(sb, "MIDWAY_COOKIE_PATH", str(tmp_path / "no-midway-cookie"))   # never the real one
        holder = {}
        fake_requests = _fake_requests(lambda *a, **k: holder["resp"])
        fake_cookie = types.SimpleNamespace(chrome=lambda domain_name=None: None)
        monkeypatch.setitem(sys.modules, "requests", fake_requests)
        monkeypatch.setitem(sys.modules, "browser_cookie3", fake_cookie)

        def run(resp):
            holder["resp"] = resp
            return sb.scrape_with_cookies("Page", "https://w.amazon.com/bin/view/X")
        return run

    @pytest.mark.parametrize("resp", [
        _resp(status=401),
        _resp(status=403),
        _resp(url="https://midway-auth.amazon.com/login?next=x",
              history=["https://w.amazon.com/bin/view/X", "https://midway-auth.amazon.com/SSO/redirect"]),
        _resp(url="https://idp.federate.amazon.com/api/oauth2/v1/authorize?x=1"),
        # (Was a bare same-host /signin URL: since the 2026-10-13 review a same-host path is
        # not judged by the URL pattern; a sign-in page is recognised by its body.)
        _resp(url="https://w.amazon.com/signin?return=x", text="<html><title>Sign in</title>" + "x " * 200),
    ])
    def test_sign_in_redirects_and_auth_refusals_are_skipped_auth(self, fetch, resp):
        assert fetch(resp) == "auth"

    @pytest.mark.parametrize("resp", [_resp(status=404), _resp(status=500)])
    def test_genuine_fetch_errors_stay_failed(self, fetch, resp):
        assert fetch(resp) == "failed"

    def test_reached_but_tiny_page_is_too_short(self, fetch):
        assert fetch(_resp(text="tiny")) == "too_short"

    def test_good_page_is_ok(self, fetch):
        assert fetch(_resp()) == "ok"

    def test_main_counts_skipped_auth_without_failing(self, sb, tmp_path, monkeypatch, capsys):
        bookmarks = tmp_path / "Bookmarks"
        bookmarks.write_text(json.dumps({"roots": {"bar": {"type": "folder", "children": [
            {"type": "url", "name": "Internal", "url": "https://w.amazon.com/bin/view/Thing"}]}}}))
        monkeypatch.setattr(sb, "BOOKMARKS_PATH", str(bookmarks))
        monkeypatch.setattr(sb, "MIDWAY_COOKIE_PATH", str(_midway_file(tmp_path / "midway")))
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set())
        monkeypatch.setattr(sb, "scrape_with_cookies", lambda name, url: "auth")
        assert sb.main() == 0
        out = capsys.readouterr().out
        assert "1 skipped_auth, 0 failed" in out
        assert "WARNING: bookmarks summary" in out and "auth=1" in out and "-> exit 0" in out
        # Deliberately changed (triage 2026-10-13): one internal URL failing is a per-URL
        # outcome for the unreachable ledger, no longer a job failure (was exit 1).
        monkeypatch.setattr(sb, "scrape_with_cookies", lambda name, url: "failed")
        assert sb.main() == 0
        assert "error=1" in capsys.readouterr().out


class TestMidwayCookies:
    """~/.midway/cookie (Netscape format, curl-style #HttpOnly_ lines) joins the jar."""

    SECRET = "s3cr3t-value-never-logged"

    @pytest.fixture
    def cookie_file(self, tmp_path):
        future, past = 4102444800, 946684800      # 2100-01-01, 2000-01-01
        path = tmp_path / "cookie"
        path.write_text("\n".join([
            "# Netscape HTTP Cookie File",
            "",
            f"#HttpOnly_midway-auth.amazon.com\tFALSE\t/\tTRUE\t{future}\tsession\t{self.SECRET}",
            f".amazon.com\tTRUE\t/\tTRUE\t{future}\tamazon_enterprise_access\tv2",
            f"#HttpOnly_idp.federate.amazon.com\tFALSE\t/\tTRUE\t{past}\texpired\tgone",
            "malformed line without tabs",
            f"amazon.com\tTRUE\t/\tTRUE\t0\tsess_only\tv3",
        ]) + "\n")
        return path

    def test_httponly_lines_are_kept_comments_expired_and_malformed_dropped(self, sb, cookie_file):
        import http.cookiejar
        jar = sb.load_midway_cookies(str(cookie_file))
        got = {(c.domain, c.name) for c in jar}
        assert got == {("midway-auth.amazon.com", "session"), (".amazon.com", "amazon_enterprise_access"),
                       (".amazon.com", "sess_only")}
        # (MozillaCookieJar drops #HttpOnly_ lines on older Pythons and raises on a single
        # malformed line; the own parser keeps them, skips bad lines and expired cookies.)

    def test_cookies_match_the_midway_redirect_chain(self, sb, cookie_file):
        import urllib.request
        jar = sb.load_midway_cookies(str(cookie_file))
        def sent(url):
            req = urllib.request.Request(url)
            jar.add_cookie_header(req)
            return req.get_header("Cookie") or ""
        assert "session=" in sent("https://midway-auth.amazon.com/SSO/redirect")
        assert "amazon_enterprise_access=" in sent("https://idp.federate.amazon.com/api/oauth2/v1/authorize")
        assert "session=" not in sent("https://w.amazon.com/bin/view/X")     # host-only cookie stays on its host
        assert sent("https://example.org/") == ""

    def test_missing_file_is_an_empty_jar(self, sb, tmp_path):
        assert list(sb.load_midway_cookies(str(tmp_path / "absent"))) == []

    def test_merged_jar_goes_to_requests_and_no_value_is_printed(self, sb, cookie_file, monkeypatch, capsys, tmp_path):
        import http.cookiejar
        monkeypatch.setattr(sb, "MIDWAY_COOKIE_PATH", str(cookie_file))
        monkeypatch.setattr(sb, "KNOWLEDGE_DIR", str(tmp_path / "sources"))
        chrome = http.cookiejar.CookieJar()
        chrome.set_cookie(http.cookiejar.Cookie(0, "chrome_c", "cv", None, False, "w.amazon.com", False, False,
                                                "/", True, True, None, True, None, None, {}, False))
        seen = {}

        def get(url, cookies=None, **kw):
            seen["names"] = {c.name for c in cookies}
            return _resp()
        monkeypatch.setitem(sys.modules, "requests", _fake_requests(get))
        monkeypatch.setitem(sys.modules, "browser_cookie3", types.SimpleNamespace(chrome=lambda domain_name=None: chrome))
        assert sb.scrape_with_cookies("Page", "https://w.amazon.com/bin/view/X") == "ok"
        assert seen["names"] == {"chrome_c", "session", "amazon_enterprise_access", "sess_only"}
        out = capsys.readouterr()
        assert self.SECRET not in out.out + out.err

    def test_expired_midway_session_still_counts_as_auth(self, sb, monkeypatch, tmp_path):
        monkeypatch.setattr(sb, "MIDWAY_COOKIE_PATH", str(tmp_path / "absent"))
        monkeypatch.setitem(sys.modules, "browser_cookie3", types.SimpleNamespace(chrome=lambda domain_name=None: None))
        resp = _resp(url="https://midway-auth.amazon.com/login?next=x",
                     history=["https://w.amazon.com/bin/view/X", "https://idp.federate.amazon.com/api/oauth2/v1/authorize"])
        monkeypatch.setitem(sys.modules, "requests", _fake_requests(lambda *a, **k: resp))
        assert sb.scrape_with_cookies("Page", "https://w.amazon.com/bin/view/X") == "auth"

    def test_chrome_cookie_failure_falls_back_to_midway_only(self, sb, cookie_file, monkeypatch):
        def broken(domain_name=None):
            raise RuntimeError("keychain")
        monkeypatch.setitem(sys.modules, "browser_cookie3", types.SimpleNamespace(chrome=broken))
        jar = sb.internal_cookie_jar("https://w.amazon.com/", midway_path=str(cookie_file))
        assert {c.name for c in jar} == {"session", "amazon_enterprise_access", "sess_only"}


class TestBacklogAlarm:
    """A deferral is normal; three runs in a row that defer are a WARNING (and a
    liveness advisory) — still exit 0."""

    def _main_with_deferrals(self, sb, tmp_path, monkeypatch, deferred):
        bookmarks = tmp_path / "Bookmarks"
        bookmarks.write_text(json.dumps({"roots": {"bar": {"type": "folder", "children": [
            {"type": "url", "name": "Public", "url": "https://example.org/x"}]}}}))
        monkeypatch.setattr(sb, "BOOKMARKS_PATH", str(bookmarks))
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set())
        # processed=1: a run that attempted something but deferred the rest (0 attempted
        # with eligible URLs is a job failure since 2026-10-13, see TestMainExitCodes).
        monkeypatch.setattr(sb, "scrape_with_crawlee", lambda urls, timeout=None, **kw: {
            "processed": 1, "skipped": 0, "failed": 0, "timed_out_batches": 0, "deferred": deferred})
        return sb.main()

    def test_warning_only_after_three_consecutive_deferring_runs(self, sb, tmp_path, monkeypatch, capsys):
        state = tmp_path / "jobs" / "bookmarks.backlog.json"
        for run in (1, 2):
            assert self._main_with_deferrals(sb, tmp_path, monkeypatch, 7) == 0
            assert "WARNING: bookmarks backlog" not in capsys.readouterr().out   # (the summary WARNING is every run)
            assert json.loads(state.read_text())["consecutive_deferred_runs"] == run
        assert self._main_with_deferrals(sb, tmp_path, monkeypatch, 7) == 0
        out = capsys.readouterr().out
        assert "WARNING: bookmarks backlog persists" in out and "3 consecutive" in out
        assert self._main_with_deferrals(sb, tmp_path, monkeypatch, 0) == 0
        assert "WARNING: bookmarks backlog" not in capsys.readouterr().out
        assert json.loads(state.read_text())["consecutive_deferred_runs"] == 0

    def test_backlog_bookkeeping_failure_never_changes_the_exit(self, sb, tmp_path, monkeypatch, capsys):
        (tmp_path / "jobs").write_text("not a directory")
        assert self._main_with_deferrals(sb, tmp_path, monkeypatch, 7) == 0
        assert "could not record the bookmarks backlog state" in capsys.readouterr().out


class TestRealCrawleeRecords:
    """tests/fixtures/crawlee-retry-records.log is real Crawlee 3.15.3 / @apify/log 2.5.28
    output (ANSI colors, multi-line records), captured offline: a PlaywrightCrawler
    (maxRequestRetries=1, navigationTimeoutSecs=3) against a 127.0.0.1 server answering 403,
    starting a download, and never answering; plus a genuine Playwright
    net::ERR_CONNECTION_REFUSED (closed 127.0.0.1 port) thrown through Crawlee's own
    retry/failure logging. The {"id":..,"url":..} data of the 403 / download / refused retry
    WARN records is on a line of its own (basic-crawler.js `[message, userLine].join('\\n')`)."""

    BLOCKED = "http://127.0.0.1:63707/blocked"
    DOWNLOAD = "http://127.0.0.1:63707/download"
    HANG = "http://127.0.0.1:63707/hang"
    REFUSED = "http://127.0.0.1:59998/refused"
    EXPECTED = {BLOCKED: "auth", DOWNLOAD: "error", HANG: "timeout", REFUSED: "error"}

    @pytest.fixture
    def text(self):
        raw = (FIXTURES / "crawlee-retry-records.log").read_text()
        assert "\x1b[" in raw                                   # really colored
        return raw

    @staticmethod
    def _without_marks(text, sb):
        return "\n".join(l for l in text.splitlines() if not sb.strip_ansi(l).lstrip().startswith("✗"))

    def test_finished_run_maps_every_url_to_its_class(self, sb, text):
        assert sb.parse_crawlee_output(text) == self.EXPECTED

    def test_killed_mid_retry_records_alone_map_every_url(self, sb, text):
        # No final ✗ lines (batch killed): the WARN/ERROR records alone must name each URL.
        assert sb.parse_crawlee_output(self._without_marks(text, sb)) == self.EXPECTED

    def test_the_multi_line_retry_warns_alone(self, sb, text):
        # Only the first records, as when a batch is killed during the first retries.
        lines = text.splitlines()
        cut = next(i for i, l in enumerate(lines) if "/download" in l and '"retryCount"' in l)
        assert sb.parse_crawlee_output("\n".join(lines[:cut + 1])) == {self.BLOCKED: "auth", self.DOWNLOAD: "error"}
        refused_warn = [l for l in lines if "Reclaiming" in l and "ERR_CONNECTION_REFUSED" in l]
        start = lines.index(refused_warn[0])
        end = next(i for i in range(start, len(lines)) if '"retryCount"' in lines[i])
        assert end > start                                      # the URL data is on a later line
        assert sb.parse_crawlee_output("\n".join(lines[start:end + 1])) == {self.REFUSED: "error"}

    def test_lone_error_record_with_apify_timeout_stack_frame_is_an_error(self, sb, text):
        # The /download ERROR record's stack ends in node_modules/@apify/timeout/cjs/index.cjs;
        # that frame (on every failed request) must not make it a timeout.
        lines = text.splitlines()
        start = next(i for i, l in enumerate(lines)
                     if sb.strip_ansi(l).startswith("ERROR") and "Download is starting" in l)
        end = next(i for i in range(start, len(lines)) if "/download" in lines[i] and '"uniqueKey"' in lines[i])
        record = "\n".join(lines[start:end + 1])
        assert "@apify/timeout/cjs/index.cjs" in record
        assert sb.parse_crawlee_output(record) == {self.DOWNLOAD: "error"}
        # a real timeout message outside the stack frames still classifies as timeout
        hang = [l for l in lines if sb.strip_ansi(l).startswith("ERROR") and "/hang" in l]
        assert sb.parse_crawlee_output(hang[0]) == {self.HANG: "timeout"}

    def test_the_line_based_parser_would_have_missed_them(self, sb, text):
        """Regression guard for the review's finding: no record's URL data shares the WARN line."""
        plain = sb.strip_ansi(text).splitlines()
        warn_with_url = [l for l in plain if l.startswith("WARN") and '"url"' in l]
        assert [l for l in warn_with_url if "/hang" not in l] == []

    def test_killed_batch_with_real_records_strikes_instead_of_deferring(self, sb, monkeypatch, text):
        urls = [("B", self.BLOCKED), ("D", self.DOWNLOAD), ("H", self.HANG), ("R", self.REFUSED),
                ("N", "http://127.0.0.1:63707/never")]
        fake = FakeCrawlee(timeout_batches={1}, partial_on_timeout=0, output=self._without_marks(text, sb))
        monkeypatch.setattr(sb.subprocess, "run", fake.run)
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set(fake.index))
        outcomes, killed = {}, set()
        stats = sb.scrape_with_crawlee(urls, timeout=5000, outcomes=outcomes, killed_deferred=killed)
        assert outcomes == self.EXPECTED
        assert stats["failed"] == 4 and stats["deferred"] == 1 and killed == {"http://127.0.0.1:63707/never"}

    def test_warn_level_is_recognised_after_the_ansi_strip(self, sb):
        line = '\x1b[33mWARN\x1b[39m \x1b[33m PlaywrightCrawler:\x1b[39m Reclaiming failed request.\n' \
               '\x1b[90m {"id":"a","url":"https://example.org/x","retryCount":1}\x1b[39m\n'
        assert sb.parse_crawlee_output(line) == {"https://example.org/x": "error"}

    def test_info_records_and_data_without_url_map_nothing(self, sb):
        text = ('\x1b[32mINFO\x1b[39m \x1b[33m PlaywrightCrawler:\x1b[39m Final request statistics:'
                '\x1b[90m {"requestsFinished":1}\x1b[39m\n✓ Some title\n')
        assert sb.parse_crawlee_output(text) == {}


class TestDeferralBound:
    """Finding 1c: a URL deferred by killed batches is tried first next run, and after
    DEFERRAL_LIMIT consecutive killed-batch deferrals takes a `timeout` strike."""

    URL = "https://example.org/stuck"

    def _main(self, sb, tmp_path, monkeypatch, crawlee):
        bookmarks = tmp_path / "Bookmarks"
        bookmarks.write_text(json.dumps({"roots": {"bar": {"type": "folder", "children": [
            {"type": "url", "name": "First", "url": "https://example.org/first"},
            {"type": "url", "name": "Stuck", "url": self.URL}]}}}))
        monkeypatch.setattr(sb, "BOOKMARKS_PATH", str(bookmarks))
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set())
        monkeypatch.setattr(sb, "scrape_with_crawlee", crawlee)
        return sb.main()

    def test_three_killed_deferrals_become_one_timeout_strike_and_are_ordered_first(self, sb, tmp_path, monkeypatch):
        from src.capture.unreachable import DEFERRAL_LIMIT, Ledger, url_key
        orders = []

        def crawlee(urls, outcomes=None, killed_deferred=None, **kw):
            orders.append([u for _, u in urls])
            outcomes["https://example.org/first"] = "ok"
            killed_deferred.add(self.URL)
            return {"processed": 1, "skipped": 0, "failed": 0, "deferred": 1, "timed_out_batches": 1,
                    "batches_run": 1, "batches_with_output": 1}
        ledger_path = tmp_path / "jobs" / "bookmarks.unreachable.json"
        for run in range(1, DEFERRAL_LIMIT):
            assert self._main(sb, tmp_path, monkeypatch, crawlee) == 0
            entry = Ledger.load(ledger_path).entries[url_key(self.URL)]
            assert entry["deferred_runs"] == run and "status" not in entry and not entry.get("consecutive_failures")
        assert orders[0] == ["https://example.org/first", self.URL]
        assert orders[1] == [self.URL, "https://example.org/first"]      # deferred URL goes first
        assert self._main(sb, tmp_path, monkeypatch, crawlee) == 0
        entry = Ledger.load(ledger_path).entries[url_key(self.URL)]
        assert entry["status"] == "timeout" and entry["consecutive_failures"] == 1
        assert "deferred_runs" not in entry

    def test_an_attempt_clears_the_count(self, sb, tmp_path, monkeypatch):
        from src.capture.unreachable import Ledger, url_key
        ledger_path = tmp_path / "jobs" / "bookmarks.unreachable.json"

        def deferring(urls, outcomes=None, killed_deferred=None, **kw):
            outcomes["https://example.org/first"] = "ok"
            killed_deferred.add(self.URL)
            return {"processed": 1, "skipped": 0, "failed": 0, "deferred": 1, "batches_run": 1, "batches_with_output": 1}

        def succeeding(urls, outcomes=None, killed_deferred=None, **kw):
            outcomes.update({u: "ok" for _, u in urls})
            return {"processed": 2, "skipped": 0, "failed": 0, "deferred": 0, "batches_run": 1, "batches_with_output": 1}
        self._main(sb, tmp_path, monkeypatch, deferring)
        assert Ledger.load(ledger_path).entries[url_key(self.URL)]["deferred_runs"] == 1
        self._main(sb, tmp_path, monkeypatch, succeeding)
        assert url_key(self.URL) not in Ledger.load(ledger_path).entries


class TestMidwayExpiry:
    """Finding 5: no unexpired midway-auth.amazon.com `session` cookie -> the internal path
    is skipped, its URLs are deferred_auth (no ledger strike), exit 0 for that alone."""

    def _main(self, sb, tmp_path, monkeypatch, cookie_path, outcome="ok"):
        bookmarks = tmp_path / "Bookmarks"
        bookmarks.write_text(json.dumps({"roots": {"bar": {"type": "folder", "children": [
            {"type": "url", "name": f"I{i}", "url": f"https://w.amazon.com/bin/view/I{i}"} for i in range(3)]}}}))
        monkeypatch.setattr(sb, "BOOKMARKS_PATH", str(bookmarks))
        monkeypatch.setattr(sb, "MIDWAY_COOKIE_PATH", str(cookie_path))
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set())
        calls = []
        # (Was "auth": since 2026-10-13 three-of-three auth with a valid session is a
        # systemic deferral, see TestInternalAuthRouting; a valid run here reaches its pages.)
        monkeypatch.setattr(sb, "scrape_with_cookies", lambda name, url: calls.append(url) or outcome)
        return sb.main(), calls

    @pytest.mark.parametrize("make", [
        lambda p: _midway_file(p, expires=PAST),                        # expired
        lambda p: p,                                                    # missing
        lambda p: _midway_file(p, name="amazon_enterprise_access"),     # no session cookie
        lambda p: _midway_file(p, domain=".amazon.com"),                # not the Midway host
    ], ids=["expired", "missing", "no-session", "other-domain"])
    def test_missing_or_expired_cookie_defers_the_internal_path(self, sb, tmp_path, monkeypatch, capsys, make):
        rc, calls = self._main(sb, tmp_path, monkeypatch, make(tmp_path / "midway"))
        out = capsys.readouterr().out
        assert rc == 0 and calls == []
        assert "WARNING: Midway cookie missing/expired — run mwinit; 3 internal URLs deferred" in out
        assert "deferred_auth=3" in out
        ledger = json.loads((tmp_path / "jobs" / "bookmarks.unreachable.json").read_text())
        assert ledger["urls"] == {}                                     # no strike

    def test_valid_cookie_runs_the_internal_path(self, sb, tmp_path, monkeypatch, capsys):
        rc, calls = self._main(sb, tmp_path, monkeypatch, _midway_file(tmp_path / "midway"))
        out = capsys.readouterr().out
        assert rc == 0 and len(calls) == 3 and "Midway cookie missing" not in out
        assert "deferred_auth=0" in out

    def test_expired_runs_build_a_separate_midway_streak_and_a_valid_run_resets_it(
            self, sb, tmp_path, monkeypatch, capsys):
        state = tmp_path / "jobs" / "bookmarks-midway.backlog.json"
        for run in (1, 2, 3):
            rc, _ = self._main(sb, tmp_path, monkeypatch, _midway_file(tmp_path / "midway", expires=PAST))
            out = capsys.readouterr().out
            assert rc == 0 and json.loads(state.read_text())["consecutive_deferred_runs"] == run
            assert json.loads(state.read_text())["deferred"] == 3
            assert ("WARNING: bookmarks-midway backlog persists" in out) is (run == 3)
        assert json.loads((tmp_path / "jobs" / "bookmarks.backlog.json").read_text())["consecutive_deferred_runs"] == 0
        rc, _ = self._main(sb, tmp_path, monkeypatch, _midway_file(tmp_path / "midway"))
        assert rc == 0 and "bookmarks-midway backlog persists" not in capsys.readouterr().out
        assert json.loads(state.read_text())["consecutive_deferred_runs"] == 0

    def test_session_check_reads_no_value(self, sb, tmp_path, capsys):
        path = tmp_path / "midway"
        path.write_text(f"#HttpOnly_midway-auth.amazon.com\tFALSE\t/\tTRUE\t{FUTURE}\tsession\tSECRETVALUE\n")
        assert sb.midway_session_valid(str(path)) is True
        assert "SECRETVALUE" not in capsys.readouterr().out


class TestCookieScoping:
    """Findings 7 and 9: which hosts the merged jar sends Midway cookies to, through the real
    `requests` request preparation (no network) with the strict host-only policy."""

    @pytest.fixture
    def jar(self, sb, tmp_path):
        path = tmp_path / "cookie"
        path.write_text("\n".join([
            f"#HttpOnly_midway-auth.amazon.com\tFALSE\t/\tTRUE\t{FUTURE}\tsession\tsv",
            f".amazon.com\tTRUE\t/\tTRUE\t{FUTURE}\tamazon_enterprise_access\tav",
            f"#HttpOnly_.auth.midway.aws.dev\tTRUE\t/\tTRUE\t{FUTURE}\tmw_dev\tdv",
        ]) + "\n")
        return sb.load_midway_cookies(str(path))

    @staticmethod
    def _sent(sb, jar, url):
        requests = pytest.importorskip("requests")
        with sb.cookie_policy_session(requests) as session:
            prepared = session.prepare_request(requests.Request("GET", url, cookies=jar))
        return prepared.headers.get("Cookie") or ""

    @pytest.mark.parametrize("url", [
        "https://contoso.sharepoint.com/sites/x",
        "https://amazon.coupahost.com/requisition",
        "https://x.coupahost.com/",
        "https://awsinsights.aws.dev/",
        "https://midway-auth.amazon.com.evil.io/",
        "https://midway-auth.amazon.co/",
        "https://example.org/?r=midway-auth.amazon.com",
    ])
    def test_no_midway_cookie_reaches_other_hosts(self, sb, jar, url):
        sent = self._sent(sb, jar, url)
        assert "session=" not in sent and "mw_dev=" not in sent and "amazon_enterprise_access=" not in sent

    def test_look_alike_amazon_host_gets_no_midway_session(self, sb, jar):
        # (.amazon.com cookies are domain cookies by design; the Midway session is host-only.)
        sent = self._sent(sb, jar, "https://evilmidway-auth.amazon.com/")
        assert "session=" not in sent and "mw_dev=" not in sent

    def test_host_only_cookie_is_not_sent_to_subdomains(self, sb, jar):
        assert "session=sv" in self._sent(sb, jar, "https://midway-auth.amazon.com/SSO/redirect")
        assert "session=" not in self._sent(sb, jar, "https://x.midway-auth.amazon.com/")
        # Domain cookies still reach their subdomains (the Midway redirect chain needs them).
        assert "mw_dev=dv" in self._sent(sb, jar, "https://x.auth.midway.aws.dev/")
        assert "amazon_enterprise_access=av" in self._sent(sb, jar, "https://idp.federate.amazon.com/")

    def test_plain_jar_policy_also_rejects_subdomains(self, sb, jar):
        import urllib.request
        req = urllib.request.Request("https://x.midway-auth.amazon.com/")
        jar.add_cookie_header(req)
        assert "session=" not in (req.get_header("Cookie") or "")

    def test_redirect_hops_keep_the_policy(self, sb, jar):
        requests = pytest.importorskip("requests")
        from requests.adapters import BaseAdapter
        seen = []

        class Hops(BaseAdapter):
            def send(self, request, **kw):
                seen.append((request.url, request.headers.get("Cookie") or ""))
                resp = requests.Response()
                resp.request, resp.url, resp.encoding = request, request.url, "utf-8"
                resp._content = b"ok"
                if "w.amazon.com" in request.url:
                    resp.status_code, resp.headers["Location"] = 302, "https://x.midway-auth.amazon.com/a"
                elif "x.midway-auth" in request.url:
                    resp.status_code, resp.headers["Location"] = 302, "https://midway-auth.amazon.com/b"
                else:
                    resp.status_code = 200
                return resp

            def close(self):
                pass
        with sb.cookie_policy_session(requests) as session:
            session.mount("https://", Hops())
            assert session.get("https://w.amazon.com/x", cookies=jar).status_code == 200
        hops = dict(seen)
        assert "session=" not in hops["https://w.amazon.com/x"]
        assert "session=" not in hops["https://x.midway-auth.amazon.com/a"]
        assert "session=sv" in hops["https://midway-auth.amazon.com/b"]


class TestBatchScrapePatchCheck:
    def test_warns_when_the_patch_is_not_applied(self, sb, tmp_path, capsys):
        (tmp_path / "batch-scrape.js").write_text("const crawler = new PlaywrightCrawler({});\n")
        assert sb.check_batch_scrape_patch() is False
        out = capsys.readouterr().out
        assert out.count("WARNING:") == 1 and "crawlee-batch-scrape.patch not applied" in out
        assert str(tmp_path / "batch-scrape.js") in out                  # derived from CRAWLEE_DIR

    def test_silent_when_applied_and_warns_when_missing(self, sb, tmp_path, capsys):
        assert sb.check_batch_scrape_patch() is False                   # no file at all
        assert "WARNING:" in capsys.readouterr().out
        (tmp_path / "batch-scrape.js").write_text("envInt('BATCH_SCRAPE_MAX_REQUEST_RETRIES', 3)\n")
        assert sb.check_batch_scrape_patch() is True
        assert capsys.readouterr().out == ""

    def test_main_warns_once_per_run(self, sb, tmp_path, monkeypatch, capsys):
        bookmarks = tmp_path / "Bookmarks"
        bookmarks.write_text(json.dumps({"roots": {"bar": {"type": "folder", "children": [
            {"type": "url", "name": f"P{i}", "url": f"https://example.org/p{i}"} for i in range(120)]}}}))
        monkeypatch.setattr(sb, "BOOKMARKS_PATH", str(bookmarks))
        fake = FakeCrawlee()
        monkeypatch.setattr(sb.subprocess, "run", fake.run)
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set(fake.index))
        assert sb.main() == 0 and len(fake.calls) == 3
        assert capsys.readouterr().out.count("does not read BATCH_SCRAPE_") == 1


class TestKeptLog:
    def test_planted_symlink_is_refused_not_truncated(self, sb, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("keep me")
        victim.chmod(0o644)
        (logs / "bookmarks-crawlee-1.log").symlink_to(victim)
        monkeypatch.setattr(sb, "_clear_kept_logs", lambda name: None)  # the planted link survives clearing
        with pytest.raises(OSError):
            sb._open_kept_log("bookmarks-crawlee", 1)
        assert victim.read_text() == "keep me" and (victim.stat().st_mode & 0o777) == 0o644

    def test_output_is_read_back_through_the_same_fd(self, sb, tmp_path, monkeypatch):
        logs = tmp_path / "logs"

        def run(cmd, stdout=None, **kw):
            os_fd = stdout.fileno()
            import os
            os.write(os_fd, "✗ https://example.org/p0\n".encode())     # as node writes: straight to the fd
            (logs / "bookmarks-crawlee-1.log").unlink()                  # path replaced mid-run
            (logs / "bookmarks-crawlee-1.log").write_text("another run's log\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        monkeypatch.setattr(sb.subprocess, "run", run)
        rc, output = sb._run_crawlee_batch([("P", "https://example.org/p0")], 10, keep_log=("bookmarks-crawlee", 1))
        assert rc == 0 and output == "✗ https://example.org/p0\n"


class TestInternalAuthRouting:
    """2026-10-13 live run: 0 scraped / 127 skipped_auth with a valid Midway session.
    The completed Midway SSO round-trip (hops through idp.federate / midway-auth,
    then 200 back on the page) was judged ``auth``; hosts behind a non-Midway
    login (Microsoft, A to Z, JS-auth CloudFront) took ledger strikes every run."""

    # Hop sequences as observed live (query strings dropped).
    W_CHAIN = ["https://w.amazon.com/bin/view/WorkingBackwards/",
               "https://idp.federate.amazon.com/api/oauth2/v1/authorize",
               "https://midway-auth.amazon.com/SSO/redirect",
               "https://idp.federate.amazon.com:443/api/v1/intermediate",
               "https://w.amazon.com/"]
    BROADCAST_CHAIN = ["https://broadcast.amazon.com/channels/1",
                       "https://idp.federate.amazon.com/api/oauth2/v1/authorize",
                       "https://midway-auth.amazon.com/SSO/redirect",
                       "https://idp.federate.amazon.com:443/api/v1/intermediate",
                       "https://broadcast.amazon.com/", "https://broadcast.amazon.com/channels/1",
                       "https://idp.federate.amazon.com/api/oauth2/v1/authorize",
                       "https://midway-auth.amazon.com/SSO/redirect",
                       "https://idp.federate.amazon.com:443/api/v1/intermediate",
                       "https://broadcast.amazon.com:443/redirect"]

    @pytest.fixture
    def fetch(self, sb, monkeypatch, tmp_path):
        monkeypatch.setattr(sb, "KNOWLEDGE_DIR", str(tmp_path / "sources"))
        holder = {}
        monkeypatch.setitem(sys.modules, "requests", _fake_requests(lambda *a, **k: holder["resp"]))
        monkeypatch.setitem(sys.modules, "browser_cookie3", types.SimpleNamespace(chrome=lambda domain_name=None: None))

        def run(url, resp):
            holder["resp"] = resp
            return sb.scrape_with_cookies("Page", url)
        return run

    def test_completed_midway_round_trip_is_ok(self, fetch):
        assert fetch(self.W_CHAIN[0], _resp(url="https://w.amazon.com/bin/view/WorkingBackwards/",
                                            history=self.W_CHAIN)) == "ok"
        assert fetch(self.BROADCAST_CHAIN[0], _resp(url="https://broadcast.amazon.com:443/channels/1",
                                                    history=self.BROADCAST_CHAIN)) == "ok"

    @pytest.mark.parametrize("resp", [
        _resp(url="https://midway-auth.amazon.com/login?next=x", history=W_CHAIN[:3]),   # session refused
        _resp(status=403, url="https://w.amazon.com/bin/view/Restricted", history=W_CHAIN),
        _resp(status=403),                                    # bare refusal on a verified Midway host
    ])
    def test_sign_in_on_a_midway_host_is_auth(self, fetch, resp):
        assert fetch("https://w.amazon.com/bin/view/X", resp) == "auth"

    @pytest.mark.parametrize("url,resp", [
        ("https://atoz.amazon.work/home", _resp(url="https://atoz-login.amazon.work/",
                                                history=["https://atoz.amazon.work/home", "https://atoz-login.amazon.work/login"])),
        ("https://amazon.sharepoint.com/sites/X", _resp(url="https://login.microsoftonline.com:443/tenant/oauth2/authorize",
                                                        history=["https://amazon.sharepoint.com/sites/X"])),
        ("https://docs.hub.amazon.dev/kiro/", _resp(status=403, url="https://docs.hub.amazon.dev/kiro/")),
        ("https://awsinsights.aws.dev/x", _resp(status=403, url="https://awsinsights.aws.dev/x")),
    ])
    def test_sign_in_midway_cannot_satisfy_is_unsupported(self, fetch, url, resp):
        assert fetch(url, resp) == "unsupported_auth"

    @pytest.mark.parametrize("url,unsupported", [
        ("https://amazon.sharepoint.com/sites/X", True), ("https://amazon-my.sharepoint.com/x", True),
        ("https://atoz.amazon.work/home", True), ("https://atoz-login.amazon.work/", True),
        ("https://amazon.coupahost.com/x", True), ("https://docs.hub.amazon.dev/kiro/", True),
        ("https://w.amazon.com/bin/view/X", False), ("https://broadcast.amazon.com/x", False),
        ("https://evilsharepoint.com/x", False), ("https://docs.hub.amazon.dev.evil.io/", False),
        ("https://ivy-help-center.talent.a2z.com/x", False),
    ])
    def test_unsupported_hosts(self, sb, url, unsupported):
        assert sb.midway_unsupported(url) is unsupported

    def _main(self, sb, tmp_path, monkeypatch, urls, outcome_for, canary="auth"):
        bookmarks = tmp_path / "Bookmarks"
        bookmarks.write_text(json.dumps({"roots": {"bar": {"type": "folder", "children": [
            {"type": "url", "name": f"B{i}", "url": u} for i, u in enumerate(urls)]}}}))
        monkeypatch.setattr(sb, "BOOKMARKS_PATH", str(bookmarks))
        monkeypatch.setattr(sb, "MIDWAY_COOKIE_PATH", str(_midway_file(tmp_path / "midway")))
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set())
        calls = []
        monkeypatch.setattr(sb, "scrape_with_cookies", lambda name, url: calls.append(url) or outcome_for(url))
        self.canary_calls = []
        # fetch_internal is only reached by the canary here (scrape_with_cookies is faked).
        monkeypatch.setattr(sb, "fetch_internal",
                            lambda url: self.canary_calls.append(url) or (canary, None))
        return sb.main(), calls

    def _ledger(self, tmp_path):
        from src.capture.unreachable import Ledger
        return Ledger.load(tmp_path / "jobs" / "bookmarks.unreachable.json")

    def test_non_midway_hosts_are_not_fetched_and_take_no_strike(self, sb, tmp_path, monkeypatch, capsys):
        from src.capture.unreachable import url_key
        foreign = ["https://amazon.sharepoint.com/sites/A", "https://atoz.amazon.work/home",
                   "https://docs.hub.amazon.dev/kiro/"]
        ledger = self._ledger(tmp_path)
        for _ in range(2):                                    # today's state: 2 strikes, 1 more = cool-off
            ledger.record(foreign[0], "auth")
        ledger.save()
        wiki = "https://w.amazon.com/bin/view/Ok"
        rc, calls = self._main(sb, tmp_path, monkeypatch, [*foreign, wiki], lambda u: "ok")
        out = capsys.readouterr().out
        assert rc == 0 and calls == [wiki]
        assert "3 unsupported_auth (3 not fetched: non-Midway sign-in)" in out
        entries = self._ledger(tmp_path).entries
        assert entries[url_key(foreign[0])]["consecutive_failures"] == 2
        assert entries[url_key(foreign[0])]["skip_until"] is None
        assert all(url_key(u) not in entries for u in foreign[1:])

    def test_dynamic_unsupported_auth_takes_no_strike(self, sb, tmp_path, monkeypatch, capsys):
        urls = ["https://awsinsights.aws.dev/a", "https://w.amazon.com/bin/view/Ok"]
        rc, calls = self._main(sb, tmp_path, monkeypatch, urls,
                               lambda u: "unsupported_auth" if "awsinsights" in u else "ok")
        out = capsys.readouterr().out
        assert rc == 0 and calls == urls and "unsupported_auth=1" in out
        assert self._ledger(tmp_path).entries == {}

    def test_valid_session_reaching_no_page_defers_instead_of_striking(self, sb, tmp_path, monkeypatch, capsys):
        urls = [f"https://w.amazon.com/bin/view/P{i}" for i in range(4)]
        rc, _ = self._main(sb, tmp_path, monkeypatch, urls, lambda u: "auth")
        out = capsys.readouterr().out
        assert rc == 0
        assert "Midway session valid but all 4 Midway-path fetch(es) hit sign-in" in out
        assert "0 skipped_auth" in out and "deferred_auth=4" in out
        assert self._ledger(tmp_path).entries == {}

    def test_restricted_pages_among_reached_ones_still_strike(self, sb, tmp_path, monkeypatch, capsys):
        from src.capture.unreachable import url_key
        urls = [f"https://w.amazon.com/bin/view/P{i}" for i in range(4)]
        rc, _ = self._main(sb, tmp_path, monkeypatch, urls, lambda u: "ok" if u.endswith("P0") else "auth")
        out = capsys.readouterr().out
        assert rc == 0 and "1 scraped, 3 skipped_auth" in out
        entries = self._ledger(tmp_path).entries
        assert [entries[url_key(u)]["status"] for u in urls[1:]] == ["auth"] * 3

    # ------------------------------------------------ safety-net canary (review §3)

    def test_canary_reached_lets_the_strikes_through(self, sb, tmp_path, monkeypatch, capsys):
        """Only restricted pages left and the session works: per-URL refusals strike."""
        from src.capture.unreachable import url_key
        urls = [f"https://w.amazon.com/bin/view/P{i}" for i in range(4)]
        rc, _ = self._main(sb, tmp_path, monkeypatch, urls, lambda u: "auth", canary="ok")
        out = capsys.readouterr().out
        assert rc == 0 and self.canary_calls == [sb.CANARY_URL] == ["https://w.amazon.com/bin/view/Main/"]
        assert "canary page was reached" in out and "4 skipped_auth" in out and "deferred_auth=0" in out
        entries = self._ledger(tmp_path).entries
        assert [entries[url_key(u)]["status"] for u in urls] == ["auth"] * 4
        state = tmp_path / "jobs" / "bookmarks-midway.backlog.json"
        assert json.loads(state.read_text())["consecutive_deferred_runs"] == 0

    def test_canary_hitting_sign_in_defers_with_its_own_cause(self, sb, tmp_path, monkeypatch, capsys):
        urls = [f"https://w.amazon.com/bin/view/P{i}" for i in range(4)]
        rc, _ = self._main(sb, tmp_path, monkeypatch, urls, lambda u: "auth", canary="auth")
        out = capsys.readouterr().out
        assert rc == 0 and self.canary_calls == [sb.CANARY_URL]
        assert sb.MIDWAY_CAUSE_CANARY == "Midway session rejected at run time (canary page hit sign-in) — run mwinit"
        assert sb.MIDWAY_CAUSE_CANARY in out and "deferred_auth=4" in out
        assert "Midway cookie missing/expired" not in out                 # a separate WARNING text
        assert self._ledger(tmp_path).entries == {}
        state = json.loads((tmp_path / "jobs" / "bookmarks-midway.backlog.json").read_text())
        assert state["consecutive_deferred_runs"] == 1 and state["cause"] == sb.MIDWAY_CAUSE_CANARY
        for _ in range(2):
            self._main(sb, tmp_path, monkeypatch, urls, lambda u: "auth", canary="auth")
        out = capsys.readouterr().out
        assert f"bookmarks-midway backlog persists" in out and sb.MIDWAY_CAUSE_CANARY in out.split("backlog persists")[-1]

    def test_cookie_expiry_keeps_its_own_cause(self, sb, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sb, "midway_session_valid", lambda path=None: False)
        rc, calls = self._main(sb, tmp_path, monkeypatch, ["https://w.amazon.com/bin/view/P"], lambda u: "ok")
        out = capsys.readouterr().out
        assert rc == 0 and calls == [] and self.canary_calls == []
        assert "WARNING: Midway cookie missing/expired — run mwinit" in out and "canary" not in out
        state = json.loads((tmp_path / "jobs" / "bookmarks-midway.backlog.json").read_text())
        assert state["cause"] == sb.MIDWAY_CAUSE_COOKIE

    def test_canary_uses_fetch_internal_and_an_exception_is_inconclusive(self, sb):
        seen = []
        assert sb.canary_outcome(lambda url: seen.append(url) or ("ok", None)) == "ok" and seen == [sb.CANARY_URL]

        def boom(url):
            raise RuntimeError("no network")
        assert sb.canary_outcome(boom) == "failed"

    # ------------------------------------------------ safety-net boundary (review §6)

    def test_two_auth_with_no_page_reached_still_strike(self, sb, tmp_path, monkeypatch, capsys):
        from src.capture.unreachable import url_key
        urls = [f"https://w.amazon.com/bin/view/P{i}" for i in range(2)]
        rc, _ = self._main(sb, tmp_path, monkeypatch, urls, lambda u: "auth")
        out = capsys.readouterr().out
        assert rc == 0 and self.canary_calls == [] and "2 skipped_auth" in out and "deferred_auth=0" in out
        entries = self._ledger(tmp_path).entries
        assert [entries[url_key(u)]["status"] for u in urls] == ["auth"] * 2

    def test_safety_net_with_failures_present_defers_auth_and_keeps_failures(self, sb, tmp_path, monkeypatch, capsys):
        from src.capture.unreachable import url_key
        urls = [f"https://w.amazon.com/bin/view/P{i}" for i in range(4)]
        rc, _ = self._main(sb, tmp_path, monkeypatch, urls, lambda u: "failed" if u.endswith("P3") else "auth")
        out = capsys.readouterr().out
        assert rc == 0 and self.canary_calls == [sb.CANARY_URL]
        assert "0 skipped_auth, 1 failed" in out and "deferred_auth=3" in out
        assert {k: e["status"] for k, e in self._ledger(tmp_path).entries.items()} == {url_key(urls[3]): "error"}

    def test_eligible_excludes_skipped_unsupported(self, sb, tmp_path, monkeypatch, capsys):
        """Only pre-routed non-Midway bookmarks pending: nothing eligible, so nothing
        'eligible but not attempted' (exit 0)."""
        urls = ["https://amazon.sharepoint.com/sites/A", "https://atoz.amazon.work/home",
                "https://docs.hub.amazon.dev/kiro/"]
        rc, calls = self._main(sb, tmp_path, monkeypatch, urls, lambda u: "ok")
        out = capsys.readouterr().out
        assert rc == 0 and calls == []
        assert "eligible=0 attempted=0" in out and "-> exit 0" in out

class TestAuthJudging:
    """Review 2026-10-13 §1: path false positives, chains on unknown hosts, a 200 after a Midway hop."""

    W = "https://w.amazon.com/bin/view/X"
    HOPS = ["https://inside.amazon.com/x", "https://idp.federate.amazon.com/api/oauth2/v1/authorize",
            "https://midway-auth.amazon.com/SSO/redirect"]

    @pytest.fixture
    def fetch(self, sb, monkeypatch):
        holder = {}
        monkeypatch.setitem(sys.modules, "requests", _fake_requests(lambda *a, **k: holder["resp"]))
        monkeypatch.setitem(sys.modules, "browser_cookie3", types.SimpleNamespace(chrome=lambda domain_name=None: None))

        def run(url, resp):
            holder["resp"] = resp
            return sb.fetch_internal(url)[0]
        return run

    @pytest.mark.parametrize("path", ["/bin/view/Team/SSO/Setup", "/bin/view/Login", "/bin/view/Oncall/signin-help",
                                      "/bin/view/OAuth2/Guide", "/bin/view/X?authorize=1", "/bin/view/SAML/"])
    def test_sign_in_words_in_a_non_redirected_wiki_path_are_content(self, fetch, path):
        url = "https://w.amazon.com" + path
        assert fetch(url, _resp(url=url)) == "ok"

    def test_same_path_after_a_completed_midway_round_trip_is_content(self, fetch):
        url = "https://w.amazon.com/bin/view/Team/SSO/Setup"
        chain = [url, "https://idp.federate.amazon.com/api/oauth2/v1/authorize", "https://midway-auth.amazon.com/SSO/redirect"]
        assert fetch(url, _resp(url=url, history=chain)) == "ok"

    def test_landing_on_a_sign_in_host_is_still_auth(self, fetch):
        assert fetch(self.W, _resp(url="https://midway-auth.amazon.com/login?next=x", history=[self.W])) == "auth"
        assert fetch(self.W, _resp(url="https://w.amazon.com/login", history=[self.W])) == "ok"   # same host

    @pytest.mark.parametrize("url", ["https://idp.federate.amazon.com/api/oauth2/v1/authorize?client_id=x",
                                     "https://midway-auth.amazon.com/login", "https://login.microsoftonline.com/x",
                                     "https://atoz-login.amazon.work/"])
    def test_bookmarks_pointing_at_a_sign_in_host_are_pre_skipped(self, sb, url):
        assert sb.midway_unsupported(url) is True

    def test_expired_chain_on_an_unknown_host_is_judged_by_its_hops(self, fetch):
        """inside.amazon.com is not a verified Midway host; the chain through idp.federate /
        midway-auth ending on the Midway login makes it ``auth`` (a strike), not unsupported."""
        assert fetch(self.HOPS[0], _resp(url="https://midway-auth.amazon.com/login?next=x", history=self.HOPS)) == "auth"

    def test_bare_403_on_a_chain_capable_unknown_host_is_unsupported(self, fetch):
        url = "https://meetings.amazon.com/x"
        assert fetch(url, _resp(status=403, url=url)) == "unsupported_auth"          # no Midway hop: no strike
        chain = [url, "https://idp.federate.amazon.com/api/oauth2/v1/authorize", "https://midway-auth.amazon.com/SSO/redirect"]
        assert fetch(url, _resp(status=403, url=url, history=chain)) == "auth"        # refused after Midway: a strike

    @pytest.mark.parametrize("error", ["login_required", "interaction_required", "access_denied"])
    def test_oidc_error_return_after_a_midway_hop_is_auth(self, fetch, error):
        url = "https://inside.amazon.com/x"
        final = f"https://inside.amazon.com/callback?error={error}&state=s"
        assert fetch(url, _resp(url=final, history=self.HOPS)) == "auth"

    def test_oidc_error_without_a_midway_hop_or_with_another_error_is_not_auth(self, fetch):
        url = "https://inside.amazon.com/x"
        assert fetch(url, _resp(url=url + "?error=login_required")) == "ok"
        assert fetch(url, _resp(url=url + "?error=server_error", history=self.HOPS)) == "ok"

    @pytest.mark.parametrize("head", [
        '<form method="post" action="https://midway-auth.amazon.com/login">',
        "<html><head><title>Sign in</title></head>",
        "<html><head><title>Redirecting…</title></head>",
    ])
    def test_sign_in_page_body_on_the_original_host_is_auth(self, fetch, head):
        assert fetch(self.W, _resp(url=self.W, history=[self.W, *self.HOPS[1:]], text=head + "word " * 100)) == "auth"

    def test_body_markers_are_only_looked_for_in_the_first_20_kb(self, sb, fetch):
        late = "<p>" + "word " * 5000 + "</p>" + "<title>Sign in</title>"
        assert len(late) > sb.SIGN_IN_SNIFF_BYTES
        assert fetch(self.W, _resp(url=self.W, text=late)) == "ok"
        content = types.SimpleNamespace(status_code=200, url=self.W, history=[], text="(not read)",
                                        content=b"<title>Redirecting</title>" + b"x" * 100)
        assert sb._is_auth_response(content, self.W) is True                       # bytes path, sliced

    def test_atoz_hosts_are_narrowed_to_the_observed_ones(self, sb):
        for url in ("https://atoz.amazon.work/", "https://atoz-login.amazon.work/", "https://atoz.amazon.com/"):
            assert sb.midway_unsupported(url)
        for url in ("https://atoz-foo.amazon.com/", "https://atoz-bar.amazon.work/"):
            assert not sb.midway_unsupported(url)
