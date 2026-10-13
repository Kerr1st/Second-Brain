#!/usr/bin/env python3
"""Extract Chrome bookmarks and scrape them via Crawlee (public) or
direct fetch with Chrome cookies (internal/authenticated).

Reads Chrome's Bookmarks JSON, filters to scrapable URLs,
deduplicates against Crawlee's knowledge index, and scrapes new URLs.
Internal Amazon links use Chrome cookies for authentication.
"""
import json, os, subprocess, re, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.capture.filters import SKIP_PATTERNS, is_private_host, needs_cookies, load_indexed_urls as _load_indexed_urls
from src.capture.render import html_to_text, render_document
from urllib.parse import urlparse
from datetime import datetime

BOOKMARKS_PATH = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome/Default/Bookmarks"
)
CRAWLEE_DIR = os.path.expanduser("~/Work/Tools/Crawlee")
INDEX_PATH = os.path.join(CRAWLEE_DIR, "knowledge", "index.md")
KNOWLEDGE_DIR = os.path.join(CRAWLEE_DIR, "knowledge", "sources")


def extract_urls(node):
    if node.get("type") == "url":
        return [(node["name"], node["url"])]
    urls = []
    for child in node.get("children", []):
        urls.extend(extract_urls(child))
    return urls


def load_indexed_urls():
    return _load_indexed_urls(INDEX_PATH, sources_dir=KNOWLEDGE_DIR)


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "page"


#: Crawlee is driven in batches, each with its own timeout, so one slow page
#: cannot consume the whole run and partial progress is always measured.
CRAWLEE_BATCH_SIZE = int(os.environ.get("CRAWLEE_BATCH_SIZE", "50"))
CRAWLEE_BATCH_TIMEOUT_S = float(os.environ.get("CRAWLEE_BATCH_TIMEOUT_S", "1200"))
#: Do not start a batch with less than this much budget left.
CRAWLEE_MIN_BATCH_S = 120.0
#: Headroom kept between the Crawlee budget and the job's wall-clock deadline.
DEADLINE_MARGIN_S = 900.0

# Internal fetches that land on a sign-in page or are refused for auth reasons
# are a session problem, not a broken bookmark.
_AUTH_STATUS = {401, 403, 407}
_AUTH_URL_RE = re.compile(
    r"signin|sign-in|sign_in|/login|login\.|midway-auth\.|idp\.federate\.|/sso/|/saml|/oauth2?/|authorize", re.I)


def job_deadline_s():
    """The bookmarks job's wall-clock deadline (plist value, else the runner floor)."""
    try:
        from src.operations.jobs import resolve_timeout
        return resolve_timeout("bookmarks", os.environ)[0]
    except Exception:  # noqa: BLE001 - a bad env value must not stop the scrape; use the floor
        return 3 * 3600.0


def _is_auth_response(resp):
    if resp.status_code in _AUTH_STATUS:
        return True
    # Any redirect target, or the final URL, pointing at sign-in. The originally
    # requested URL (history[0]) is not judged.
    redirects = [getattr(r, "url", "") or "" for r in (getattr(resp, "history", None) or [])][1:]
    return any(_AUTH_URL_RE.search(u) for u in [*redirects, resp.url or ""])


MIDWAY_COOKIE_PATH = os.path.expanduser("~/.midway/cookie")
MIDWAY_SESSION_DOMAIN = "midway-auth.amazon.com"


def _cookie_policy():
    """Host-only cookies (no Domain attribute) go back to exactly their host,
    never to subdomains (Python's default policy would send them on)."""
    import http.cookiejar
    return http.cookiejar.DefaultCookiePolicy(
        strict_ns_domain=http.cookiejar.DefaultCookiePolicy.DomainStrictNonDomain)


def midway_session_valid(path=None):
    """True when the Midway cookie file holds an unexpired ``session`` cookie
    for midway-auth.amazon.com (``load_midway_cookies`` drops expired ones).
    Only domains/names/expiry are inspected; no value is read out."""
    return any(c.name == "session" and c.domain.lstrip(".") == MIDWAY_SESSION_DOMAIN
               for c in load_midway_cookies(path))


