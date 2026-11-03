"""Slack rate limits (fix 6), channel ``main()`` exit contracts (fix 7) and the
failure taxonomy for unexpected exception types (fix 9 / D N-7).

Hermetic: fake MCP sessions, stubbed collectors, no DB, no network.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from src.capture import mcp_client as mc
from src.capture import slack, outlook
from src.capture.mcp_client import (
    EXIT_AUTH, EXIT_ERROR, EXIT_TRANSPORT, CaptureFailure, McpClient, McpRateLimited, McpToolError,
    rate_limit_retry_after, is_rate_limited,
)

RATE_LIMIT_TEXT = json.dumps({
    "error": "RATE_LIMIT_EXCEEDED",
    "message": 'Rate limit exceeded for tool "get_messages": 120 calls per 60s window. Retry after 38s.',
    "details": {"operation": "get_messages", "timeout": 37937},
    "timestamp": "2026-10-06T02:04:51.078Z",
}, indent=2)


def _reply(text, is_error=False):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], isError=is_error)


class FakeSession:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    async def call_tool(self, tool, params):
        self.calls.append((tool, params))
        item = self.script.pop(0) if self.script else _reply('{"ok": true}')
        if isinstance(item, BaseException):
            raise item
        return item


def _client(session, **kw):
    client = McpClient("/nonexistent/server", [], reconnect_backoff=0, **kw)
    client._session = session
    return client


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(mc.asyncio, "sleep", fake_sleep)
    return slept


# --------------------------------------------------------------------------- #
# Fix 6 — Retry-After is honoured within a bounded budget                        #
# --------------------------------------------------------------------------- #
class TestRateLimitDetection:
    def test_slack_mcp_rate_limit_text_is_recognised_with_its_retry_after(self):
        assert is_rate_limited(RATE_LIMIT_TEXT)
        assert rate_limit_retry_after(RATE_LIMIT_TEXT) == 38.0
        assert rate_limit_retry_after('{"details": {"timeout": 1500}}') == 1.5
        assert rate_limit_retry_after("Retry after 250ms") == 0.25
        assert rate_limit_retry_after("no hint") is None
        assert not is_rate_limited("rate limited")  # free-form text stays an ordinary tool error

    def test_rate_limited_tool_error_is_a_tool_error_subclass_with_retry_after(self):
        client = _client(FakeSession(_reply(RATE_LIMIT_TEXT, is_error=True)))
        with pytest.raises(McpRateLimited) as exc:
            asyncio.run(client.call_json("get_messages"))
        assert isinstance(exc.value, McpToolError) and exc.value.retry_after == 38.0
        assert client.rate_limit_hits == 1


class TestRateLimitBackoff:
    def test_retry_after_is_awaited_then_the_call_succeeds(self, no_sleep):
        session = FakeSession(_reply(RATE_LIMIT_TEXT, is_error=True), _reply('{"messages": []}'))
        client = _client(session, rate_limit_retries=3)
        assert asyncio.run(client.call_json("get_messages", {"channel": "C1"})) == {"messages": []}
        assert no_sleep == [38.0] and client.rate_limit_waited == 38.0 and client.rate_limit_hits == 1
        assert [c[0] for c in session.calls] == ["get_messages", "get_messages"]

    def test_single_wait_is_capped_and_missing_retry_after_uses_the_fallback(self, no_sleep):
        session = FakeSession(_reply(RATE_LIMIT_TEXT, is_error=True),
                              _reply('{"error":"RATE_LIMIT_EXCEEDED","message":"Rate limit exceeded"}', is_error=True),
                              _reply('{"ok": 1}'))
        client = _client(session, rate_limit_retries=3, rate_limit_max_wait=10)
        assert asyncio.run(client.call_json("get_messages")) == {"ok": 1}
        assert no_sleep == [10.0, mc.DEFAULT_RATE_LIMIT_FALLBACK_WAIT]

    def test_retries_are_bounded_per_call_and_the_last_refusal_propagates(self, no_sleep):
        session = FakeSession(*[_reply(RATE_LIMIT_TEXT, is_error=True)] * 5)
        client = _client(session, rate_limit_retries=2)
        with pytest.raises(McpRateLimited):
            asyncio.run(client.call_json("get_messages"))
        assert len(session.calls) == 3 and no_sleep == [38.0, 38.0]

    def test_total_wait_budget_is_per_session(self, no_sleep):
        session = FakeSession(_reply(RATE_LIMIT_TEXT, is_error=True), _reply('{"ok": 1}'),
                              _reply(RATE_LIMIT_TEXT, is_error=True), _reply('{"ok": 2}'))
        client = _client(session, rate_limit_retries=3, rate_limit_wait_budget=50)
        assert asyncio.run(client.call_json("a")) == {"ok": 1}
        with pytest.raises(McpRateLimited):  # 38 + 38 > 50: no second sleep
            asyncio.run(client.call_json("b"))
        assert no_sleep == [38.0] and client.rate_limit_hits == 2

    def test_rate_limit_retries_are_off_unless_the_adapter_opts_in(self, no_sleep):
        client = _client(FakeSession(_reply(RATE_LIMIT_TEXT, is_error=True), _reply('{"ok": 1}')))
        with pytest.raises(McpRateLimited):
            asyncio.run(client.call_json("x"))
        assert no_sleep == []

    def test_rate_limited_reply_never_triggers_a_reconnect(self, no_sleep):
        client = _client(FakeSession(_reply(RATE_LIMIT_TEXT, is_error=True)))
        with pytest.raises(McpRateLimited):
            asyncio.run(client.call_json("x"))
        assert client.reconnects_used == 0


HEALTHY = {"sections": [{"channels": [{"id": "C1", "name": "eng"}, {"id": "C2", "name": "ops"}]}]}


def _throttled_call(refuse_channels):
    """A fake slack-mcp: get_messages is refused for ``refuse_channels`` (post-budget)."""
    async def call(tool, params):
        if tool == "list_my_channels":
            return HEALTHY
        if tool == "list_dms":
            return {"dms": []}
        assert tool == "get_messages"
        if params["channel"] in refuse_channels:
            raise McpRateLimited(f"get_messages: {RATE_LIMIT_TEXT}", 38.0)
        return {"channel": {"purpose": ""}, "messages": [
            {"timestamp": params["since"], "user": {"id": "U1", "realName": "Ann"}, "text": "hello there"}], "hasMore": False}
    return call


class TestSlackCountsRefusedConversationDays:
    def test_collect_counts_still_rate_limited_days_as_failed_not_dropped(self):
        days = [date(2026, 10, 3), date(2026, 10, 4)]
        items = asyncio.run(slack.collect(_throttled_call({"C2"}), days, set()))
        assert len(items) == 2                     # C1 x 2 days captured
        assert items.failed == 2 and items.rate_limited == 2  # C2 x 2 days lost, visibly

    def test_run_is_partial_and_main_exits_non_zero(self, monkeypatch):
        class Throttled:
            rate_limit_hits = 4
            rate_limit_waited = 76.0

            def __init__(self):
                self.call_json = _throttled_call({"C2"})

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        monkeypatch.setattr(slack, "McpClient", lambda *a, **k: Throttled())
        monkeypatch.setattr("src.db.get_processed_source_urls", lambda *a, **k: set())
        monkeypatch.setattr("src.ingest.ingest_batch", lambda items, st: {"processed": len(items), "skipped": 0, "failed": 0})
        monkeypatch.setattr(slack, "completed_days", lambda n: [date(2026, 10, 3)])
        stats = slack.capture(days_back=1)
        assert stats["processed"] == 1 and stats["failed"] == 1 and stats["rate_limited"] == 1
        assert stats["throttle"] == {"hits": 4, "waited_s": 76.0}
        monkeypatch.setattr("sys.argv", ["prog"])
        monkeypatch.setattr(slack, "capture", lambda **kw: stats)
        assert slack.main() == EXIT_ERROR

    def test_slack_opts_into_the_client_rate_limit_budget(self, monkeypatch):
        seen = {}

        class Recording:
            def __init__(self, *a, **k):
                seen.update(k)
                self.rate_limit_hits = 0
                self.rate_limit_waited = 0.0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def call_json(self, tool, params=None):
                return HEALTHY if tool == "list_my_channels" else {"dms": []}

        monkeypatch.setattr(slack, "McpClient", Recording)
        monkeypatch.setattr("src.db.get_processed_source_urls", lambda *a, **k: set())
        slack.capture(days_back=1, dry_run=True)
        assert seen["rate_limit_retries"] == slack.SLACK_RATE_LIMIT_RETRIES >= 1
        assert seen["rate_limit_max_wait"] == slack.SLACK_RATE_LIMIT_MAX_WAIT > 0
        assert seen["rate_limit_wait_budget"] == slack.SLACK_RATE_LIMIT_WAIT_BUDGET >= seen["rate_limit_max_wait"]


# --------------------------------------------------------------------------- #
# Fix 9 — every exception type still yields a classified exit code (N-7)         #
# --------------------------------------------------------------------------- #
class _Client:
    reconnects_used = 0
    rate_limit_hits = 0
    rate_limit_waited = 0.0

    def __init__(self, error):
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def call_json(self, tool, params=None):
        if tool in (slack.PROBE_TOOL,):
            return HEALTHY
        raise self.error


def _exception_group(inner):
    return ExceptionGroup("unhandled errors in a TaskGroup", [inner])


@pytest.mark.parametrize("error,expected", [
    (KeyError("channelId"), EXIT_ERROR),
    (TypeError("'NoneType' object is not subscriptable"), EXIT_ERROR),
    (_exception_group(McpError(ErrorData(code=0, message="Connection closed"))), EXIT_TRANSPORT),
    (_exception_group(RuntimeError("AUTH_ERROR: Failed to refresh authentication token")), EXIT_AUTH),
], ids=["KeyError", "TypeError", "ExceptionGroup(transport)", "ExceptionGroup(auth)"])
def test_slack_unexpected_exception_types_are_classified_and_exit_non_zero(monkeypatch, error, expected):
    monkeypatch.setattr(slack, "McpClient", lambda *a, **k: _Client(error))
    monkeypatch.setattr("src.db.get_processed_source_urls", lambda *a, **k: set())
    monkeypatch.setattr("src.capture.mcp_client.notify", lambda *a, **k: True)
    monkeypatch.setattr("sys.argv", ["prog"])
    with pytest.raises(CaptureFailure) as exc:
        slack.capture(days_back=1)
    assert exc.value.exit_code == expected
    assert slack.main() == expected


@pytest.mark.parametrize("error,expected", [
    (KeyError("id"), EXIT_ERROR),
    (_exception_group(McpError(ErrorData(code=0, message="Connection closed"))), EXIT_TRANSPORT),
], ids=["KeyError", "ExceptionGroup(transport)"])
def test_outlook_unexpected_exception_types_are_classified_and_exit_non_zero(monkeypatch, error, expected):
    class Client(_Client):
        async def call_json(self, tool, params=None):
            raise self.error

    monkeypatch.setattr(outlook, "McpClient", lambda *a, **k: Client(error))
    monkeypatch.setattr("src.db.get_processed_source_urls", lambda *a, **k: set())
    monkeypatch.setattr("src.capture.mcp_client.notify", lambda *a, **k: True)
    monkeypatch.setattr("sys.argv", ["prog"])
    with pytest.raises(CaptureFailure) as exc:
        outlook.capture(days_back=1)
    assert exc.value.exit_code == expected
    assert outlook.main() == expected


def test_keyboard_interrupt_is_never_reclassified(monkeypatch):
    monkeypatch.setattr(slack, "McpClient", lambda *a, **k: _Client(KeyboardInterrupt()))
    monkeypatch.setattr("src.db.get_processed_source_urls", lambda *a, **k: set())
    with pytest.raises(KeyboardInterrupt):
        slack.capture(days_back=1)


# --------------------------------------------------------------------------- #
# Fix 7 — browser / youtube / crawlee / scrape_bookmarks main() exit codes       #
# --------------------------------------------------------------------------- #
class TestMainExitCodes:
    def test_browser_main_maps_failures_to_non_zero(self, monkeypatch):
        from src.capture import browser
        monkeypatch.setattr("sys.argv", ["prog", "--no-scrape"])
        monkeypatch.setattr(browser, "capture", lambda **kw: {"processed": 2, "skipped": 0, "failed": 1, "reading": 0})
        assert browser.main() == 1
        monkeypatch.setattr(browser, "capture", lambda **kw: {"processed": 2, "skipped": 0, "failed": 0, "reading": 0})
        assert browser.main() == 0

    def test_browser_main_treats_per_url_reading_failures_as_advisory(self, monkeypatch):
        # Before 2026-10-14 every failed reading URL was added to "failed" and exited 1;
        # now per-URL outcomes are reported as reading_failed and only "failed" fails.
        from src.capture import browser
        monkeypatch.setattr("sys.argv", ["prog"])
        monkeypatch.setattr(browser, "capture", lambda **kw: {
            "processed": 1, "skipped": 0, "failed": 0, "reading": 0, "reading_failed": 2, "reading_too_short": 5})
        assert browser.main() == 0

    def test_browser_missing_history_is_a_failure_not_a_clean_noop(self, tmp_path, monkeypatch):
        from src.capture import browser
        monkeypatch.setattr("src.db.get_processed_source_urls", lambda *a, **k: set())
        stats = browser.capture(days_back=1, history_path=str(tmp_path / "absent"), scrape=False)
        assert stats["failed"] == 1 and stats["processed"] == 0
        monkeypatch.setattr("sys.argv", ["prog", "--no-scrape"])
        monkeypatch.setattr(browser, "capture", lambda **kw: stats)
        assert browser.main() == 1

    def test_youtube_main_maps_failures_to_non_zero(self, monkeypatch):
        from src.capture import youtube
        monkeypatch.setattr("sys.argv", ["prog"])
        monkeypatch.setattr(youtube, "capture", lambda **kw: {"processed": 0, "skipped": 3, "failed": 2, "no_transcript": 0, "planned": 0})
        assert youtube.main() == 1
        monkeypatch.setattr(youtube, "capture", lambda **kw: {"processed": 1, "skipped": 3, "failed": 0, "no_transcript": 1, "planned": 0})
        assert youtube.main() == 0

    def test_youtube_unreachable_database_exits_non_zero(self, monkeypatch):
        from src.capture import youtube
        monkeypatch.setattr(youtube, "is_reachable", lambda: False)
        monkeypatch.setattr("sys.argv", ["prog"])
        assert youtube.main() == 1

    def test_youtube_partial_enumeration_keeps_entries_and_is_counted_not_failed(self, monkeypatch):
        from src.capture import youtube
        proc = subprocess.CompletedProcess([], 1, '{"id":"v1","title":"t","duration":600}\n', "ERROR: Private video")
        monkeypatch.setattr(youtube, "_ytdlp", lambda *a, **k: proc)
        entries = youtube.list_playlist("LL")
        assert [e["id"] for e in entries] == ["v1"] and "LL" in youtube.last_enumeration_partial
        monkeypatch.setattr(youtube, "is_reachable", lambda: True)
        monkeypatch.setattr(youtube, "get_processed_source_urls", lambda c: {"https://www.youtube.com/watch?v=v1"})
        stats = youtube.capture(playlists=["LL"], dry_run=True)
        assert stats["partial_enumeration"] == 1 and stats["failed"] == 0 and stats["skipped"] == 1
        empty = subprocess.CompletedProcess([], 1, "", "no credentials")
        monkeypatch.setattr(youtube, "_ytdlp", lambda *a, **k: empty)
        with pytest.raises(RuntimeError):
            youtube.list_playlist("LL")

    def test_crawlee_main_maps_failures_to_non_zero(self, monkeypatch, capsys):
        from src.capture import crawlee
        monkeypatch.setattr("sys.argv", ["prog", "--dry-run"])
        monkeypatch.setattr(crawlee, "capture", lambda **kw: {"article": {"processed": 0, "skipped": 0, "failed": 1}, "source_files": {"failed": 0}})
        assert crawlee.main() == 1
        monkeypatch.setattr(crawlee, "capture", lambda **kw: {"article": {"processed": 1, "skipped": 0, "failed": 0}, "source_files": {"failed": 0}})
        assert crawlee.main() == 0
        monkeypatch.setattr(crawlee, "capture", lambda **kw: {"failed": 1, "reason": "missing_source_store"})
        assert crawlee.main() == 1
        assert "missing_source_store" in capsys.readouterr().out

    def test_crawlee_compat_script_delegates_to_the_channel_main(self):
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location("crawlee_ingest", pathlib.Path("scripts/crawlee_ingest.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from src.capture import crawlee
        assert module.main is crawlee.main

    @pytest.mark.parametrize("rc", [1, 0])
    def test_crawlee_compat_script_main_guard_propagates_the_exit_code(self, rc, monkeypatch):
        """R4 review S3-4 (mutant M7c): the ``if __name__ == "__main__"`` line is the
        nightly ``ingest`` job's exit code. Run the script *as* ``__main__`` with the
        channel ``main`` patched and assert ``SystemExit`` carries its return value."""
        import runpy
        from src.capture import crawlee
        monkeypatch.setattr(crawlee, "main", lambda: rc)
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_path("scripts/crawlee_ingest.py", run_name="__main__")
        assert excinfo.value.code == rc

    def test_crawlee_compat_script_exits_non_zero_as_a_subprocess(self, tmp_path):
        """Same contract end to end in a real interpreter: a failing channel ``main``
        must surface as a non-zero process exit for the job wrapper."""
        import pathlib, sys
        shim = tmp_path / "sitecustomize.py"
        shim.write_text(
            "import sys, pathlib\n"
            f"sys.path.insert(0, {str(pathlib.Path.cwd())!r})\n"
            "import src.capture.crawlee as crawlee\n"
            "crawlee.main = lambda: 7\n")
        env = {"PYTHONPATH": str(tmp_path), "PATH": "/usr/bin:/bin"}
        proc = subprocess.run([sys.executable, "scripts/crawlee_ingest.py"], env=env,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 7, proc.stderr

    def _bookmarks(self, tmp_path, monkeypatch, entries):
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location("scrape_bookmarks", pathlib.Path("scripts/scrape_bookmarks.py"))
        sb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sb)
        bookmarks = tmp_path / "Bookmarks"
        bookmarks.write_text(json.dumps({"roots": {"bar": {"type": "folder", "children": [
            {"type": "url", "name": name, "url": url} for name, url in entries]}}}))
        monkeypatch.setattr(sb, "BOOKMARKS_PATH", str(bookmarks))
        monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path / "jobs"))   # ledger + backlog sidecars stay in tmp
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set())
        monkeypatch.setattr(sb, "CRAWLEE_DIR", str(tmp_path))
        monkeypatch.setenv("SECOND_BRAIN_LOG_DIR", str(tmp_path / "logs"))
        cookie = tmp_path / "midway-cookie"     # a valid (unexpired) Midway session, never the real file
        cookie.write_text("#HttpOnly_midway-auth.amazon.com\tFALSE\t/\tTRUE\t4102444800\tsession\tv\n")
        monkeypatch.setattr(sb, "MIDWAY_COOKIE_PATH", str(cookie))
        return sb

    def test_scrape_bookmarks_main_maps_only_run_level_breaks_to_non_zero(self, tmp_path, monkeypatch):
        """Exit contract since the 2026-10-13 triage. Deliberately changed: per-URL failures
        (an internal fetch failing, a public batch with failed URLs) used to exit 1; they are
        now the unreachable ledger's business and exit 0 while the run as a whole worked."""
        sb = self._bookmarks(tmp_path, monkeypatch, [("Public", "https://example.org/public"),
                                                     ("Internal", "https://w.amazon.com/bin/view/Thing")])
        monkeypatch.setattr(sb, "scrape_with_cookies", lambda name, url: False)
        monkeypatch.setattr(sb, "scrape_with_crawlee", lambda urls, **kw: {"processed": 1, "skipped": 0, "failed": 0})
        assert sb.main() == 0                      # the internal fetch failed: per-URL, ledger
        monkeypatch.setattr(sb, "scrape_with_cookies", lambda name, url: True)
        assert sb.main() == 0
        monkeypatch.setattr(sb, "scrape_with_crawlee", lambda urls, **kw: {"processed": 0, "skipped": 0, "failed": 1})
        assert sb.main() == 0                      # the public URL failed: per-URL, ledger
        # Crawlee itself broke (missing node_modules / no parseable output): run-level, exit 1.
        monkeypatch.setattr(sb, "scrape_with_crawlee", lambda urls, **kw: {
            "processed": 0, "skipped": 0, "failed": 1, "deferred": 1, "crawlee_broken": 1})
        assert sb.main() == 1

    def test_scrape_bookmarks_zero_attempted_with_eligible_urls_is_a_failure(self, tmp_path, monkeypatch, capsys):
        sb = self._bookmarks(tmp_path, monkeypatch, [("Public", "https://example.org/public")])
        monkeypatch.setattr(sb, "scrape_with_crawlee", lambda urls, **kw: {
            "processed": 0, "skipped": 0, "failed": 0, "deferred": 1})
        assert sb.main() == 1
        assert "0 of 1 eligible URL(s) attempted" in capsys.readouterr().out

    def test_scrape_bookmarks_zero_public_output_is_a_failure_even_with_internal_attempts(
            self, tmp_path, monkeypatch, capsys):
        """Finding 2 (review 2026-10-13): the "0 attempted" rule applies per path. Chromium /
        Crawlee hangs (every batch killed, no URL line, all public URLs deferred) while one
        internal fetch is attempted: still exit 1."""
        sb = self._bookmarks(tmp_path, monkeypatch, [("Public", "https://example.org/public"),
                                                     ("Internal", "https://w.amazon.com/bin/view/Thing")])
        monkeypatch.setattr(sb, "scrape_with_cookies", lambda name, url: "error")
        monkeypatch.setattr(sb, "scrape_with_crawlee", lambda urls, **kw: {
            "processed": 0, "skipped": 0, "failed": 0, "deferred": 1, "timed_out_batches": 1,
            "batches_run": 1, "batches_with_output": 0})
        assert sb.main() == 1
        assert "0 of 1 public URL(s) attempted in 1 Crawlee batch(es)" in capsys.readouterr().out
        # A batch that printed anything (a mark, a Crawlee record, an index addition) is not this.
        monkeypatch.setattr(sb, "scrape_with_crawlee", lambda urls, **kw: {
            "processed": 0, "skipped": 0, "failed": 1, "deferred": 0, "batches_run": 1, "batches_with_output": 1})
        assert sb.main() == 0
        # No batch ran (budget exhausted before the first): the run-wide rule decides.
        monkeypatch.setattr(sb, "scrape_with_crawlee", lambda urls, **kw: {
            "processed": 0, "skipped": 0, "failed": 0, "deferred": 1, "batches_run": 0, "batches_with_output": 0})
        assert sb.main() == 0

    def test_scrape_bookmarks_zero_public_output_through_real_batches(self, tmp_path, monkeypatch, capsys):
        """The same, end to end through scrape_with_crawlee: every batch is killed silently."""
        sb = self._bookmarks(tmp_path, monkeypatch, [("Public", "https://example.org/public"),
                                                     ("Internal", "https://w.amazon.com/bin/view/Thing")])
        monkeypatch.setattr(sb, "scrape_with_cookies", lambda name, url: "ok")

        def hang(cmd, timeout=None, **kw):
            raise subprocess.TimeoutExpired(cmd, timeout)
        monkeypatch.setattr(sb.subprocess, "run", hang)
        assert sb.main() == 1
        assert "public URL(s) attempted" in capsys.readouterr().out

    def test_scrape_bookmarks_nothing_eligible_is_a_clean_noop(self, tmp_path, monkeypatch):
        sb = self._bookmarks(tmp_path, monkeypatch, [])
        assert sb.main() == 0

    def test_scrape_bookmarks_systemic_rule_until_the_failures_cool_off(self, tmp_path, monkeypatch, capsys):
        """>= SYSTEMIC_MIN_ATTEMPTS attempted, none reached, none cooling off -> exit 1. The
        ledger's third strike puts them in cool-off, and from then on it is exit 0."""
        urls = [(f"P{i}", f"https://w.amazon.com/bin/view/P{i}") for i in range(3)]
        sb = self._bookmarks(tmp_path, monkeypatch, urls)
        monkeypatch.setattr(sb, "scrape_with_cookies", lambda name, url: "timeout")
        from datetime import datetime, timedelta, timezone
        from src.capture import unreachable
        start = datetime(2026, 10, 2, 17, 0, tzinfo=timezone.utc)

        def week(n):                                   # at most one strike per URL per day
            monkeypatch.setattr(unreachable, "_now", lambda: start + timedelta(days=7 * n))
        week(0); assert sb.main() == 1 and "systemic" in capsys.readouterr().out
        week(1); assert sb.main() == 1                 # strike 2
        week(2); assert sb.main() == 0                 # strike 3: all three now cooling off
        out = capsys.readouterr().out
        assert "cooling_off=3" in out and "-> exit 0" in out
        week(3); assert sb.main() == 0                 # cooling off: nothing eligible

    def test_scrape_bookmarks_a_few_cooling_urls_do_not_hide_an_outage(self, tmp_path, monkeypatch, capsys):
        """Finding 4: one URL reaching strike 3 this run must not hide the other failures:
        systemic is `not reached and (failed - cooling) >= SYSTEMIC_MIN_ATTEMPTS`."""
        from src.capture.unreachable import Ledger
        urls = [(f"P{i}", f"https://w.amazon.com/bin/view/P{i}") for i in range(4)]
        sb = self._bookmarks(tmp_path, monkeypatch, urls)
        ledger = Ledger.load()
        from datetime import datetime, timedelta, timezone
        for days_ago in (3, 2):                         # P0 already has two strikes (2 days; DST-safe)
            ledger.record(urls[0][1], "timeout", now=datetime.now(timezone.utc) - timedelta(days=days_ago))
        ledger.save()
        monkeypatch.setattr(sb, "scrape_with_cookies", lambda name, url: "timeout")
        assert sb.main() == 1                           # P0 cools off now; P1..P3 (3) still failing
        out = capsys.readouterr().out
        assert "cooling_off=1" in out and "systemic" in out and "3 not cooling off" in out

    def test_scrape_bookmarks_all_internal_auth_is_not_systemic(self, tmp_path, monkeypatch):
        """An expired Midway cookie: every internal URL is skipped_auth -> exit 0."""
        sb = self._bookmarks(tmp_path, monkeypatch, [(f"P{i}", f"https://w.amazon.com/x{i}") for i in range(5)])
        monkeypatch.setattr(sb, "scrape_with_cookies", lambda name, url: "auth")
        assert sb.main() == 0

    def test_scrape_bookmarks_one_reached_page_is_not_systemic(self, tmp_path, monkeypatch):
        sb = self._bookmarks(tmp_path, monkeypatch, [(f"P{i}", f"https://w.amazon.com/x{i}") for i in range(5)])
        results = iter(["error", "error", "timeout", "error", "too_short"])
        monkeypatch.setattr(sb, "scrape_with_cookies", lambda name, url: next(results))
        assert sb.main() == 0

    def test_scrape_with_crawlee_reports_only_index_verified_successes(self, tmp_path, monkeypatch):
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location("scrape_bookmarks", pathlib.Path("scripts/scrape_bookmarks.py"))
        sb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sb)
        monkeypatch.setattr(sb, "CRAWLEE_DIR", str(tmp_path))
        indexed = [set(), {"https://example.org/a"}]
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: indexed.pop(0))
        monkeypatch.setattr(sb.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""))
        stats = sb.scrape_with_crawlee([("A", "https://example.org/a"), ("B", "https://example.org/b")])
        assert stats == {"processed": 1, "skipped": 0, "failed": 1, "too_short": 0, "timed_out_batches": 0, "deferred": 0,
                         "crawlee_broken": 0, "batches_run": 1, "batches_with_output": 1, "failed_batches": 0}
        assert not list(tmp_path.glob(".sb-urls-*"))  # the temp URL file is always removed
