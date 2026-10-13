"""Hermetic tests for the bookmarks browser path (src/capture/browser_fetch.py and its
routing in scripts/scrape_bookmarks.py).

No browser, no MCP server, no network: a fake client stands in for the
midway-playwright session and answers in the server's real markdown shapes
(captured 2026-10-13 from midway-playwright-mcp --caps core).
"""
import asyncio
import importlib.util
import json
import os
import pathlib
import tempfile
import time

import pytest

from src.capture import browser_fetch as bf
from src.capture.mcp_client import McpToolError, McpTransportError

LONG = "Data Access and Permissions " * 20          # 560 chars
FUTURE = 4102444800


def nav_md(url, title="Page"):
    return (f"### Ran Playwright code\n```js\nawait page.goto('{url}');\n```\n### Page\n"
            f"- Page URL: {url}\n- Page Title: {title}\n### Snapshot\n- [Snapshot](.playwright-mcp/page.yml)")


def eval_md(u, x, t="Page", f=0):
    return ("### Result\n" + json.dumps(json.dumps({"u": u, "t": t, "x": x, "f": f}))
            + "\n### Ran Playwright code\n```js\nawait page.evaluate('...');\n```")


def snap_md(lines):
    return "### Page\n- Page URL: https://x/\n- Page Title: T\n### Snapshot\n```yaml\n" + "\n".join(lines) + "\n```"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.t += seconds


class FakeBrowser:
    """One scripted page per URL: ``evals`` is the sequence of innerText states (the
    last repeats); an entry that is an exception is raised instead."""

    def __init__(self, pages, clock=None, hang=(), nav_cost=0.0):
        self.pages = pages
        self.clock = clock
        self.hang = set(hang)
        self.nav_cost = nav_cost
        self.calls = []
        self.current = None
        self.entered = self.exited = False
        self.cwd = self.env = None

    def factory(self, cwd, env):
        self.cwd, self.env = cwd, env
        os.makedirs(os.path.join(cwd, ".playwright-mcp"))          # what the real server does
        pathlib.Path(cwd, ".playwright-mcp", "page.yml").write_text("page text")
        self.cwd_existed = os.path.isdir(cwd)
        return self

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True

    async def call_text(self, tool, args):
        self.calls.append(tool)
        if tool == "browser_navigate":
            url = args["url"]
            if url in self.hang:
                await asyncio.sleep(30)                             # real time: only the page cap stops it
            if self.clock is not None:
                self.clock.t += self.nav_cost
            page = self.pages[url]
            if isinstance(page.get("nav_error"), BaseException):
                raise page["nav_error"]
            self.current = page
            page.setdefault("i", 0)
            return nav_md(page.get("nav_url", url), page.get("title", "Page"))
        if tool == "browser_evaluate":
            page = self.current
            evals = page["evals"]
            item = evals[min(page["i"], len(evals) - 1)]
            page["i"] += 1
            if isinstance(item, BaseException):
                raise item
            return eval_md(page.get("final", page.get("nav_url")), item, page.get("title", "Page"), page.get("f", 0))
        if tool == "browser_snapshot":
            return snap_md(self.current.get("snapshot", []))
        if tool == "browser_close":
            return "### Result\nclosed"
        raise AssertionError(tool)


def run(urls, browser, clock=None, **kw):
    clock = clock or FakeClock()
    return asyncio.run(bf.fetch_pages(urls, client_factory=browser.factory, clock=clock,
                                      sleep=clock.sleep, log=lambda *a: None, **kw))


A = "https://help.awsentral.aws.dev/article-detail?id=kA05w0000011pDLCAY&source=awsentral"
B = "https://docs.hub.amazon.dev/getting-started/"
C = "https://sage.amazon.dev/tags/AmazonQ"


def page(url, evals, **kw):
    return {"nav_url": url, "final": url, "evals": evals, **kw}