def load_midway_cookies(path=None):
    """Read the Netscape-format Midway cookie file into a CookieJar.

    ``http.cookiejar.MozillaCookieJar`` drops curl-style ``#HttpOnly_<domain>``
    lines, which is how mwinit writes the session cookies, so the file is parsed
    here. Expired cookies are skipped. Values never leave the jar (no logging);
    a missing/unreadable file is an empty jar."""
    import http.cookiejar
    jar = http.cookiejar.CookieJar(_cookie_policy())
    try:
        lines = Path(path or MIDWAY_COOKIE_PATH).read_text(errors="replace").splitlines()
    except OSError:
        return jar
    now = time.time()
    for line in lines:
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        fields = line.rstrip("\r\n").split("\t")
        if len(fields) != 7:
            continue
        domain, subdomains, cpath, secure, expires, name, value = fields
        if subdomains.upper() == "TRUE" and not domain.startswith("."):
            domain = "." + domain
        try:
            expires = int(expires) or None
        except ValueError:
            expires = None
        if expires is not None and expires < now:
            continue
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
            path=cpath or "/", path_specified=True, secure=secure.upper() == "TRUE", expires=expires,
            discard=expires is None, comment=None, comment_url=None,
            rest={"HttpOnly": None} if http_only else {}, rfc2109=False))
    return jar


def internal_cookie_jar(url, midway_path=None):
    """Chrome's cookies for the target host merged with the Midway jar, so the
    idp.federate.amazon.com -> midway-auth.amazon.com redirect chain carries
    the Midway session. Midway cookies win on (domain, path, name) clashes."""
    import http.cookiejar
    import browser_cookie3
    jar = http.cookiejar.CookieJar(_cookie_policy())
    try:
        chrome = browser_cookie3.chrome(domain_name=urlparse(url).netloc)
    except Exception as exc:  # noqa: BLE001 - no Chrome cookies still leaves the Midway jar
        print(f"    ! Chrome cookies unavailable: {type(exc).__name__}")
        chrome = None
    for cookie in chrome or ():
        jar.set_cookie(cookie)
    for cookie in load_midway_cookies(midway_path):
        jar.set_cookie(cookie)
    return jar


def cookie_policy_session(requests):
    """A ``requests.Session`` whose Cookie headers follow ``_cookie_policy``.

    requests copies the ``cookies=`` jar into a fresh RequestsCookieJar with
    the default (liberal) policy before building the header, so the jar's own
    policy would be ignored. The prepared request's jar gets the strict policy
    and its header is rebuilt; redirects reuse that jar (``copy`` keeps the
    policy), so every hop of the Midway redirect chain is judged the same way."""
    session = requests.Session()
    prepare = session.prepare_request

    def prepare_request(request):
        prepared = prepare(request)
        jar = getattr(prepared, "_cookies", None)
        if jar is not None:
            jar.set_policy(_cookie_policy())
            prepared.headers.pop("Cookie", None)
            prepared.prepare_cookies(jar)
        return prepared
    session.prepare_request = prepare_request
    return session


def fetch_internal(url):
    """GET an internal page with the merged cookie jar. Returns (outcome, resp);
    outcome is "ok" (200, not judged as auth), "auth" or "failed". Writes nothing."""
    import requests
    with cookie_policy_session(requests) as session:
        resp = session.get(url, cookies=internal_cookie_jar(url), timeout=30,
                           headers={"User-Agent": "Mozilla/5.0"})
    if _is_auth_response(resp):
        return "auth", resp
    if resp.status_code != 200:
        return "failed", resp
    return "ok", resp


def scrape_with_cookies(name, url):
    """Fetch an internal page using Chrome + Midway cookies, save as markdown.

    Returns "ok", "auth" (redirected to sign-in / refused for auth: counted as
    skipped_auth — also when the Midway cookie has expired), "too_short"
    (reached, but too little text to index), "timeout" or "failed" (genuine
    fetch error)."""
    try:
        outcome, resp = fetch_internal(url)
        if outcome != "ok":
            return outcome

        text = html_to_text(resp.text)

        if len(text) < 200:
            return "too_short"

        # Write with metadata header
        date = datetime.now().strftime("%Y-%m-%d")
        import hashlib
        filename = f"{slugify(name)}-{hashlib.sha256(url.encode()).hexdigest()[:12]}.md"
        content = render_document(name, {"Source": url, "Type": "web-page", "Date": date}, text)

        os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
        with open(os.path.join(KNOWLEDGE_DIR, filename), "w") as f:
            f.write(content)
        return "ok"

    except Exception as e:  # noqa: BLE001 - one bookmark never aborts the batch (and `requests` may be unbound on ImportError)
        print(f"    ✗ Cookie fetch failed: {type(e).__name__}")
        return "timeout" if "Timeout" in type(e).__name__ else "failed"


