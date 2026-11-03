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
  ``main``/``article``/``body`` innerText is > 200 chars, unchanged for 2 s (5 s
  after a redirect: the navigate reply's URL differs from the requested one, a
  destroyed context or a route change) and free of "Loading" (30 s cap). One
  retry on "Execution context was destroyed"
  (Sage redirects client-side after load). Short innerText on a page with
  iframes (awsinsights: cross-origin QuickSight frames) falls back to the
  ``browser_snapshot`` text.
* Bounds: ``PAGE_TIMEOUT_S`` per page, ``MAX_PAGES`` per run, and an overall
  budget; URLs beyond either are ``deferred`` (not attempted, no strike). A
  tool error is that page's ``error``; a dead server defers the remaining URLs.
* HTTP status: ``EVAL_FN`` reads the navigation entry's ``responseStatus``;
  401/403/407 is ``auth``, any other status >= 400 is ``error`` (``detail``
  ``http_<status>``). A final URL on a known sign-in page (``is_sign_in_page``:
  Midway, A to Z / idprism, idp.federate, accelerate /portal/signin) is ``auth``
  whatever its length.
* URLs are navigated only in their urllib re-serialized form
  (``filters.canonical_url``); one Python and Chromium could read differently
  (backslash, userinfo, odd host) is ``invalid_url`` and never navigated.
* Sign-in hops: before the first page on a ``SIGN_IN_HOPS`` host the session
  navigates that host's Midway sign-in link once (its sign-in is a button, not a
  redirect); a failed hop is only logged (the page then comes back as sign-in).
  A budget the hop used up defers the rest (``budget``), never a near-zero page timeout.
* Teardown: a launch that fails or exceeds ``LAUNCH_TIMEOUT_S`` is torn down
  with a bounded ``__aexit__`` too. The SDK runs the server in its own session
  (``start_new_session=True``), out of reach of the job wrapper's killpg, so the
  server's pgid (``McpClient.server_pids``) is recorded and its whole group is
  SIGTERM→SIGKILLed after the pass (Chromium children included) and from an
  ``atexit`` hook; :func:`fetch_pages_sync` turns SIGTERM into ``SystemExit``
  for the pass so those ``finally`` blocks run.
* The server gets a minimal env (``SERVER_ENV_KEYS``: HOME, PATH, USER,
  LOGNAME, TMPDIR, LANG, SHELL — probe §1: HOME+PATH suffice).