# --------------------------------------------------------------------------- #
# browser_fetch                                                               #
# --------------------------------------------------------------------------- #
class TestHydration:
    def test_waits_for_long_stable_text_without_loading(self):
        b = FakeBrowser({A: page(A, ["Submit a Bug", "Loading... " + LONG, LONG + "x", LONG])})
        res = run([A], b)[A]
        assert res.outcome == "ok" and res.text == LONG and res.via == "innerText"
        # 4 changing states, then unchanged for >= 2 s at 0.5 s polls before it stops.
        assert b.calls.count("browser_evaluate") >= 4 + 4
        assert b.calls[0] == "browser_navigate" and b.calls[-1] == "browser_close"

    def test_hydration_wait_is_capped_at_30s(self):
        clock = FakeClock()
        states = [f"Loading {i} " + LONG for i in range(500)]     # never settles
        res = run([A], FakeBrowser({A: page(A, states)}), clock=clock)[A]
        assert res.outcome == "ok" and 30 <= res.seconds <= 31

    def test_parsers_read_the_real_reply_shapes(self):
        assert bf.page_state(nav_md(A, "Article Detail")) == (A, "Article Detail")
        assert bf.evaluate_result(eval_md(A, "hi", f=2)) == {"u": A, "t": "Page", "x": "hi", "f": 2}
        with pytest.raises(McpToolError):
            bf.evaluate_result("### Error\nboom")


class TestContextDestroyed:
    DESTROYED = McpToolError("browser_evaluate: ### Error\nError: page.evaluate: Execution context was "
                             "destroyed, most likely because of a navigation")

    def test_retries_once_after_a_client_side_redirect(self):
        res = run([C], FakeBrowser({C: page(C, [self.DESTROYED, LONG])}))[C]
        assert res.outcome == "ok" and res.chars == len(LONG)

    def test_a_second_destroyed_context_is_an_error_not_a_loop(self):
        res = run([C], FakeBrowser({C: page(C, [self.DESTROYED, self.DESTROYED, LONG])}))[C]
        assert res.outcome == "error" and res.detail == "tool"


class TestIframeFallback:
    def test_short_inner_text_with_iframes_uses_the_snapshot_text(self):
        url = "https://awsinsights.aws.dev/chapter/x"
        snapshot = ['- navigation [ref=e1]:', '  - link "Home" [ref=e2] [cursor=pointer]:', '    - /url: /home',
                    '- iframe [ref=e3]:', *[f'  - text: QuickSight metric row {i} value {i * 7}' for i in range(80)]]
        b = FakeBrowser({url: page(url, ["Chapter dashboard"], f=2, snapshot=snapshot)})
        res = run([url], b)[url]
        assert res.outcome == "ok" and res.via == "snapshot" and "browser_snapshot" in b.calls
        assert "QuickSight metric row 79 value 553" in res.text and "Home" in res.text
        assert "/url" not in res.text and "[ref=" not in res.text

    def test_no_iframes_means_no_snapshot(self):
        b = FakeBrowser({A: page(A, [LONG])})
        run([A], b)
        assert "browser_snapshot" not in b.calls


class TestClassification:
    @pytest.mark.parametrize("final", ["https://midway-auth.amazon.com/login?next=x",
                                       "https://sentry.amazon.com/SSO/redirect"])
    def test_final_host_midway_or_sentry_is_auth(self, final):
        res = run([B], FakeBrowser({B: page(B, [LONG], nav_url=final, final=final)}))[B]
        assert res.outcome == "auth"

    def test_landing_on_sign_in_after_hydration_is_auth(self):
        final = "https://midway-auth.amazon.com/login"
        res = run([B], FakeBrowser({B: page(B, [LONG], final=final)}))[B]
        assert res.outcome == "auth"

    def test_short_text_is_too_short(self):
        res = run([A], FakeBrowser({A: page(A, ["tiny"])}))[A]
        assert res.outcome == "too_short" and res.chars == 4