# batch-scrape.js (stderr) prints one line per finished request:
#   "✓ <title>"   indexed (detected through the index, as before)
#   "✗ <url>"     failed after Crawlee's retries
#   "⚠ Skip (too short): <url>"
# Crawlee's own log records (@apify/log, ANSI-colored even into a file) say why
# a request failed. A record is one write that may span several lines: Crawlee
# builds the retry WARN as `[message, userLine].join('\n')`
# (@crawlee/basic basic-crawler.js:1240/:1287), and Playwright errors carry a
# "Call log:", so the {"id":..,"url":..} data often lands on a line of its own
# (tests/fixtures/crawlee-retry-records.log was captured from the real logger).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_TOO_SHORT_RE = re.compile(r"^\s*⚠ Skip \(too short\):\s*(\S+)\s*$")
_FAILED_RE = re.compile(r"^\s*✗\s+(\S+)\s*$")
_RECORD_START_RE = re.compile(r"^\s*(WARN|ERROR|INFO)\b")
_LOG_URL_RE = re.compile(r'\{"id":"[^"]*","url":"((?:[^"\\]|\\.)+)"')
_LOG_AUTH_RE = re.compile(r"\b(401|403|407)\b status code|status code:? (401|403|407)\b", re.I)
_LOG_TIMEOUT_RE = re.compile(r"timed? ?out|TimeoutError", re.I)
_LOG_ERROR_RE = re.compile(r"Error|fail", re.I)


def strip_ansi(text):
    return _ANSI_RE.sub("", text or "")


#: Any of these in a batch's output means the Crawlee install itself is broken
#: (missing node_modules, node not on PATH), not that a URL failed. The
#: module/package patterns also need a non-zero exit (a page *title* can say
#: "Cannot find module"), and per-URL mark lines (✓/⚠/✗ + title/URL) are never
#: matched.
_ENV_BROKEN_RE = re.compile(
    r"node: (command )?not found|No such file or directory.*node|Executable doesn't exist", re.I)
_ENV_BROKEN_MODULE_RE = re.compile(r"ERR_MODULE_NOT_FOUND|Cannot find (package|module)", re.I)
_MARK_RE = re.compile(r"^\s*(✓|✗|⚠)", re.M)


def _iter_records(text):
    """Yield ``(level, joined_text, url)`` for each Crawlee log record that names a URL.

    A record opens on a WARN/ERROR/INFO line and closes on the line carrying
    its ``{"id":..,"url":..}`` data; a mark line or the next record start ends
    an open record that never named a URL (e.g. "Starting the crawler.")."""
    level, lines = None, []
    for line in text.splitlines():
        start = _RECORD_START_RE.match(line)
        if start or (level is not None and _MARK_RE.match(line)):
            level, lines = None, []
        if start:
            level = start.group(1)
        if level is None:
            continue
        lines.append(line)
        match = _LOG_URL_RE.search(line)
        if match:
            yield level, "\n".join(lines), match.group(1).replace('\\"', '"')
            level, lines = None, []


def parse_crawlee_output(text):
    """Map URLs to "too_short" / "auth" / "timeout" / "error" from batch-scrape.js output.

    A URL is mapped only when the output shows it was attempted and did not
    produce a page: a final ``✗``/``⚠`` line, or (when the batch was killed
    before Crawlee finished its retries) a Crawlee WARN/ERROR record naming it.
    ANSI colors are stripped first and records are classified on their joined
    text. URLs the output never mentions are absent: the caller decides whether
    they were attempted (finished run) or never reached (killed batch -> deferred)."""
    text = strip_ansi(text)
    hints, outcomes = {}, {}
    for level, record, url in _iter_records(text):
        if _LOG_AUTH_RE.search(record):
            hints[url] = "auth"
        elif _LOG_TIMEOUT_RE.search(record):
            hints.setdefault(url, "timeout")
        elif level in ("WARN", "ERROR") or _LOG_ERROR_RE.search(record):
            hints.setdefault(url, "error")
    for line in text.splitlines():
        match = _TOO_SHORT_RE.match(line)
        if match:
            outcomes[match.group(1)] = "too_short"
            continue
        match = _FAILED_RE.match(line)
        if match and match.group(1).startswith("http"):
            outcomes[match.group(1)] = "fail"
    result = {url: (hints.get(url, "error") if cls == "fail" else cls) for url, cls in outcomes.items()}
    for url, cls in hints.items():   # attempted and erroring when the batch ended
        result.setdefault(url, cls)
    return result