* Nothing here logs page text (counts only) or cookies.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import shutil
import signal
import tempfile
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.capture.filters import canonical_url
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
#: After a client-side redirect (context destroyed or a route change) the text must be
#: unchanged this long: Sage's new UI renders a ~300-char shell for ~2 s first (2026-10-13).
STABLE_AFTER_REDIRECT_S = 5.0
POLL_S = 0.5
MIN_CHARS = 200
#: Text containing "Loading" counts as still hydrating only when it is this short
#: (or starts with "Loading"): a long article that mentions loading is content.
LOADING_MAX_CHARS = 300
#: The only variables the server (node, Chromium) inherits from the job.
SERVER_ENV_KEYS = ("HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "LANG", "SHELL")
#: Recorded server process groups get SIGTERM, then SIGKILL after this long.
REAP_GRACE_S = 2.0
#: innerText shorter than this on a page with iframes is replaced by the snapshot text when that is longer.
IFRAME_FALLBACK_CHARS = 2000
#: Do not start a page with less than this much run budget left.
MIN_PAGE_START_S = 10.0

#: Hosts whose Midway sign-in is a *link* on their own sign-in page rather than an
#: automatic redirect, so the browser lands on "Sign in with Midway" (too_short /
#: auth). Navigating the link's target once per session completes SSO; the
#: session's later pages on that host render. Exact hosts -> hop URL. Probe
#: 2026-10-15 (second-brain-triage/2026-10-15/reading-route-probe.md):
#: apg-library.amazonaws.com redirects to accelerate.amazonaws.com/portal/signin,
#: whose Midway button is /authenticate?appId=mpl&identityProvider=AmazonCorporate.
SIGN_IN_HOPS = {
    "apg-library.amazonaws.com":
        "https://accelerate.amazonaws.com/authenticate?appId=mpl&identityProvider=AmazonCorporate",
}

#: A navigation that ends here never reached the page: Midway sign-in.
AUTH_FINAL_HOSTS = re.compile(r"^(midway-auth|sentry)\.amazon\.com$", re.I)
#: Other sign-in pages a navigation can end on (exact hosts; a login form there is
#: 245-505 chars, so ``MIN_CHARS`` alone would call it "ok" and index the login text —
#: second-brain-triage/2026-10-15/bookmarks-gaps-probe.md §3). Not Midway's own sign-in:
#: ``is_auth_host`` (the session-problem test) stays Midway-only.
SIGN_IN_FINAL_HOSTS = re.compile(
    r"^(atoz-login\.amazon\.work|idp\.federate\.amazon\.com|[a-z0-9-]+\.idprism-auth\.amazon\.com)$", re.I)
#: Sign-in pages identified by exact host + path (the host also serves real pages).
SIGN_IN_FINAL_PATHS = {("accelerate.amazonaws.com", "/portal/signin")}
#: HTTP statuses of the page itself that mean "refused for auth" (as on the cookie path).
AUTH_STATUS = {401, 403, 407}
CONTEXT_DESTROYED = "execution context was destroyed"
EVAL_FN = ("() => JSON.stringify({u: location.href, t: document.title, "
           "x: ((document.querySelector('main,article')||document.body||{}).innerText)||'', "
           "f: document.querySelectorAll('iframe').length, "
           "s: performance.getEntriesByType('navigation')[0]?.responseStatus})")


@dataclass
class PageResult:
    url: str
    outcome: str            # "ok" | "auth" | "too_short" | "error" | "deferred" | "invalid_url"
    text: str = ""
    final_url: str = ""
    title: str = ""
    seconds: float = 0.0
    detail: str = ""        # error: timeout/transport/tool/http_<n>; auth: server/http_<n>;
                            # deferred: budget/max_pages/launch/server_down
    via: str = ""           # "innerText" | "snapshot"
    status: int | None = None   # the page's HTTP status (navigation entry), when the browser knew it
    requested_url: str = ""     # what was navigated: the canonical form of ``url``

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


def is_sign_in_page(url: str) -> bool:
    """A final URL that is a sign-in page: Midway's (``is_auth_host``) or another
    known one (``SIGN_IN_FINAL_HOSTS`` / ``SIGN_IN_FINAL_PATHS``). Host/path only,
    never a text heuristic (a real article mentioning "log in" stays ok)."""
    host = _host(url)
    if not host:
        return False
    if AUTH_FINAL_HOSTS.match(host) or SIGN_IN_FINAL_HOSTS.match(host):
        return True
    try:
        path = (urlsplit(url).path or "/").rstrip("/").lower()
    except ValueError:
        return False
    return (host, path) in SIGN_IN_FINAL_PATHS


def sign_in_hop(url: str) -> str | None:
    """The ``SIGN_IN_HOPS`` URL to navigate once per session before ``url``'s host, if any."""
    return SIGN_IN_HOPS.get(_host(url))


def classify(final_url: str, text: str, status: int | None = None) -> str:
    if is_sign_in_page(final_url) or status in AUTH_STATUS:
        return "auth"
    if status is not None and status >= 400:
        return "error"
    return "too_short" if len(text) < MIN_CHARS else "ok"


def still_loading(text: str) -> bool:
    """A short ("Loading…" placeholder) or leading "Loading" text is still hydrating."""
    return "Loading" in text and (len(text) < LOADING_MAX_CHARS or text.lstrip().startswith("Loading"))


def _status(state: dict) -> int | None:
    try:
        value = int(state.get("s") or 0)
    except (TypeError, ValueError):
        return None
    return value or None             # 0: unknown (cached / cross-origin redirect)


def server_env(env=None) -> dict:
    """``SERVER_ENV_KEYS`` from ``env`` (default: this process's env)."""
    env = os.environ if env is None else env
    return {k: env[k] for k in SERVER_ENV_KEYS if env.get(k)}


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


async def _hydrate(client, clock, sleep, nav_url: str = "", requested: str = "") -> dict:
    """Poll ``EVAL_FN`` until the text is settled (see module docstring); the last state at the cap.

    ``nav_url`` is the navigate reply's final URL: when it differs from ``requested``
    the page already redirected (server 3xx, or a client-side redirect that finished
    before the first evaluate), so the longer post-redirect window applies."""
    deadline = clock() + HYDRATE_CAP_S
    retried = redirected = False
    last, last_url, since, state = None, None, clock(), {}
    if nav_url and requested and nav_url != requested:
        last_url, redirected = nav_url, True
    while True:
        try:
            state = evaluate_result(await client.call_text("browser_evaluate", {"function": EVAL_FN}))
        except McpToolError as e:
            if CONTEXT_DESTROYED in str(e).lower() and not retried:
                retried = redirected = True             # client-side redirect: evaluate the new page
                last, since = None, clock()             # the old page's stability does not count
                await sleep(POLL_S)
                continue
            raise
        text, now, url = str(state.get("x") or ""), clock(), str(state.get("u") or "")
        if is_auth_host(url) or (_status(state) or 0) >= 400:
            return state                                # sign-in / error page: nothing to wait for
        if last_url is not None and url != last_url:
            redirected = True                           # SPA route change (pushState): restart the window
            last = None
        last_url = url
        # A redirecting SPA (Sage's legacy -> new UI) shows a short shell for ~2 s before
        # the content renders; after a redirect the text must hold still for longer.
        stable_for = STABLE_AFTER_REDIRECT_S if redirected else STABLE_S
        if text != last:
            last, since = text, now
        elif now - since >= stable_for and not still_loading(text) and (len(text) > MIN_CHARS or state.get("f")):
            return state
        if now >= deadline:
            return state
        await sleep(POLL_S)


async def _fetch_one(client, url, clock, sleep, target=None) -> PageResult:
    target = target or url
    nav = await client.call_text("browser_navigate", {"url": target})
    final_url, title = page_state(nav)
    if is_auth_host(final_url):
        return PageResult(url, "auth", final_url=final_url, title=title, requested_url=target)
    state = await _hydrate(client, clock, sleep, nav_url=final_url, requested=target)
    final_url = str(state.get("u") or final_url)
    title = str(state.get("t") or title)
    status = _status(state)
    text, via = str(state.get("x") or ""), "innerText"
    if state.get("f") and len(text) < IFRAME_FALLBACK_CHARS and not is_auth_host(final_url) \
            and (status or 0) < 400:
        snap = snapshot_text(await client.call_text("browser_snapshot", {}))
        if len(snap) > len(text):
            text, via = snap, "snapshot"
    outcome = classify(final_url, text, status)
    detail = f"http_{status}" if status is not None and status >= 400 else ""
    return PageResult(url, outcome, text=text, final_url=final_url, title=title, via=via, status=status,
                      detail=detail, requested_url=target)


# --------------------------------------------------------------------------- #
# Server process groups                                                        #
# --------------------------------------------------------------------------- #
#: pgids of servers started by this process and not yet confirmed gone.
_LIVE_PGIDS: set[int] = set()
_atexit_installed = False


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:          # not ours (pid reused by another user): leave it alone
        return False


def reap_groups(pgids=None, grace_s: float = REAP_GRACE_S) -> list[int]:
    """SIGTERM, then (after ``grace_s``) SIGKILL every recorded server process group
    that still has members; returns the pgids that had to be signalled. Never raises."""
    targets = sorted(set(_LIVE_PGIDS if pgids is None else pgids))
    signalled = []
    for pgid in targets:
        if pgid <= 1 or pgid == os.getpgrp():
            _LIVE_PGIDS.discard(pgid)
            continue
        if _group_alive(pgid):
            signalled.append(pgid)
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                pass
    deadline = time.monotonic() + grace_s
    for pgid in signalled:
        while _group_alive(pgid) and time.monotonic() < deadline:
            _reap_zombie(pgid)
            time.sleep(0.05)
        if _group_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
    for pgid in targets:
        _reap_zombie(pgid)
        if not _group_alive(pgid):
            _LIVE_PGIDS.discard(pgid)
    return signalled


def _reap_zombie(pid: int) -> None:
    """Collect the server's exit status when it is our (zombie) child."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _track(pids) -> None:
    global _atexit_installed
    _LIVE_PGIDS.update(int(p) for p in pids or ())
    if _LIVE_PGIDS and not _atexit_installed:
        atexit.register(reap_groups)
        _atexit_installed = True


async def _bounded_aexit(client_cm) -> None:
    try:
        async with asyncio.timeout(CLOSE_TIMEOUT_S):
            await client_cm.__aexit__(None, None, None)
    except SystemExit:
        raise    # SIGTERM (_SigtermExit) ends the job; the callers' finally blocks clean up and reap
    except BaseException:  # noqa: BLE001 - teardown noise must not lose the results; reap_groups follows
        pass


def _default_client(cwd: str, env: dict):
    return McpClient(BROWSER_MCP_CMD, BROWSER_MCP_ARGS, call_timeout=CALL_TIMEOUT_S, env=env, cwd=cwd)


async def fetch_pages(urls, *, page_timeout: float | None = None, max_pages: int | None = None,
                      budget_s: float | None = None, client_factory=None, env: dict | None = None,
                      clock=time.monotonic, sleep=asyncio.sleep, log=print) -> dict[str, PageResult]:
    """Fetch ``urls`` in one browser session; returns url -> :class:`PageResult` for every URL.

    ``client_factory(cwd, env)`` returns an (unentered) async context manager with
    ``call_text`` (and, for :class:`McpClient`, ``server_pids``); the default
    starts ``BROWSER_MCP_CMD`` with ``env`` (default: :func:`server_env`) in a
    private temp cwd that is always removed. Never raises for a page or the
    server: a launch failure or a dead server defers the URLs it left
    unattempted, and the server's process group is reaped either way."""
    urls = list(dict.fromkeys(urls))
    results: dict[str, PageResult] = {}
    if not urls:
        return results
    page_timeout = PAGE_TIMEOUT_S if page_timeout is None else page_timeout
    max_pages = MAX_PAGES if max_pages is None else max_pages
    budget_s = RUN_BUDGET_S if budget_s is None else budget_s
    start = clock()
    scratch = tempfile.mkdtemp(prefix="sb-browser-")
    client_cm = (client_factory or _default_client)(scratch, server_env() if env is None else dict(env))

    def defer(rest, reason):
        for u in rest:
            results.setdefault(u, PageResult(u, "deferred", detail=reason))

    try:
        try:
            try:
                async with asyncio.timeout(LAUNCH_TIMEOUT_S):
                    client = await client_cm.__aenter__()
            except Exception as e:  # noqa: BLE001 - a browser that will not start defers, never crashes the job
                # A half-entered client (process + task groups) must not outlive the job.
                _track(getattr(client_cm, "server_pids", None))
                await _bounded_aexit(client_cm)
                log(f"WARNING: browser path unavailable ({type(e).__name__}: {classify_error(e)}); "
                    f"{len(urls)} URL(s) deferred")
                defer(urls, "launch")
                return results
            _track(getattr(client_cm, "server_pids", None))
            hopped: set[str] = set()
            try:
                for i, url in enumerate(urls):
                    remaining = budget_s - (clock() - start)
                    if i >= max_pages or remaining < MIN_PAGE_START_S:
                        reason = "max_pages" if i >= max_pages else "budget"
                        log(f"Browser: {reason} reached ({i} page(s), {clock() - start:.0f}s of {budget_s:.0f}s); "
                            f"{len(urls) - i} URL(s) deferred to the next run")
                        defer(urls[i:], reason)
                        break
                    target = canonical_url(url)
                    if target is None:
                        results[url] = PageResult(url, "invalid_url")      # never navigated
                        log(f"  [browser {i + 1}/{len(urls)}] {_host(url) or '?'}: invalid_url")
                        continue
                    hop = sign_in_hop(target)
                    if hop and hop not in hopped:
                        hopped.add(hop)            # once per session, whatever happens
                        try:
                            async with asyncio.timeout(min(page_timeout, remaining)):
                                hop_nav = await client.call_text("browser_navigate", {"url": hop})
                            log(f"  [browser] sign-in hop {_host(hop)} -> {_host(page_state(hop_nav)[0]) or '?'}")
                        except Exception as e:  # noqa: BLE001 - the page itself then shows the sign-in (auth)
                            log(f"  [browser] sign-in hop {_host(hop)} failed ({type(e).__name__})")
                        remaining = budget_s - (clock() - start)
                        if remaining < MIN_PAGE_START_S:
                            # The hop used up the budget: defer (no strike), never a near-zero page timeout.
                            log(f"Browser: budget reached after the sign-in hop ({clock() - start:.0f}s of "
                                f"{budget_s:.0f}s); {len(urls) - i} URL(s) deferred to the next run")
                            defer(urls[i:], "budget")
                            break
                    t0 = clock()
                    dead = False
                    try:
                        async with asyncio.timeout(min(page_timeout, remaining)):
                            res = await _fetch_one(client, url, clock, sleep, target)
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
                    _track(getattr(client_cm, "server_pids", None))           # a reconnect spawns a new group
                    log(f"  [browser {i + 1}/{len(urls)}] {_host(url)}: {res.outcome}"
                        f"{f' ({res.detail})' if res.detail else ''} status={res.status or '-'} "
                        f"chars={res.chars} s={res.seconds:.1f}")
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
                await _bounded_aexit(client_cm)
        finally:
            # Its own finally, ahead of the reap: a SIGTERM during the reap (or a
            # teardown that re-raised one) still removes the .playwright-mcp dumps.
            shutil.rmtree(scratch, ignore_errors=True)
    finally:
        try:
            pids = getattr(client_cm, "server_pids", None)
            if pids:
                _track(pids)
                reap_groups(pids)        # Chromium etc. left in the server's group after a clean exit
        finally:
            defer(urls, "budget")       # never reached (cannot happen normally): still accounted for
    return results


class _SigtermExit(SystemExit):
    """SIGTERM during the browser pass, raised so every ``finally`` (teardown, reap) runs."""


def _install_sigterm():
    """SIGTERM -> ``SystemExit(143)`` while the pass runs (main thread only); returns the restorer."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous = signal.getsignal(signal.SIGTERM)

    def on_sigterm(signum, frame):
        raise _SigtermExit(128 + signal.SIGTERM)
    signal.signal(signal.SIGTERM, on_sigterm)
    return lambda: signal.signal(signal.SIGTERM, previous)


def fetch_pages_sync(urls, **kwargs) -> dict[str, PageResult]:
    """:func:`fetch_pages` under a SIGTERM->SystemExit handler; recorded server
    groups are reaped on the way out whatever happened (the ``atexit`` hook is the
    last resort)."""
    restore = _install_sigterm()
    try:
        return asyncio.run(fetch_pages(urls, **kwargs))
    finally:
        try:
            reap_groups()       # still under the SIGTERM->SystemExit handler: a SIGTERM here
        finally:                # cannot take SIG_DFL and cut the reap short
            restore()
