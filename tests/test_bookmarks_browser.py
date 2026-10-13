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


def eval_md(u, x, t="Page", f=0, s=None):
    state = {"u": u, "t": t, "x": x, "f": f} if s is None else {"u": u, "t": t, "x": x, "f": f, "s": s}
    return ("### Result\n" + json.dumps(json.dumps(state))
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
            self.navigated = [*getattr(self, "navigated", []), url]
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
            return eval_md(page.get("final", page.get("nav_url")), item, page.get("title", "Page"), page.get("f", 0),
                           page.get("status"))
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

    def test_short_shell_after_a_redirect_is_not_taken_as_the_page(self):
        # Live Sage 2026-10-13: after the redirect a ~289-char shell held still for ~2 s,
        # then the 2035-char page rendered. A 2 s window stored the shell.
        shell = "Sage navigation shell " * 13                     # 286 chars, over MIN_CHARS
        res = run([C], FakeBrowser({C: page(C, [self.DESTROYED] + [shell] * 7 + [LONG])}))[C]
        assert res.outcome == "ok" and res.text == LONG

    def test_route_change_restarts_the_stability_window(self):
        shell = "Sage navigation shell " * 13
        b = FakeBrowser({C: page(C, [shell] * 7 + [LONG])})
        urls = iter([C] * 2 + [C + "&t=7"] * 100)                # pushState after the 2nd poll

        real = b.call_text

        async def call_text(tool, args):
            reply = await real(tool, args)
            if tool == "browser_evaluate":
                state = bf.evaluate_result(reply)
                return eval_md(next(urls), state["x"])
            return reply
        b.call_text = call_text
        res = run([C], b)[C]
        assert res.outcome == "ok" and res.text == LONG

    def test_no_redirect_keeps_the_short_window(self):
        clock = FakeClock()
        res = run([A], FakeBrowser({A: page(A, [LONG])}), clock=clock)[A]
        assert res.outcome == "ok" and res.seconds < bf.STABLE_AFTER_REDIRECT_S

    def test_redirect_finished_before_the_first_evaluate_uses_the_long_window(self):
        # The navigate reply already shows the new URL (3xx, or a client-side redirect
        # that finished before the first evaluate): no URL change between evaluates.
        shell = "Sage navigation shell " * 13
        moved = "https://sage.amazon.dev/new/tags/AmazonQ?autoRedirectedFromLegacyUI=1"
        res = run([C], FakeBrowser({C: page(C, [shell] * 7 + [LONG], nav_url=moved, final=moved)}))[C]
        assert res.outcome == "ok" and res.text == LONG and res.final_url == moved

    @pytest.mark.parametrize("how", ["nav_url", "destroyed"])
    def test_redirect_then_text_that_never_settles_still_returns_at_the_cap(self, how):
        clock = FakeClock()
        moved = "https://sage.amazon.dev/new/tags/AmazonQ"
        states = [f"Counter {i} " + LONG for i in range(500)]     # a live counter: never stable
        if how == "nav_url":
            p = page(C, states, nav_url=moved, final=moved)
        else:
            p = page(C, [self.DESTROYED] + states)
        res = run([C], FakeBrowser({C: p}), clock=clock)[C]
        assert res.outcome == "ok"
        assert bf.HYDRATE_CAP_S <= res.seconds <= bf.HYDRATE_CAP_S + bf.POLL_S


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
        # A minimal env, not the job's (probe: HOME + PATH suffice for node/aim/curl).
        assert set(b.env) <= {"HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "LANG", "SHELL"}
        assert b.env["HOME"] == os.environ["HOME"] and b.env["PATH"] == os.environ["PATH"]
        assert b.env == bf.server_env(os.environ)
        assert bf.server_env({"HOME": "/h", "PATH": "/p", "AWS_PROFILE": "x", "SECOND_BRAIN_DB": "y",
                              "LANG": "C"}) == {"HOME": "/h", "PATH": "/p", "LANG": "C"}

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
        self.cookie_calls, self.browser_calls, self.budgets = [], [], []
        monkeypatch.setattr(sb, "scrape_with_cookies",
                            lambda name, url: self.cookie_calls.append(url) or cookie_outcome(url))

        def fake_browser(urls, budget_s):
            self.browser_calls.append(list(urls))
            self.budgets.append(budget_s)
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
        browser_backlog = json.loads((tmp_path / "jobs" / "bookmarks-browser.backlog.json").read_text())
        assert browser_backlog["deferred"] == 1 and browser_backlog["cause"].startswith("1 max_pages: ")
        public = json.loads((tmp_path / "jobs" / "bookmarks.backlog.json").read_text())
        assert public["deferred"] == 0 and "cause" not in public        # browser deferrals stay out of it

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


# --------------------------------------------------------------------------- #
# 2026-10-13 review fixes                                                     #
# --------------------------------------------------------------------------- #
EVIL_BACKSLASH = "https://evil.io\\@docs.hub.amazon.dev/"


class TestLaunchTeardown:
    def test_hanging_launch_is_torn_down_with_a_bounded_aexit(self, monkeypatch):
        monkeypatch.setattr(bf, "LAUNCH_TIMEOUT_S", 0.2)

        class Hangs(FakeBrowser):
            async def __aenter__(self):
                self.entered = True
                await asyncio.sleep(30)

        b = Hangs({})
        started = time.monotonic()
        out = asyncio.run(bf.fetch_pages([A, B], client_factory=b.factory, log=lambda *a: None))
        assert time.monotonic() - started < 5
        assert b.entered and b.exited                                      # teardown was called
        assert {r.outcome for r in out.values()} == {"deferred"} and out[A].detail == "launch"
        assert not os.path.exists(b.cwd)

    def test_failing_launch_is_torn_down(self):
        class Broken(FakeBrowser):
            async def __aenter__(self):
                raise McpTransportError("Connection closed")
        b = Broken({})
        out = run([A], b)
        assert b.exited and out[A].detail == "launch"


class TestHttpStatus:
    @pytest.mark.parametrize("status,outcome", [(404, "error"), (500, "error"), (403, "auth"), (401, "auth"),
                                                (407, "auth"), (200, "ok")])
    def test_status_classification(self, status, outcome):
        res = run([B], FakeBrowser({B: page(B, [LONG], status=status)}))[B]
        assert res.outcome == outcome and res.status == status
        assert res.detail == (f"http_{status}" if status >= 400 else "")

    def test_error_page_does_not_wait_for_hydration(self):
        res = run([B], FakeBrowser({B: page(B, ["Not found"], status=404)}))[B]
        assert res.outcome == "error" and res.seconds < 1

    def test_unknown_status_is_judged_on_text(self):
        res = run([B], FakeBrowser({B: page(B, [LONG], status=0)}))[B]
        assert res.outcome == "ok" and res.status is None

    def test_eval_fn_reads_the_navigation_status(self):
        assert "performance.getEntriesByType('navigation')[0]?.responseStatus" in bf.EVAL_FN


class TestLoading:
    def test_long_text_that_mentions_loading_is_content(self):
        text = LONG + " Loading docks and loading bays are covered in chapter 3."
        clock = FakeClock()
        res = run([A], FakeBrowser({A: page(A, [text])}), clock=clock)[A]
        assert res.outcome == "ok" and res.seconds < 5                     # not the 30 s cap

    @pytest.mark.parametrize("text,loading", [("Loading…", True), ("Loading " + LONG, True),
                                              (LONG + " Loading", False), ("x" * 250 + " Loading", True)])
    def test_still_loading(self, text, loading):
        assert bf.still_loading(text) is loading


class TestInvalidUrl:
    def test_backslash_url_is_never_navigated(self):
        b = FakeBrowser({B: page(B, [LONG])})
        out = run([EVIL_BACKSLASH, B], b)
        assert out[EVIL_BACKSLASH].outcome == "invalid_url" and out[B].outcome == "ok"
        assert b.navigated == [B]

    def test_navigates_the_reserialized_url(self):
        mixed = "https://DOCS.hub.amazon.dev/getting-started/"
        b = FakeBrowser({B: page(B, [LONG])})
        out = run([mixed], b)
        assert b.navigated == [B] and out[mixed].outcome == "ok" and out[mixed].requested_url == B

    @pytest.mark.parametrize("url", [EVIL_BACKSLASH, "https://user@docs.hub.amazon.dev/",
                                     "https://docs.hub.amazon.dev%2eevil.io/", "https://docs.hub.amazon.dev./",
                                     "https://0x7f000001/", "https://d\u00f6cs.amazon.dev/", "javascript:alert(1)",
                                     "https://docs.hub.amazon.dev/\tx", "https://docs.hub.amazon.dev:99999/"])
    def test_canonical_url_rejects_parser_differentials(self, url):
        from src.capture.filters import canonical_url
        assert canonical_url(url) is None

    def test_canonical_url_keeps_ordinary_urls(self):
        from src.capture.filters import canonical_url
        for url in (A, B, C, "https://sage.amazon.dev/tags/AmazonQ?t=7&autoRedirectedFromLegacyUI=1",
                    "https://docs.hub.amazon.dev/kiro/user-guide/howto-mcp/#buildermcp-read-internal-websites"):
            assert canonical_url(url) == url


class TestReviewRouting:
    """scrape_bookmarks.main with the cookie and browser paths faked (TestRouting._main)."""

    def _main(self, sb, tmp_path, monkeypatch, urls, cookie_outcome, browser_result=None, budgets=None,
              session_left=None):
        t = TestRouting()
        if session_left is not None:
            values, last = iter(session_left), [None]

            def left(path=None):
                last[0] = next(values, last[0])
                return last[0]
            monkeypatch.setattr(sb, "midway_session_seconds_left", left)
        rc = t._main(sb, tmp_path, monkeypatch, urls, cookie_outcome, browser_result=browser_result)
        if budgets is not None:
            budgets.extend(t.budgets)
        return rc, t

    @staticmethod
    def _ledger(tmp_path):
        return json.loads((tmp_path / "jobs" / "bookmarks.unreachable.json").read_text())["urls"]

    def test_backslash_bookmark_is_skipped_invalid_url_without_fetch_or_strike(self, sb, tmp_path, monkeypatch,
                                                                              capsys):
        rc, t = self._main(sb, tmp_path, monkeypatch, [EVIL_BACKSLASH, W], lambda u: "ok")
        out = capsys.readouterr().out
        assert rc == 0 and t.cookie_calls == [W] and t.browser_calls == []
        assert "skipped_invalid_url=1" in out and "eligible=1" in out
        assert self._ledger(tmp_path) == {}

    @pytest.mark.parametrize("url", ["https://aws.highspot.com/items/1", "https://w.amazon.com.evil.io/x",
                                     "https://quip-amazon.com/abc", "https://amazon-mentoring.chronus.com/p"])
    def test_fallback_only_for_amazon_owned_suffixes(self, sb, tmp_path, monkeypatch, capsys, url):
        assert not sb.browser_fallback_allowed(url)
        rc, t = self._main(sb, tmp_path, monkeypatch, [url], lambda u: "unsupported_auth")
        out = capsys.readouterr().out
        assert t.cookie_calls == [url] and t.browser_calls == []           # never reaches the Midway browser
        assert "unsupported_auth=1" in out

    def test_fallback_allowlist(self, sb):
        for url in (UNSUP, TLS, "https://gather.a2z.com/x", "https://command-center.support.aws.a2z.com/",
                    "https://inside.hr.amazon.dev/", "https://studio.us-east-1.prod.workshops.aws/w"):
            assert sb.browser_fallback_allowed(url), url
        for url in ("https://evilamazon.com/", "https://amazon.com.evil.io/", "https://amazon.awsapps.com/start",
                    SHAREPOINT, "https://aws.highspot.com/"):
            assert not sb.browser_fallback_allowed(url), url
        from src.capture.filters import INTERNAL_DOMAINS
        assert INTERNAL_DOMAINS.search("https://w.amazon.com.evil.io/")    # shared pattern left unchanged

    @pytest.mark.parametrize("final,status", [("https://login.example-idp.com/authorize?x=1", None),
                                              (None, 403)])
    def test_fallback_that_ends_on_a_non_midway_login_is_unsupported_auth(self, sb, tmp_path, monkeypatch,
                                                                          capsys, final, status):
        def browser(u):
            if status:
                return bf.PageResult(u, "auth", final_url=u, status=status, detail=f"http_{status}")
            return bf.PageResult(u, "ok", text=LONG, final_url=final, title="Welcome")
        rc, t = self._main(sb, tmp_path, monkeypatch, [UNSUP], lambda u: "unsupported_auth", browser_result=browser)
        out = capsys.readouterr().out
        assert t.browser_calls == [[UNSUP]]
        assert "unsupported_auth=1" in out and "browser_auth=0" in out
        assert self._ledger(tmp_path) == {}                                 # no strike
        state = json.loads((tmp_path / "jobs" / "bookmarks-unsupported.backlog.json").read_text())
        assert state["deferred"] == 1 and "foo.corp.amazon.com" in state["cause"]

    def test_fallback_that_ends_on_midway_sign_in_is_auth(self, sb, tmp_path, monkeypatch, capsys):
        browser = lambda u: bf.PageResult(u, "auth", final_url="https://midway-auth.amazon.com/login")
        self._main(sb, tmp_path, monkeypatch, [UNSUP, W], lambda u: "unsupported_auth" if u == UNSUP else "ok",
                   browser_result=browser)
        from src.capture.unreachable import url_key
        assert self._ledger(tmp_path)[url_key(UNSUP)]["status"] == "auth"

    def test_same_host_login_path_is_auth(self, sb):
        res = bf.PageResult(B, "ok", text=LONG, final_url="https://docs.hub.amazon.dev/login?next=/x",
                            title="Docs", requested_url=B)
        assert sb.browser_outcome(B, res) == "auth"
        same = bf.PageResult(B, "ok", text=LONG, final_url=B + "#top", title="Docs", requested_url=B)
        assert sb.browser_outcome(B, same) == "ok"
        moved = bf.PageResult(C, "ok", text=LONG, final_url="https://sage.amazon.dev/new/tags/AmazonQ",
                              title="Sage", requested_url=C)
        assert sb.browser_outcome(C, moved) == "ok"                       # moved, but not a sign-in URL

    def test_http_statuses_reach_the_ledger(self, sb, tmp_path, monkeypatch):
        results = {A: bf.PageResult(A, "error", status=404, detail="http_404", final_url=A),
                   B: bf.PageResult(B, "auth", status=403, detail="http_403", final_url=B)}
        self._main(sb, tmp_path, monkeypatch, [A, B, W], lambda u: "ok", browser_result=results.get)
        from src.capture.unreachable import url_key
        ledger = self._ledger(tmp_path)
        assert ledger[url_key(A)]["status"] == "error" and ledger[url_key(B)]["status"] == "auth"

    def test_browser_only_systemic_auth_is_deferred_auth_despite_a_cookie_ok(self, sb, tmp_path, monkeypatch,
                                                                            capsys):
        browser = lambda u: bf.PageResult(u, "auth", final_url="https://midway-auth.amazon.com/login")
        rc, t = self._main(sb, tmp_path, monkeypatch, [A, B, C, W], lambda u: "ok", browser_result=browser)
        out = capsys.readouterr().out
        assert rc == 0 and t.cookie_calls == [W]
        assert "deferred_auth=3" in out and "browser_auth=0" in out and "auth=0" in out
        assert self._ledger(tmp_path) == {}                                 # no strikes
        midway = json.loads((tmp_path / "jobs" / "bookmarks-midway.backlog.json").read_text())
        assert midway["deferred"] == 3 and midway["cause"] == sb.MIDWAY_CAUSE_BROWSER
        assert midway["cause"] == "browser sign-in failed for every page (Midway mint) — run mwinit"

    def test_two_browser_auths_are_per_url_strikes(self, sb, tmp_path, monkeypatch):
        browser = lambda u: bf.PageResult(u, "auth", final_url="https://midway-auth.amazon.com/login")
        self._main(sb, tmp_path, monkeypatch, [A, B, W], lambda u: "ok", browser_result=browser)
        assert len(self._ledger(tmp_path)) == 2

    @pytest.mark.parametrize("detail", ["launch", "server_down", "budget", "max_pages"])
    def test_browser_deferrals_get_their_own_backlog_with_a_cause_per_detail(self, sb, tmp_path, monkeypatch,
                                                                            capsys, detail):
        browser = lambda u: bf.PageResult(u, "deferred", detail=detail)
        rc, _ = self._main(sb, tmp_path, monkeypatch, [A, B, W], lambda u: "ok", browser_result=browser)
        out = capsys.readouterr().out
        state = json.loads((tmp_path / "jobs" / "bookmarks-browser.backlog.json").read_text())
        assert state["deferred"] == 2 and state["consecutive_deferred_runs"] == 1
        assert state["cause"] == f"2 {detail}: {sb.BROWSER_DEFER_CAUSES[detail]}"
        assert "WARNING: bookmarks-browser backlog persists" in out         # threshold 1
        public = json.loads((tmp_path / "jobs" / "bookmarks.backlog.json").read_text())
        assert public["deferred"] == 0 and "cause" not in public
        assert self._ledger(tmp_path) == {}

    def test_a_clean_browser_run_resets_the_browser_backlog(self, sb, tmp_path, monkeypatch):
        from src.operations import backlog
        backlog.record_run("bookmarks-browser", 4, tmp_path / "jobs", cause="4 launch: x")
        self._main(sb, tmp_path, monkeypatch, [A, W], lambda u: "ok")
        assert backlog.read_state("bookmarks-browser", tmp_path / "jobs")["consecutive_deferred_runs"] == 0

    def test_browser_budget_is_capped_by_the_session_left(self, sb, tmp_path, monkeypatch):
        budgets = []
        self._main(sb, tmp_path, monkeypatch, [A, W], lambda u: "ok", budgets=budgets, session_left=[2000.0, 2000.0])
        assert budgets == [pytest.approx(2000.0 - 600.0)]

    def test_session_running_out_before_the_browser_pass_defers_it(self, sb, tmp_path, monkeypatch, capsys):
        # Preflight sees 40 min; by the browser pass (after the cookie path) only 10.5 min are left.
        rc, t = self._main(sb, tmp_path, monkeypatch, [A, B, UNSUP, W],
                           lambda u: "unsupported_auth" if u == UNSUP else "ok", session_left=[2400.0, 630.0])
        out = capsys.readouterr().out
        assert rc == 0 and t.browser_calls == [] and t.cookie_calls == [UNSUP, W]
        assert "deferred_auth=3" in out                                       # A, B and the UNSUP fallback
        assert self._ledger(tmp_path) == {}
        midway = json.loads((tmp_path / "jobs" / "bookmarks-midway.backlog.json").read_text())
        assert midway["deferred"] == 3 and midway["cause"] == sb.MIDWAY_CAUSE_LATE
        assert not (tmp_path / "jobs" / "bookmarks-browser.backlog.json").exists()

    def test_job_deadline_too_short_for_the_browser_pass_defers_it_with_its_own_cause(self, sb, tmp_path,
                                                                                     monkeypatch, capsys):
        monkeypatch.setattr(sb, "browser_budget_s", lambda started: 30.0)
        rc, t = self._main(sb, tmp_path, monkeypatch, [A, B, W], lambda u: "ok", session_left=[7200.0, 7200.0])
        out = capsys.readouterr().out
        assert t.browser_calls == [] and "deferred_auth=2" in out and "job deadline" in out
        assert self._ledger(tmp_path) == {}
        midway = json.loads((tmp_path / "jobs" / "bookmarks-midway.backlog.json").read_text())
        assert midway["deferred"] == 2 and midway["cause"] == sb.MIDWAY_CAUSE_DEADLINE


# --------------------------------------------------------------------------- #
# A real stdio MCP server (tests/_stub_browser_mcp.py): no orphans             #
# --------------------------------------------------------------------------- #
from tests import _stub_browser_mcp as stub  # noqa: E402


def _stub_factory(script, mode, directory):
    from src.capture.mcp_client import McpClient

    def factory(cwd, env):
        return McpClient(stub.PYTHON, [str(script), mode], call_timeout=20, env={**env, **stub.stub_env(directory)},
                         cwd=cwd)
    return factory


class TestRealStdioServer:
    def test_normal_exit_leaves_no_process(self, tmp_path):
        script = stub.write_stub(tmp_path)
        missing = "https://docs.hub.amazon.dev/missing"
        out = asyncio.run(bf.fetch_pages([B, missing], client_factory=_stub_factory(script, "ok", tmp_path),
                                         log=lambda *a: None))
        assert out[B].outcome == "ok" and out[B].status == 200 and out[B].chars > 200
        assert out[missing].outcome == "error" and out[missing].status == 404
        pids = stub.stub_pids(tmp_path)
        assert stub.wait_gone(pids) == []                  # server and its /bin/sleep "Chromium" both gone

    def test_launch_timeout_leaves_no_process(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bf, "LAUNCH_TIMEOUT_S", 3.0)
        script = stub.write_stub(tmp_path)
        started = time.monotonic()
        out = asyncio.run(bf.fetch_pages([B], client_factory=_stub_factory(script, "hang", tmp_path),
                                         log=lambda *a: None))
        assert out[B].outcome == "deferred" and out[B].detail == "launch"
        assert time.monotonic() - started < 3.0 + 15
        assert stub.wait_gone(stub.stub_pids(tmp_path)) == []

    def test_sigterm_during_the_pass_leaves_no_process(self, tmp_path):
        import signal
        import subprocess
        import sys
        script = stub.write_stub(tmp_path)
        runner = tmp_path / "run_pass.py"
        runner.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(pathlib.Path.cwd())!r})\n"
            "from src.capture import browser_fetch as bf\n"
            "from src.capture.mcp_client import McpClient\n"
            f"script, d = {str(script)!r}, {str(tmp_path)!r}\n"
            "env = {'STUB_DIR': d}\n"
            "f = lambda cwd, e: McpClient(sys.executable, [script, 'slow'], call_timeout=600, env={**e, **env}, cwd=cwd)\n"
            "bf.fetch_pages_sync(['https://docs.hub.amazon.dev/x'], client_factory=f, page_timeout=600)\n"
            "print('pass returned')\n")
        proc = subprocess.Popen([sys.executable, str(runner)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        try:
            stub.wait_for_file(tmp_path / "navigating", timeout=30)
            pids = stub.stub_pids(tmp_path)
            assert all(stub.alive(p) for p in pids)
            proc.send_signal(signal.SIGTERM)                   # the job only: the server is in its own session
            stdout, _ = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)
        assert proc.returncode == 143 and "pass returned" not in stdout
        assert stub.wait_gone(pids) == []