def crawlee_broken(rc, output):
    """True when a batch says nothing about the URLs because Crawlee itself broke:
    its environment is missing, or it exited non-zero without one parseable line."""
    text = strip_ansi(output)
    unmarked = "\n".join(line for line in text.splitlines() if not _MARK_RE.match(line))
    failed_exit = rc not in (0, None)
    if _ENV_BROKEN_RE.search(unmarked) or (failed_exit and _ENV_BROKEN_MODULE_RE.search(unmarked)):
        return True
    return failed_exit and not _MARK_RE.search(text) and not _LOG_URL_RE.search(text)


#: Options for batch-scrape.js, read from env only once
#: second-brain-triage/2026-10-13/crawlee-batch-scrape.patch is applied in
#: ~/Work/Tools/Crawlee (harmless before; its defaults are Crawlee's, so other
#: callers are unchanged). What actually bounds a blocked URL is
#: MAX_REQUEST_RETRIES=1: a 403 "Request blocked" is a plain ``Error``
#: (basic-crawler.js ``_throwOnBlockedRequest``), not a ``SessionError``, so it
#: is retried maxRequestRetries times (default 3) and session rotations never
#: apply to it. MAX_SESSION_ROTATIONS=0 only affects real SessionErrors
#: (e.g. proxy/session-pool failures). ABORT_DOWNLOADS=1 fails a URL that
#: starts a file download on its first attempt. ``check_batch_scrape_patch``
#: warns once per run when the patch is missing.
BATCH_SCRAPE_ENV = {
    "BATCH_SCRAPE_MAX_REQUEST_RETRIES": "1",
    "BATCH_SCRAPE_MAX_SESSION_ROTATIONS": "0",
    "BATCH_SCRAPE_ABORT_DOWNLOADS": "1",
}
#: logs/bookmarks-crawlee-<batch>.log
CRAWLEE_LOG_NAME = "bookmarks-crawlee"


def batch_scrape_path():
    """batch-scrape.js inside ``CRAWLEE_DIR`` (the same place it is run from)."""
    return os.path.join(CRAWLEE_DIR, "batch-scrape.js")


def check_batch_scrape_patch(path=None):
    """Warn (once per call; main calls it once per run) when batch-scrape.js does
    not read BATCH_SCRAPE_* options, i.e. the patch is not applied and
    ``BATCH_SCRAPE_ENV`` has no effect. Returns True when the patch is present."""
    path = path or batch_scrape_path()
    try:
        applied = "BATCH_SCRAPE_" in Path(path).read_text(errors="replace")
    except OSError:
        applied = False
    if not applied:
        print(f"WARNING: {path} does not read BATCH_SCRAPE_* options (crawlee-batch-scrape.patch "
              f"not applied): Crawlee keeps its default 3 retries per blocked URL")
    return applied


def log_dir():
    from src.operations.backlog import REPO
    return Path(os.environ.get("SECOND_BRAIN_LOG_DIR", str(REPO / "logs")))


def _open_kept_log(name, n):
    """Open logs/<name>-<n>.log read/write (truncated, mode 0600). O_NOFOLLOW:
    a planted symlink is refused, never truncated or chmod'ed through."""
    path = log_dir() / f"{name}-{n}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return fd, str(path)


def _clear_kept_logs(name):
    for old in log_dir().glob(f"{name}-*.log"):
        try:
            old.unlink()
        except OSError:
            pass


