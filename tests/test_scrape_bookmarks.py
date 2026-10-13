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
    monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path / "jobs"))
    return mod


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
                         "deferred": 40, "crawlee_broken": 0}
        assert "40 deferred" in out
        assert not list(pathlib.Path(sb.CRAWLEE_DIR).glob(".sb-urls-*"))

    def test_killed_batch_counts_only_urls_the_output_shows_attempted_as_failed(self, sb, monkeypatch):
        output = ("✗ https://example.org/p1\n"
                  'WARN PlaywrightCrawler: Reclaiming failed request back to the list or queue. '
                  'Request blocked - received 403 status code. {"id":"x","url":"https://example.org/p2","retryCount":2}\n'
                  'ERROR PlaywrightCrawler: navigation timed out {"url":"https://example.org/p3"}\n')
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
        (0, "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'crawlee'\n", True),
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
        assert stats["processed"] == 3 and stats["failed"] == 1

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
        holder = {}
        fake_requests = types.SimpleNamespace(get=lambda *a, **k: holder["resp"])
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
        _resp(url="https://w.amazon.com/signin?return=x"),
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