class TestErrorsNeverHang:
    def test_hung_tool_hits_the_page_cap_and_the_run_continues(self):
        b = FakeBrowser({A: page(A, [LONG]), B: page(B, [LONG])}, hang=[A])
        started = time.monotonic()
        out = asyncio.run(bf.fetch_pages([A, B], client_factory=b.factory, page_timeout=0.3,
                                         sleep=FakeClock().sleep, log=lambda *a: None))
        assert time.monotonic() - started < 5
        assert out[A].outcome == "error" and out[A].detail == "timeout" and out[B].outcome == "ok"
        assert b.exited

    def test_tool_error_is_that_pages_error(self):
        b = FakeBrowser({A: page(A, [LONG], nav_error=McpToolError("browser_navigate: net::ERR_NAME")),
                         B: page(B, [LONG])})
        out = run([A, B], b)
        assert out[A].outcome == "error" and out[A].detail == "tool" and out[B].outcome == "ok"

    def test_dead_server_defers_the_rest(self):
        b = FakeBrowser({A: page(A, [LONG], nav_error=McpTransportError("reconnect failed")),
                         B: page(B, [LONG])})
        out = run([A, B], b)
        assert out[A].outcome == "error" and out[B].outcome == "deferred" and out[B].detail == "server_down"

    def test_launch_failure_defers_everything(self):
        class Broken(FakeBrowser):
            async def __aenter__(self):
                raise McpToolError("Connection closed")
        out = run([A, B], Broken({}))
        assert {r.outcome for r in out.values()} == {"deferred"} and out[A].detail == "launch"


class TestBudget:
    def test_max_pages_defers_the_rest(self):
        b = FakeBrowser({u: page(u, [LONG]) for u in (A, B, C)})
        out = run([A, B, C], b, max_pages=2)
        assert [out[u].outcome for u in (A, B, C)] == ["ok", "ok", "deferred"] and out[C].detail == "max_pages"

    def test_run_budget_defers_the_rest(self):
        clock = FakeClock()
        b = FakeBrowser({u: page(u, [LONG]) for u in (A, B, C)}, clock=clock, nav_cost=20)
        out = run([A, B, C], b, clock=clock, budget_s=50)
        assert out[A].outcome == "ok" and out[B].outcome == "ok"
        assert out[C].outcome == "deferred" and out[C].detail == "budget"
        assert b.calls.count("browser_navigate") == 2


class TestScratchCwd:
    def test_server_runs_in_a_private_temp_dir_that_is_removed(self):
        b = FakeBrowser({A: page(A, [LONG])})
        run([A], b)
        assert b.cwd_existed and b.cwd.startswith(tempfile.gettempdir())
        assert pathlib.Path(b.cwd).name.startswith("sb-browser-")
        assert not os.path.exists(b.cwd)                                  # .playwright-mcp dumps gone with it
        assert not pathlib.Path(b.cwd).is_relative_to(pathlib.Path.cwd())
        assert b.env == dict(os.environ)                                   # the job's own env

    def test_default_client_is_the_midway_playwright_server(self, tmp_path):
        client = bf._default_client(str(tmp_path), {"PATH": "/usr/bin"})
        assert client._params.command.endswith("midway-playwright-mcp")
        assert client._params.args == ["--caps", "core"] and str(client._params.cwd) == str(tmp_path)


# --------------------------------------------------------------------------- #
# scrape_bookmarks routing                                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture
def sb(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scrape_bookmarks", pathlib.Path("scripts/scrape_bookmarks.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "CRAWLEE_DIR", str(tmp_path))
    monkeypatch.setattr(mod, "KNOWLEDGE_DIR", str(tmp_path / "sources"))
    monkeypatch.setattr(mod, "load_indexed_urls", lambda: set())
    monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("SECOND_BRAIN_LOG_DIR", str(tmp_path / "logs"))
    return mod


def _cookie(path, expires=FUTURE):
    path.write_text(f"#HttpOnly_midway-auth.amazon.com\tFALSE\t/\tTRUE\t{int(expires)}\tsession\tv\n")
    return path


W = "https://w.amazon.com/bin/view/Team/"
BROADCAST = "https://broadcast.amazon.com/videos/1"
SHAREPOINT = "https://amazon.sharepoint.com/sites/A"
PERSONAL = "https://help.awsentral.aws.dev/?tab=cases"
UNSUP = "https://foo.corp.amazon.com/x"
TLS = "https://bar.corp.amazon.com/y"