def _run_crawlee_batch(batch, timeout, *, keep_log=None, env=None):
    """Run one Crawlee batch; returns (returncode or None on timeout, output text).

    Output goes to a file rather than a pipe, so what was printed before a
    timeout kill is still readable. With ``keep_log=(name, n)`` that file is
    ``logs/<name>-<n>.log`` (mode 0600, overwritten each run) and kept, so
    Crawlee's per-request error lines stay inspectable; otherwise a temporary
    file that is deleted. batch-scrape.js prints titles, URLs and Crawlee's
    error messages only — never cookies or headers. ``env`` adds variables
    for batch-scrape.js. A missing ``node`` is rc 127 with a message."""
    fd, url_file = tempfile.mkstemp(prefix=".sb-urls-", suffix=".txt", dir=CRAWLEE_DIR)
    if keep_log:
        log_fd, log_file = _open_kept_log(*keep_log)
    else:
        log_fd, log_file = tempfile.mkstemp(prefix=".sb-out-", suffix=".log")
    try:
        with os.fdopen(fd, "w") as handle:
            for _, url in batch:
                handle.write(url + "\n")
        with os.fdopen(log_fd, "w+", errors="replace") as log:
            try:
                result = subprocess.run(
                    ["node", "batch-scrape.js", url_file], cwd=CRAWLEE_DIR,
                    timeout=timeout, stdout=log, stderr=subprocess.STDOUT, text=True,
                    **({"env": {**os.environ, **env}} if env else {}),
                )
                rc = result.returncode
            except subprocess.TimeoutExpired:
                rc = None
            except OSError as exc:  # node missing / not executable
                log.write(f"node: not found ({type(exc).__name__})\n")
                rc = 127
            # Read back through the same fd (not by path): another run's file
            # at that path after _clear_kept_logs can never be read instead.
            try:
                log.flush()
                log.seek(0)            # lseek(fd, 0, SEEK_SET); node wrote via the fd directly
                output = log.read()
            except (OSError, ValueError):
                output = ""
        return rc, output
    finally:
        os.unlink(url_file)
        if not keep_log and os.path.exists(log_file):
            os.unlink(log_file)


def scrape_with_crawlee(urls, timeout=None, batch_size=None, batch_timeout=None, outcomes=None,
                        log_name=None, env=None, killed_deferred=None):
    """Scrape public URLs via Crawlee in bounded batches.

    ``timeout`` is the wall-clock budget for the whole call (default: the job
    deadline less a margin); each batch also has its own ``batch_timeout``.
    Only verified index additions count as processed, also after a timeout, so
    partial progress is kept. Pages Crawlee reached but skipped as too short
    are ``too_short`` (not failed).

    ``deferred`` (not failed, no ledger strike; retried next run because they
    are still unindexed) counts URLs never started because the budget ran out,
    and URLs of a killed (timed-out) or crashed batch that the output never
    shows as attempted. ``failed`` counts only URLs the output shows were
    attempted and failed (``✗``, or Crawlee error lines when the batch was
    killed mid-retry), plus unmarked URLs of a batch that finished normally
    (rc 0: Crawlee attempted all of them). ``crawlee_broken`` counts batches
    whose output shows Crawlee itself broke (see ``crawlee_broken``).

    When given, ``outcomes`` is filled with url -> "ok" or a ledger class
    (never for deferred URLs), and ``killed_deferred`` (a set) with the URLs
    deferred because their batch was killed or crashed (not budget-deferred,
    not a broken Crawlee): the caller bounds those through the ledger's
    ``deferred_runs``. ``batches_run`` / ``batches_with_output`` count batches
    that ran and those whose output showed any mark, Crawlee record or index
    addition; ``failed_batches`` counts non-zero exits that named no failed URL.
    ``log_name`` keeps each batch's output as ``logs/<log_name>-<n>.log``;
    ``env`` is passed to batch-scrape.js.
    """
    stats = {"processed": 0, "skipped": 0, "failed": 0, "too_short": 0, "timed_out_batches": 0,
             "deferred": 0, "crawlee_broken": 0, "batches_run": 0, "batches_with_output": 0,
             "failed_batches": 0}
    outcomes = {} if outcomes is None else outcomes
    if not urls:
        return stats
    batch_size = batch_size or CRAWLEE_BATCH_SIZE
    batch_timeout = batch_timeout or CRAWLEE_BATCH_TIMEOUT_S
    budget = timeout if timeout is not None else job_deadline_s() - DEADLINE_MARGIN_S
    started = time.monotonic()
    before = load_indexed_urls()
    if log_name:
        _clear_kept_logs(log_name)
    batches = [urls[i:i + batch_size] for i in range(0, len(urls), batch_size)]
    for n, batch in enumerate(batches, start=1):
        remaining = budget - (time.monotonic() - started)
        if remaining < min(CRAWLEE_MIN_BATCH_S, batch_timeout):
            left = sum(len(b) for b in batches[n - 1:])
            stats["deferred"] += left
            print(f"Crawlee: budget of {budget:.0f}s exhausted; {left} URL(s) "
                  f"in {len(batches) - n + 1} batch(es) deferred to the next run")
            break
        this_timeout = min(batch_timeout, remaining)
        requested = {url for _, url in batch}
        skipped_here = requested & before
        rc, output = _run_crawlee_batch(batch, this_timeout,
                                        keep_log=(log_name, n) if log_name else None, env=env)
        parsed = parse_crawlee_output(output)
        broken = crawlee_broken(rc, output)
        # Measure progress from the index whatever happened, so partial
        # successes in a timed-out or failed batch still count.
        after = load_indexed_urls()
        processed = len((requested - before) & after)
        failed = too_short = deferred = 0
        for url in requested - before:
            if url in after:
                outcomes[url] = "ok"
            elif url in parsed:
                outcomes[url] = parsed[url]
                if parsed[url] == "too_short":
                    too_short += 1
                else:
                    failed += 1
            elif rc == 0 and not broken:
                outcomes[url] = "error"    # Crawlee finished the run, so it attempted this URL
                failed += 1
            else:
                deferred += 1              # killed / crashed before the output shows an attempt
                if killed_deferred is not None and not broken:
                    killed_deferred.add(url)
        stats["batches_run"] += 1
        stats["batches_with_output"] += int(bool(processed or parsed or _MARK_RE.search(strip_ansi(output))))
        if rc is None:
            stats["timed_out_batches"] += 1
            print(f"Crawlee batch {n}/{len(batches)} TIMED OUT after {this_timeout:.0f}s: "
                  f"{processed} of {len(requested) - len(skipped_here)} new URL(s) indexed, "
                  f"{failed} failed, {deferred} deferred (not attempted)")
        elif rc:
            print(f"Crawlee batch {n}/{len(batches)} exited {rc}"
                  f"{' (Crawlee itself broke)' if broken else ''}: {processed} indexed, "
                  f"{failed} failed, {deferred} deferred")
            if not failed:
                # A nonzero exit without one failed URL: counted on its own, not as a URL failure.
                stats["failed_batches"] += 1
        stats["processed"] += processed
        stats["skipped"] += len(skipped_here)
        stats["failed"] += failed
        stats["too_short"] += too_short
        stats["deferred"] += deferred
        stats["crawlee_broken"] += int(broken)
        before = after
    return stats


