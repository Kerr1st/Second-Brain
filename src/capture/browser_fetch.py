"""Fetch internal pages that plain ``requests`` cannot reach through midway-playwright.

Some internal sites (docs.hub, sage, awsentral, awsinsights, ...) sign in through
JavaScript, sit behind CloudFront auth, or fail TLS for ``requests``; a real
browser with the Midway cookie jar reaches them headlessly (probe 2026-10-13,
second-brain-triage/2026-10-13/mwpw-probe.md). This module drives the internal
``midway-playwright-mcp`` server (``--caps core``) over one :class:`McpClient`
session per run and returns one :class:`PageResult` per URL; the bookmarks job
maps those onto its existing outcome / ledger / backlog machinery.

* The server writes console logs and page snapshots (which may hold page text)
  into ``<cwd>/.playwright-mcp/``, so it runs in a private ``mkdtemp`` that is
  removed afterwards.
* Per page: ``browser_navigate``, then ``browser_evaluate`` polled until the
  ``main``/``article``/``body`` innerText is > 200 chars, unchanged for 2 s and
  free of "Loading" (30 s cap). One retry on "Execution context was destroyed"
  (Sage redirects client-side after load). Short innerText on a page with
  iframes (awsinsights: cross-origin QuickSight frames) falls back to the
  ``browser_snapshot`` text.
* Bounds: ``PAGE_TIMEOUT_S`` per page, ``MAX_PAGES`` per run, and an overall
  budget; URLs beyond either are ``deferred`` (not attempted, no strike). A
  tool error is that page's ``error``; a dead server defers the remaining URLs.
* Nothing here logs page text (counts only) or cookies.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.capture.mcp_client import McpAuthError, McpClient, McpToolError, McpTransportError, classify_error

#: Found like slack.py / outlook.py find their servers; overridable for tests and other hosts.
BROWSER_MCP_CMD = os.environ.get("MIDWAY_PLAYWRIGHT_MCP",
                                 os.path.expanduser("~/.aim/mcp-servers/midway-playwright-mcp"))
BROWSER_MCP_ARGS = ["--caps", "core"]
PAGE_TIMEOUT_S = float(os.environ.get("BROWSER_PAGE_TIMEOUT_S", "45"))
MAX_PAGES = int(os.environ.get("BROWSER_MAX_PAGES", "60"))
RUN_BUDGET_S = float(os.environ.get("BROWSER_RUN_BUDGET_S", "1500"))
CALL_TIMEOUT_S = float(os.environ.get("BROWSER_CALL_TIMEOUT_S", "40"))
LAUNCH_TIMEOUT_S = 60.0     # server start + initialize
CLOSE_TIMEOUT_S = 15.0
HYDRATE_CAP_S = 30.0
STABLE_S = 2.0
POLL_S = 0.5
MIN_CHARS = 200
#: innerText shorter than this on a page with iframes is replaced by the snapshot text when that is longer.
IFRAME_FALLBACK_CHARS = 2000
#: Do not start a page with less than this much run budget left.
MIN_PAGE_START_S = 10.0

#: A navigation that ends here never reached the page: Midway sign-in.
AUTH_FINAL_HOSTS = re.compile(r"^(midway-auth|sentry)\.amazon\.com$", re.I)
CONTEXT_DESTROYED = "execution context was destroyed"
EVAL_FN = ("() => JSON.stringify({u: location.href, t: document.title, "
           "x: ((document.querySelector('main,article')||document.body||{}).innerText)||'', "
           "f: document.querySelectorAll('iframe').length})")


@dataclass
class PageResult:
    url: str
    outcome: str            # "ok" | "auth" | "too_short" | "error" | "deferred"
    text: str = ""
    final_url: str = ""
    title: str = ""
    seconds: float = 0.0
    detail: str = ""        # error: timeout/transport/tool; deferred: budget/max_pages/launch/server_down
    via: str = ""           # "innerText" | "snapshot"

    @property
    def chars(self) -> int:
        return len(self.text)


def _host(url: str) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def is_auth_host(url: str) -> bool:
    return bool(AUTH_FINAL_HOSTS.match(_host(url)))


def classify(final_url: str, text: str) -> str:
    if is_auth_host(final_url):
        return "auth"
    return "too_short" if len(text) < MIN_CHARS else "ok"


_STATE_RE = {k: re.compile(rf"^- Page {k}: (.*)$", re.M) for k in ("URL", "Title")}
_RESULT_RE = re.compile(r"### Result\n(.*?)(?=\n### |\Z)", re.S)


def page_state(markdown: str) -> tuple[str, str]:
    """(URL, title) from a playwright reply's ``### Page`` section."""
    found = [(m.group(1).strip() if (m := rx.search(markdown or "")) else "") for rx in _STATE_RE.values()]
    return found[0], found[1]