class TestRouting:
    def _main(self, sb, tmp_path, monkeypatch, urls, cookie_outcome, browser_result=None, expires=FUTURE):
        bookmarks = tmp_path / "Bookmarks"
        bookmarks.write_text(json.dumps({"roots": {"bar": {"type": "folder", "children": [
            {"type": "url", "name": f"B{i}", "url": u} for i, u in enumerate(urls)]}}}))
        monkeypatch.setattr(sb, "BOOKMARKS_PATH", str(bookmarks))
        monkeypatch.setattr(sb, "MIDWAY_COOKIE_PATH", str(_cookie(tmp_path / "midway", expires)))
        self.cookie_calls, self.browser_calls = [], []
        monkeypatch.setattr(sb, "scrape_with_cookies",
                            lambda name, url: self.cookie_calls.append(url) or cookie_outcome(url))

        def fake_browser(urls, budget_s):
            self.browser_calls.append(list(urls))
            return {u: (browser_result or (lambda u: bf.PageResult(u, "ok", text=LONG, final_url=u,
                                                                   title="T", via="innerText")))(u) for u in urls}
        monkeypatch.setattr(sb, "browser_fetch_pages", fake_browser)
        return sb.main()

    def test_hosts(self, sb):
        for url in (A, B, C, "https://awsinsights.aws.dev/x", "https://gather.a2z.com/e/1",
                    "https://collaborate-corp.amazon.com/x", "https://hyperbadge.amazon.dev/faqs/"):
            assert sb.browser_host(url), url
        for url in (W, BROADCAST, SHAREPOINT, "https://docs.hub.amazon.dev.evil.io/", "https://evilamazon.dev/",
                    "https://notaws.dev/", "https://console.harmony.a2z.com/x", "https://ivy-help-center.talent.a2z.com/"):
            assert not sb.browser_host(url), url
        assert not sb.midway_unsupported(B) and sb.midway_unsupported(SHAREPOINT)

    def test_routing_fallback_and_storage(self, sb, tmp_path, monkeypatch, capsys):
        urls = [A, B, W, BROADCAST, SHAREPOINT, PERSONAL, UNSUP, TLS]
        outcome = {W: "ok", BROADCAST: "ok", UNSUP: "unsupported_auth", TLS: "tls_error"}
        rc = self._main(sb, tmp_path, monkeypatch, urls, lambda u: outcome[u])
        out = capsys.readouterr().out
        assert rc == 0
        assert self.cookie_calls == [W, BROADCAST, UNSUP, TLS]             # w.amazon / broadcast stay on cookies
        assert self.browser_calls == [[A, B, UNSUP, TLS]]                  # one session: hosts + same-run fallbacks
        assert "browser_ok=4 browser_auth=0 browser_error=0 browser_deferred=0 skipped_personal=1" in out
        assert "unsupported_auth=0" in out and "(1 not fetched: non-Midway sign-in)" in out
        stored = {}
        for f in (tmp_path / "sources").glob("*.md"):
            text = f.read_text()
            source = next(line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("Source: "))
            stored[source] = text
        assert set(stored) == {A, B, UNSUP, TLS}                           # cookie path is faked here
        assert all("Type: web-page" in t and "\nDate: " in t and "fetched_via: browser" in t and LONG in t
                   for t in stored.values())
        ledger = json.loads((tmp_path / "jobs" / "bookmarks.unreachable.json").read_text())
        assert ledger["urls"] == {}

    def test_cookie_path_storage_has_no_fetched_via(self, sb, tmp_path):
        name = sb.save_page("Page", W, LONG)
        text = (tmp_path / "sources" / name).read_text()
        assert text.startswith("# Page\n\nSource: " + W) and "fetched_via" not in text

    def test_browser_outcomes_reach_the_ledger_and_summary(self, sb, tmp_path, monkeypatch, capsys):
        results = {A: bf.PageResult(A, "auth", final_url="https://midway-auth.amazon.com/login"),
                   B: bf.PageResult(B, "error", detail="timeout"),
                   C: bf.PageResult(C, "deferred", detail="max_pages")}
        rc = self._main(sb, tmp_path, monkeypatch, [A, B, C, W], lambda u: "ok", browser_result=results.get)
        out = capsys.readouterr().out
        assert rc == 0
        assert "browser_ok=0 browser_auth=1 browser_error=1 browser_deferred=1" in out
        from src.capture.unreachable import Ledger, url_key
        entries = Ledger.load(tmp_path / "jobs" / "bookmarks.unreachable.json").entries
        assert entries[url_key(A)]["status"] == "auth" and entries[url_key(B)]["status"] == "timeout"
        assert url_key(C) not in entries                                    # deferred: no strike
        backlog = json.loads((tmp_path / "jobs" / "bookmarks.backlog.json").read_text())
        assert backlog["deferred"] == 1 and "browser" in backlog["cause"]

    def test_sign_in_title_or_host_from_the_cookie_markers_is_auth(self, sb):
        ok = dict(outcome="ok", text=LONG, title="Docs")
        assert sb.browser_outcome(B, bf.PageResult(B, final_url=B, **ok)) == "ok"
        assert sb.browser_outcome(B, bf.PageResult(B, final_url="https://idp.federate.amazon.com/x", **ok)) == "auth"
        assert sb.browser_outcome(B, bf.PageResult(B, **{**ok, "title": "Sign in"}, final_url=B)) == "auth"
        assert sb.browser_outcome(B, bf.PageResult(B, "error", detail="tool")) == "error"