def main():
    started = time.monotonic()
    with open(BOOKMARKS_PATH) as f:
        data = json.load(f)

    all_urls = []
    for key in data["roots"]:
        if isinstance(data["roots"][key], dict):
            all_urls.extend(extract_urls(data["roots"][key]))

    scrapable = [(n, u) for n, u in all_urls if not SKIP_PATTERNS.search(u) and not is_private_host(u)]
    indexed = load_indexed_urls()
    new = [(n, u) for n, u in scrapable if u not in indexed]

    ledger = _load_ledger()
    unreachable = [(n, u) for n, u in new if ledger is not None and ledger.should_skip(u)]
    skip = {u for _, u in unreachable}
    pending = [(n, u) for n, u in new if u not in skip]
    internal = [(n, u) for n, u in pending if needs_cookies(u)]
    public = [(n, u) for n, u in pending if not needs_cookies(u)]
    if ledger is not None:
        # URLs a killed batch left unattempted go first, so they are attempted
        # (and bounded by the ledger's deferred_runs) instead of queuing behind
        # the same hang again. sorted() is stable: bookmark order otherwise.
        public.sort(key=lambda item: ledger.deferred_runs(item[1]) == 0)

    print(f"Total: {len(all_urls)}, Scrapable: {len(scrapable)}, New: {len(new)}")
    print(f"  skipped_unreachable (ledger cool-off): {len(unreachable)}")
    print(f"  Internal (cookies): {len(internal)}")
    print(f"  Public (Crawlee): {len(public)}")

    ok = fail = skipped_auth = too_short = deferred_auth = 0
    outcomes = {}

    if internal and not midway_session_valid():
        # Every internal fetch would be an auth strike for a session problem:
        # skip the path, no ledger strike, retried next run (still unindexed).
        deferred_auth = len(internal)
        print(f"WARNING: Midway cookie missing/expired — run mwinit; {deferred_auth} internal URLs deferred")
        internal = []

    # Internal links with cookies
    if internal:
        print(f"\n--- Internal links ({len(internal)}) ---")
        for i, (name, url) in enumerate(internal):
            print(f"  [{i+1}/{len(internal)}] {name[:50]}")
            outcome = scrape_with_cookies(name, url)
            if outcome is True or outcome == "ok":
                ok += 1
                outcomes[url] = "ok"
            elif outcome == "auth":
                skipped_auth += 1
                outcomes[url] = "auth"
            elif outcome == "too_short":
                too_short += 1
                outcomes[url] = "too_short"
            else:
                fail += 1
                outcomes[url] = "timeout" if outcome == "timeout" else "error"

    # Public links with Crawlee: bounded batches, total kept under the job deadline.
    budget = job_deadline_s() - (time.monotonic() - started) - DEADLINE_MARGIN_S
    killed_deferred = set()
    if public:
        check_batch_scrape_patch()
    public_stats = scrape_with_crawlee(public, timeout=max(0.0, budget), outcomes=outcomes,
                                       log_name=CRAWLEE_LOG_NAME, env=BATCH_SCRAPE_ENV,
                                       killed_deferred=killed_deferred) if public else \
        {"processed": 0, "skipped": 0, "failed": 0, "too_short": 0, "timed_out_batches": 0, "deferred": 0}

    print(f"\nInternal: {ok} scraped, {skipped_auth} skipped_auth, {fail} failed, {too_short} too_short, "
          f"{deferred_auth} deferred_auth")
    print(f"Public: {public_stats}")
    record_unreachable(ledger, outcomes, len(unreachable), killed_deferred=killed_deferred)
    report_backlog(public_stats.get("deferred", 0))
    internal_counts = {"ok": ok, "auth": skipped_auth, "too_short": too_short, "failed": fail,
                       "deferred_auth": deferred_auth}
    internal_auth = {u for u, c in outcomes.items() if c == "auth" and needs_cookies(u)}
    return run_exit_code(internal_counts, public_stats, outcomes, ledger,
                         eligible=len(pending) - deferred_auth, public_eligible=len(public),
                         skipped_unreachable=len(unreachable), internal_auth=internal_auth)