def evaluate_result(markdown: str) -> dict:
    """The object ``EVAL_FN`` returned (JSON-encoded twice: our stringify + the server's)."""
    m = _RESULT_RE.search(markdown or "")
    if not m:
        raise McpToolError("browser_evaluate: no ### Result in reply")
    value = m.group(1).strip()
    try:
        for _ in range(3):
            if isinstance(value, dict):
                return value
            value = json.loads(value)
    except (ValueError, TypeError) as e:
        raise McpToolError(f"browser_evaluate: unparseable result ({type(e).__name__})") from e
    if isinstance(value, dict):
        return value
    raise McpToolError("browser_evaluate: result is not an object")


_ATTR_RE = re.compile(r"\s*\[[^\]]*\]")
_SNAP_LINE_RE = re.compile(r'^([\w-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*:?\s*(.*)$')


def snapshot_text(markdown: str) -> str:
    """Visible text of a ``browser_snapshot`` aria tree (names and text nodes; no refs or URLs)."""
    blocks = re.findall(r"```yaml\n(.*?)```", markdown or "", re.S)
    out: list[str] = []
    for line in "\n".join(blocks).splitlines():
        s = line.strip()
        if s.startswith("- "):
            s = s[2:]
        if not s or s.startswith("/"):
            continue                                    # /url:, /placeholder: ...
        m = _SNAP_LINE_RE.match(_ATTR_RE.sub("", s))
        if not m:
            continue
        for piece in (m.group(2), m.group(3)):
            piece = (piece or "").strip().strip('"')
            if piece and (not out or out[-1] != piece):
                out.append(piece)
    return "\n".join(out)


async def _hydrate(client, clock, sleep) -> dict:
    """Poll ``EVAL_FN`` until the text is settled (see module docstring); the last state at the cap."""
    deadline = clock() + HYDRATE_CAP_S
    retried = False
    last, since, state = None, clock(), {}
    while True:
        try:
            state = evaluate_result(await client.call_text("browser_evaluate", {"function": EVAL_FN}))
        except McpToolError as e:
            if CONTEXT_DESTROYED in str(e).lower() and not retried:
                retried = True                          # client-side redirect: evaluate the new page
                await sleep(POLL_S)
                continue
            raise
        text, now = str(state.get("x") or ""), clock()
        if is_auth_host(str(state.get("u") or "")):
            return state
        if text != last:
            last, since = text, now
        elif now - since >= STABLE_S and "Loading" not in text and (len(text) > MIN_CHARS or state.get("f")):
            return state
        if now >= deadline:
            return state
        await sleep(POLL_S)


async def _fetch_one(client, url, clock, sleep) -> PageResult:
    nav = await client.call_text("browser_navigate", {"url": url})
    final_url, title = page_state(nav)
    if is_auth_host(final_url):
        return PageResult(url, "auth", final_url=final_url, title=title)
    state = await _hydrate(client, clock, sleep)
    final_url = str(state.get("u") or final_url)
    title = str(state.get("t") or title)
    text, via = str(state.get("x") or ""), "innerText"
    if state.get("f") and len(text) < IFRAME_FALLBACK_CHARS and not is_auth_host(final_url):
        snap = snapshot_text(await client.call_text("browser_snapshot", {}))
        if len(snap) > len(text):
            text, via = snap, "snapshot"
    return PageResult(url, classify(final_url, text), text=text, final_url=final_url, title=title, via=via)


