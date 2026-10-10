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
    return mod


class FakeCrawlee:
    """Index that grows as batches 'scrape'; a batch can time out after indexing some URLs."""

    def __init__(self, indexed=(), timeout_batches=(), partial_on_timeout=1, rc=0):
        self.index = set(indexed)
        self.timeout_batches = set(timeout_batches)
        self.partial = partial_on_timeout
        self.rc = rc
        self.calls = []

    def run(self, cmd, cwd=None, timeout=None, **kw):
        urls = pathlib.Path(cmd[-1]).read_text().split()
        self.calls.append((len(urls), timeout))
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
        # batch 1: p0 already indexed (skipped), p0..p9 written before the kill -> p1..p9 processed,
        # p10..p49 failed; batch 2 completes all 10.
        assert stats == {"processed": 9 + 10, "skipped": 1, "failed": 40, "timed_out_batches": 1, "deferred": 0}
        assert not list(pathlib.Path(sb.CRAWLEE_DIR).glob(".sb-urls-*"))

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

    @pytest.mark.parametrize("resp", [_resp(status=404), _resp(status=500), _resp(text="tiny")])
    def test_genuine_fetch_errors_stay_failed(self, fetch, resp):
        assert fetch(resp) == "failed"

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
        assert "1 skipped_auth, 0 failed" in capsys.readouterr().out
        monkeypatch.setattr(sb, "scrape_with_cookies", lambda name, url: "failed")
        assert sb.main() == 1