#: Fewer attempts than this can not show a systemic break by themselves (one
#: bad bookmark left over is not an outage; network loss is caught by the
#: wrapper's preflight and a broken Crawlee by ``crawlee_broken``).
SYSTEMIC_MIN_ATTEMPTS = 3


def run_exit_code(internal, public_stats, outcomes, ledger, *, eligible, public_eligible=0,
                  skipped_unreachable=0, internal_auth=()):
    """The job's exit code: 0 unless the run as a whole broke.

    Per-URL outcomes (auth, too_short, timeout, error on individual URLs) are
    the unreachable ledger's business and never fail the job. Exit 1 when:

    * a Crawlee batch shows Crawlee itself broke (``crawlee_broken``: missing
      node / node_modules, or non-zero exit with no parseable output);
    * there were eligible URLs (new, not cooling off, not ``deferred_auth``)
      but none was attempted (budget-deferred URLs are not attempts);
    * per path, public: public URLs were eligible, at least one Crawlee batch
      ran, and the batches produced no mark, no Crawlee record naming a URL
      and no index addition — a hung Chromium/Crawlee is an outage even when
      internal fetches were attempted in the same run;
    * systemic: none reached a page (0 ok, 0 too_short) and at least
      ``SYSTEMIC_MIN_ATTEMPTS`` of the failed URLs (internal skipped_auth
      excluded — a session issue) are not in ledger cool-off after this run's
      strikes were recorded. (A few URLs that just reached strike 3 no longer
      hide an outage of the rest.)

    ``failed_batches`` (non-zero exits that named no failed URL) are reported,
    not counted as URL failures. This job does not touch the DB (the nightly
    ingest job ingests Crawlee's index); ledger/backlog bookkeeping errors are
    warnings, never the exit. Always prints one WARNING summary line with the
    per-class counts.
    """
    from collections import Counter
    classes = Counter(outcomes.values())
    pub_ok = public_stats.get("processed", 0)
    pub_short = public_stats.get("too_short", 0)
    pub_failed = public_stats.get("failed", 0)
    deferred = public_stats.get("deferred", 0)
    broken = public_stats.get("crawlee_broken", 0)
    batches_run = public_stats.get("batches_run", 0)
    batches_with_output = public_stats.get("batches_with_output", batches_run)
    failed_batches = public_stats.get("failed_batches", 0)
    deferred_auth = internal.get("deferred_auth", 0)
    reached = internal["ok"] + internal["too_short"] + pub_ok + pub_short
    failed = internal["failed"] + pub_failed
    attempted = reached + failed + internal["auth"]
    internal_auth = set(internal_auth or ())
    failed_urls = [u for u, c in outcomes.items()
                   if c in ("auth", "timeout", "error") and u not in internal_auth]
    cooling = sum(1 for u in failed_urls if ledger is not None and ledger.should_skip(u))

    reason = None
    if broken:
        reason = f"crawlee_broken ({broken} batch(es))"
    elif eligible and not attempted:
        reason = f"0 of {eligible} eligible URL(s) attempted"
    elif public_eligible and batches_run and not batches_with_output:
        reason = f"0 of {public_eligible} public URL(s) attempted in {batches_run} Crawlee batch(es)"
    elif not reached and (failed - cooling) >= SYSTEMIC_MIN_ATTEMPTS:
        reason = f"systemic: all {failed} attempted URL(s) failed, {failed - cooling} not cooling off"
    print(f"WARNING: bookmarks summary — eligible={eligible} attempted={attempted} ok={internal['ok'] + pub_ok} "
          f"auth={classes.get('auth', 0)} too_short={internal['too_short'] + pub_short} "
          f"timeout={classes.get('timeout', 0)} error={classes.get('error', 0)} failed={failed} "
          f"deferred={deferred} deferred_auth={deferred_auth} skipped_unreachable={skipped_unreachable} "
          f"cooling_off={cooling} failed_batches={failed_batches} "
          f"crawlee_broken={broken} -> exit {1 if reason else 0}"
          f"{f' ({reason})' if reason else ' (per-URL outcomes are left to the unreachable ledger)'}")
    return 1 if reason else 0