def _default_client(cwd: str, env: dict):
    return McpClient(BROWSER_MCP_CMD, BROWSER_MCP_ARGS, call_timeout=CALL_TIMEOUT_S, env=env, cwd=cwd)


async def fetch_pages(urls, *, page_timeout: float | None = None, max_pages: int | None = None,
                      budget_s: float | None = None, client_factory=None, env: dict | None = None,
                      clock=time.monotonic, sleep=asyncio.sleep, log=print) -> dict[str, PageResult]:
    """Fetch ``urls`` in one browser session; returns url -> :class:`PageResult` for every URL.

    ``client_factory(cwd, env)`` returns an (unentered) async context manager with
    ``call_text``; the default starts ``BROWSER_MCP_CMD`` with ``env`` (default:
    this process's env) in a private temp cwd that is always removed. Never
    raises for a page or the server: a launch failure or a dead server defers
    the URLs it left unattempted."""
    urls = list(dict.fromkeys(urls))
    results: dict[str, PageResult] = {}
    if not urls:
        return results
    page_timeout = PAGE_TIMEOUT_S if page_timeout is None else page_timeout
    max_pages = MAX_PAGES if max_pages is None else max_pages
    budget_s = RUN_BUDGET_S if budget_s is None else budget_s
    start = clock()
    scratch = tempfile.mkdtemp(prefix="sb-browser-")
    client_cm = (client_factory or _default_client)(scratch, dict(os.environ) if env is None else dict(env))

    def defer(rest, reason):
        for u in rest:
            results.setdefault(u, PageResult(u, "deferred", detail=reason))

    try:
        try:
            async with asyncio.timeout(LAUNCH_TIMEOUT_S):
                client = await client_cm.__aenter__()
        except Exception as e:  # noqa: BLE001 - a browser that will not start defers, never crashes the job
            log(f"WARNING: browser path unavailable ({type(e).__name__}: {classify_error(e)}); "
                f"{len(urls)} URL(s) deferred")
            defer(urls, "launch")
            return results
        try:
            for i, url in enumerate(urls):
                remaining = budget_s - (clock() - start)
                if i >= max_pages or remaining < MIN_PAGE_START_S:
                    reason = "max_pages" if i >= max_pages else "budget"
                    log(f"Browser: {reason} reached ({i} page(s), {clock() - start:.0f}s of {budget_s:.0f}s); "
                        f"{len(urls) - i} URL(s) deferred to the next run")
                    defer(urls[i:], reason)
                    break
                t0 = clock()
                dead = False
                try:
                    async with asyncio.timeout(min(page_timeout, remaining)):
                        res = await _fetch_one(client, url, clock, sleep)
                except TimeoutError:
                    res = PageResult(url, "error", detail="timeout")
                except McpAuthError:
                    res = PageResult(url, "auth", detail="server")     # e.g. "Missing ~/.midway/cookie"
                except McpTransportError:
                    res, dead = PageResult(url, "error", detail="transport"), True
                except Exception as e:  # noqa: BLE001 - one page never aborts the run
                    kind = classify_error(e)
                    res = PageResult(url, "error", detail="timeout" if kind == "transport" else "tool")
                res.seconds = clock() - t0
                results[url] = res
                log(f"  [browser {i + 1}/{len(urls)}] {_host(url)}: {res.outcome}"
                    f"{f' ({res.detail})' if res.detail else ''} chars={res.chars} s={res.seconds:.1f}")
                if dead:
                    log(f"WARNING: browser server died; {len(urls) - i - 1} URL(s) deferred")
                    defer(urls[i + 1:], "server_down")
                    break
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_S):
                    await client.call_text("browser_close", {})
            except Exception:  # noqa: BLE001 - best-effort close; teardown kills the server anyway
                pass
        finally:
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_S):
                    await client_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - teardown noise must not lose the results
                pass
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        defer(urls, "budget")       # never reached (cannot happen normally): still accounted for
    return results


def fetch_pages_sync(urls, **kwargs) -> dict[str, PageResult]:
    return asyncio.run(fetch_pages(urls, **kwargs))