class TestPersonalPages:
    @pytest.mark.parametrize("url,personal", [
        ("https://help.awsentral.aws.dev/", True), ("https://help.awsentral.aws.dev", True),
        ("https://help.awsentral.aws.dev/?tab=cases", True), (A, False),
        ("https://evil.io/?u=https://help.awsentral.aws.dev/", False), ("https://other.aws.dev/", False),
    ])
    def test_personal_list(self, sb, url, personal):
        assert sb.personal_page(url) is personal

    def test_personal_page_is_skipped_without_fetch_or_strike(self, sb, tmp_path, monkeypatch, capsys):
        rc = TestRouting()._main(sb, tmp_path, monkeypatch, [PERSONAL], lambda u: "ok")
        out = capsys.readouterr().out
        assert rc == 0 and "skipped_personal=1" in out and "eligible=0" in out
        assert json.loads((tmp_path / "jobs" / "bookmarks.unreachable.json").read_text())["urls"] == {}


class TestBrowserPreflight:
    @pytest.mark.parametrize("left", [600, 1700])
    def test_short_session_defers_browser_urls_without_launching(self, sb, tmp_path, monkeypatch, capsys, left):
        launched = []
        monkeypatch.setattr(bf, "_default_client", lambda cwd, env: launched.append(cwd))
        t = TestRouting()
        rc = t._main(sb, tmp_path, monkeypatch, [A, B, W, UNSUP],
                     lambda u: "unsupported_auth" if u == UNSUP else "ok", expires=time.time() + left)
        out = capsys.readouterr().out
        assert rc == 0 and t.browser_calls == [] and launched == []
        assert t.cookie_calls == [W, UNSUP]                                 # cookie path still runs, no fallback
        assert "Midway session expires in under 30 min" in out and "deferred_auth=2" in out
        assert "unsupported_auth=1" in out
        assert json.loads((tmp_path / "jobs" / "bookmarks-midway.backlog.json").read_text())["deferred"] == 2
        assert json.loads((tmp_path / "jobs" / "bookmarks.unreachable.json").read_text())["urls"] == {}

    def test_missing_cookie_defers_everything_without_launching(self, sb, tmp_path, monkeypatch, capsys):
        t = TestRouting()
        t._main(sb, tmp_path, monkeypatch, [A, W], lambda u: "ok")
        (tmp_path / "midway").unlink()
        t.cookie_calls.clear()
        t.browser_calls.clear()
        rc = sb.main()
        assert rc == 0 and t.browser_calls == [] and t.cookie_calls == []
        assert "deferred_auth=2" in capsys.readouterr().out

    def test_session_seconds_left(self, sb, tmp_path):
        assert sb.midway_session_seconds_left(str(tmp_path / "absent")) is None
        left = sb.midway_session_seconds_left(str(_cookie(tmp_path / "c", time.time() + 900)))
        assert 890 < left <= 900


class TestTlsClassification:
    def test_ssl_error_is_tls_error(self, sb, monkeypatch):
        class SSLError(OSError):
            pass

        def boom(url):
            raise SSLError("CERTIFICATE_VERIFY_FAILED")
        monkeypatch.setattr(sb, "fetch_internal", boom)
        assert sb.scrape_with_cookies("P", A) == "tls_error"

        def other(url):
            raise ValueError("x")
        monkeypatch.setattr(sb, "fetch_internal", other)
        assert sb.scrape_with_cookies("P", A) == "failed"