def _load_ledger():
    try:
        from src.capture.unreachable import Ledger
        return Ledger.load()
    except Exception as exc:  # noqa: BLE001 - a broken ledger means "retry everything", never a crash
        print(f"WARNING: could not read the bookmarks unreachable ledger: {type(exc).__name__}")
        return None


def record_unreachable(ledger, outcomes, skipped_unreachable, killed_deferred=()):
    """Persist this run's per-URL outcomes and print the ledger summary (counts only).

    ``killed_deferred`` URLs (in a killed batch, never shown attempted) get one
    ``deferred_runs`` count each; at the ledger's ``DEFERRAL_LIMIT`` that is a
    ``timeout`` strike (``deferral strike(s)`` in the summary)."""
    if ledger is None:
        return None
    from collections import Counter
    counts = Counter(cls for cls in outcomes.values() if cls != "ok")
    newly = deferral_strikes = 0
    try:
        for url, cls in outcomes.items():
            was_skipping = ledger.skip_until(url) is not None
            entry = ledger.record(url, cls)
            if entry and entry.get("skip_until") and not was_skipping:
                newly += 1
        for url in sorted(set(killed_deferred) - set(outcomes)):
            was_skipping = ledger.skip_until(url) is not None
            entry = ledger.record_deferred(url)
            if entry:
                deferral_strikes += 1
                if entry.get("skip_until") and not was_skipping:
                    newly += 1
        ledger.save()
    except Exception as exc:  # noqa: BLE001 - ledger bookkeeping must not change the job's outcome
        print(f"WARNING: could not record the bookmarks unreachable ledger: {type(exc).__name__}")
        return None
    print(f"Unreachable: {skipped_unreachable} skipped_unreachable, {newly} newly cooling off, "
          f"attempt classes {dict(sorted(counts.items()))}, {len(set(killed_deferred))} killed-batch "
          f"deferral(s), {deferral_strikes} deferral strike(s), ledger {len(ledger.entries)} URL(s)")
    return counts


def report_backlog(deferred):
    """Track consecutive runs that deferred URLs; warn once the backlog persists.

    A single deferral is normal (the rest is retried next run) and never fails
    the job; the liveness sweep turns a persistent streak into an advisory."""
    try:
        from src.operations import backlog
        state = backlog.record_run("bookmarks", deferred)
        limit = backlog.threshold("bookmarks")
    except Exception as exc:  # noqa: BLE001 - backlog bookkeeping must not change the job's outcome
        print(f"WARNING: could not record the bookmarks backlog state: {type(exc).__name__}")
        return None
    streak = state["consecutive_deferred_runs"]
    if streak >= limit:
        print(f"WARNING: bookmarks backlog persists — {deferred} URL(s) deferred, {streak} consecutive "
              f"run(s) with deferrals (threshold {limit}); the Crawlee budget is not draining the queue")
    return state


if __name__ == "__main__":
    sys.exit(main())
