"""capture-browser reading ledger and bot-wall blocklist (src/capture/reading_ledger.py)."""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest

from src.capture import reading_ledger as rl
from src.capture import unreachable
from src.operations import liveness

NOW = datetime(2026, 10, 15, 1, 15, tzinfo=timezone.utc)


def _entry(url, status="auth", cooling=True, now=NOW):
    until = (now + timedelta(days=10)) if cooling else (now - timedelta(days=1))
    return unreachable.url_key(url), {"domain": urlparse(url).netloc, "status": status,
                                      "attempts": 3, "consecutive_failures": 3,
                                      "skip_until": until.isoformat()}


def _write(path, entries):
    path.write_text(json.dumps({"schema_version": 1, "urls": dict(entries)}))


@pytest.fixture
def sd(tmp_path, monkeypatch):
    monkeypatch.delenv("SECOND_BRAIN_WRAPPED", raising=False)
    _write(tmp_path / rl.FILENAME, [])
    return tmp_path


def _bookmarks(sd, *entries):
    _write(sd / unreachable.FILENAME, entries)


def _reading(sd, *entries):
    _write(sd / rl.FILENAME, entries)


# ------------------------------------------------------------- seed

@pytest.mark.parametrize("url", [
    "https://queue.acm.org/doi/10.1145/3454122.3454124",
    "https://www.crunchbase.com/organization/profisee",
    "https://glassdoor.com/Reviews/x.htm",
    "http://www.yelp.com/biz/mr-tint-san-jose",
    "https://www.etsy.com/listing/1",
    "https://www.homedepot.com/p/1",
    "https://QUEUE.ACM.ORG./doi/1",
])
def test_seed_matches_host_and_subdomains(sd, url):
    keep, skipped = rl.filter_reading([url], status_dir=sd, now=NOW)
    assert keep == [] and skipped == {url: "bot_walled"}


@pytest.mark.parametrize("url", [
    "https://queue.acm.org.evil.io/doi/1",
    "https://notcrunchbase.com/x",
    "https://evil.io/?r=queue.acm.org",
    "https://acm.org/x",
    "https://evil.io/queue.acm.org",
])
def test_lookalike_hosts_are_not_matched(sd, url):
    assert rl.filter_reading([url], status_dir=sd, now=NOW) == ([url], {})


def test_seed_is_in_bot_walled_hosts(sd):
    assert rl.BOT_WALLED_SEED <= rl.bot_walled_hosts(status_dir=sd, now=NOW)


# ------------------------------------------------------------- dynamic rule

@pytest.mark.parametrize("ledger", ["reading", "bookmarks"])
def test_dynamic_rule_from_each_ledger_threshold_two(sd, ledger):
    write = _reading if ledger == "reading" else _bookmarks
    write(sd, _entry("https://blocked.example/a"))
    assert "blocked.example" not in rl.bot_walled_hosts(status_dir=sd, now=NOW)   # 1 < 2
    write(sd, _entry("https://blocked.example/a"), _entry("https://blocked.example/b"))
    assert "blocked.example" in rl.bot_walled_hosts(status_dir=sd, now=NOW)
    url = "https://blocked.example/c"
    assert rl.filter_reading([url], status_dir=sd, now=NOW) == ([], {url: "bot_walled"})


def test_dynamic_rule_counts_distinct_urls_across_both_ledgers(sd):
    same = _entry("https://blocked.example/a")
    _reading(sd, same)
    _bookmarks(sd, same)                      # same URL twice is still one URL
    assert "blocked.example" not in rl.bot_walled_hosts(status_dir=sd, now=NOW)
    _bookmarks(sd, same, _entry("https://blocked.example/b"))
    assert "blocked.example" in rl.bot_walled_hosts(status_dir=sd, now=NOW)


def test_dynamic_rule_needs_auth_and_active_cool_off(sd):
    _bookmarks(sd, _entry("https://a.example/1", status="timeout"), _entry("https://a.example/2", status="timeout"),
               _entry("https://b.example/1", status="too_short"), _entry("https://b.example/2", status="too_short"),
               _entry("https://c.example/1", cooling=False), _entry("https://c.example/2", cooling=False),
               _entry("https://d.example/1"), _entry("https://d.example/2", status="error"))
    hosts = rl.bot_walled_hosts(status_dir=sd, now=NOW)
    assert not {"a.example", "b.example", "c.example", "d.example"} & hosts


def test_dynamic_host_ignores_port_in_domain(sd):
    _bookmarks(sd, _entry("https://p.example:8443/1"), _entry("https://p.example:8443/2"))
    assert "p.example" in rl.bot_walled_hosts(status_dir=sd, now=NOW)


# ------------------------------------------------------------- internal hosts

@pytest.mark.parametrize("host", ["w.amazon.com", "phonetool.amazon.com", "docs.hub.amazon.dev",
                                  "foo.aws.dev", "gather.a2z.com", "s3.us-east-1.amazonaws.com",
                                  "atoz.amazon.work", "amazon.sharepoint.com", "workshops.aws"])
def test_internal_hosts_are_never_bot_walled(sd, host):
    _bookmarks(sd, *(_entry(f"https://{host}/{i}") for i in range(4)))
    _reading(sd, *(_entry(f"https://{host}/r{i}") for i in range(4)))
    assert host not in rl.bot_walled_hosts(status_dir=sd, now=NOW)
    url = f"https://{host}/new"
    assert rl.filter_reading([url], status_dir=sd, now=NOW) == ([url], {})


def test_seed_contains_no_internal_host():
    assert not any(rl.internal_host(h) for h in rl.BOT_WALLED_SEED)


# ------------------------------------------------------------- filter_reading

def test_cooling_off_skip_and_order_kept(sd):
    cooling = "https://ok.example/cooling"
    _reading(sd, _entry(cooling, status="timeout"), _entry("https://ok.example/expired", cooling=False))
    urls = ["https://z.example/1", cooling, "https://a.example/2", "https://queue.acm.org/doi/1",
            "https://ok.example/expired", "https://m.example/3"]
    keep, skipped = rl.filter_reading(urls, status_dir=sd, now=NOW)
    assert keep == ["https://z.example/1", "https://a.example/2", "https://ok.example/expired", "https://m.example/3"]
    assert skipped == {cooling: "cooling_off", "https://queue.acm.org/doi/1": "bot_walled"}


def test_own_cool_off_wins_over_bot_wall(sd):
    url = "https://queue.acm.org/doi/2"
    _reading(sd, _entry(url))
    assert rl.filter_reading([url], status_dir=sd, now=NOW)[1] == {url: "cooling_off"}


@pytest.mark.parametrize("content", ["{not json", "[]", '{"urls": []}', ""])
def test_corrupt_ledger_keeps_everything_with_warning(sd, caplog, content):
    (sd / rl.FILENAME).write_text(content)
    (sd / unreachable.FILENAME).write_text(content)
    urls = ["https://a.example/1", "https://b.example/2", "not a url", ""]
    with caplog.at_level(logging.WARNING, logger=rl.__name__):
        assert rl.filter_reading(urls, status_dir=sd, now=NOW) == (urls, {})
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_missing_ledger_keeps_everything_without_warning(tmp_path, caplog):
    urls = ["https://a.example/1"]
    with caplog.at_level(logging.DEBUG, logger=rl.__name__):
        for _ in range(3):                                   # every run until the first record
            assert rl.filter_reading(urls, status_dir=tmp_path / "nope", now=NOW) == (urls, {})
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert all(r.levelno == logging.INFO for r in caplog.records if "not created yet" in r.getMessage())


def test_filter_reading_never_raises(sd, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("x")
    monkeypatch.setattr(rl, "_bot_walled", boom)
    assert rl.filter_reading(["https://a.example/"], status_dir=sd, now=NOW) == (["https://a.example/"], {})


def test_default_status_dir_follows_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path))
    assert rl.ledger_path() == tmp_path / "capture-browser.unreachable.json"


def test_sidecar_suffix_is_skipped_by_liveness():
    assert rl.FILENAME.endswith(liveness.SIDECAR_SUFFIXES)


# ------------------------------------------------------------- record_outcomes

def _entries(sd):
    return json.loads((sd / rl.FILENAME).read_text())["urls"]


def test_record_outcomes_strikes_then_cools_off_and_ok_clears(sd):
    url = "https://slow.example/1"
    key = unreachable.url_key(url)
    for i in range(unreachable.THRESHOLD - 1):
        rl.record_outcomes({url: "timeout"}, status_dir=sd, now=NOW)
        assert _entries(sd)[key]["skip_until"] is None
        assert rl.filter_reading([url], status_dir=sd, now=NOW) == ([url], {})
    rl.record_outcomes({url: "timeout"}, status_dir=sd, now=NOW)
    assert _entries(sd)[key]["consecutive_failures"] == unreachable.THRESHOLD
    assert rl.filter_reading([url], status_dir=sd, now=NOW)[1] == {url: "cooling_off"}
    later = NOW + unreachable.COOL_OFF + timedelta(seconds=1)
    assert rl.filter_reading([url], status_dir=sd, now=later) == ([url], {})
    rl.record_outcomes({url: "ok"}, status_dir=sd, now=later)
    assert key not in _entries(sd)


def test_record_outcomes_too_short_cools_off_immediately(sd):
    url = "https://thin.example/1"
    rl.record_outcomes({url: "too_short", "https://fine.example/": "ok"}, status_dir=sd, now=NOW)
    entry = _entries(sd)[unreachable.url_key(url)]
    assert entry["status"] == "too_short" and entry["domain"] == "thin.example"
    assert url not in json.dumps(_entries(sd))            # domain only, never the URL
    assert rl.filter_reading([url], status_dir=sd, now=NOW)[1] == {url: "cooling_off"}


def test_record_outcomes_auth_feeds_dynamic_rule(sd):
    urls = ["https://wall.example/a", "https://wall.example/b"]
    for _ in range(unreachable.THRESHOLD):
        rl.record_outcomes({u: "auth" for u in urls}, status_dir=sd, now=NOW)
    assert "wall.example" in rl.bot_walled_hosts(status_dir=sd, now=NOW)


def test_record_outcomes_never_raises(sd, monkeypatch):
    rl.record_outcomes({}, status_dir=sd, now=NOW)
    monkeypatch.setattr(unreachable.Ledger, "save", lambda self: (_ for _ in ()).throw(OSError("disk")))
    rl.record_outcomes({"https://a.example/": "auth"}, status_dir=sd, now=NOW)
    (sd / rl.FILENAME).write_text("{corrupt")
    monkeypatch.undo()
    rl.record_outcomes({"https://a.example/": "auth"}, status_dir=sd, now=NOW)   # replaces the corrupt file
    assert unreachable.url_key("https://a.example/") in _entries(sd)


def test_record_outcomes_file_mode_0600(sd):
    rl.record_outcomes({"https://a.example/": "auth"}, status_dir=sd, now=NOW)
    assert (sd / rl.FILENAME).stat().st_mode & 0o777 == 0o600


# ------------------------------------------------------------- bookmarks ledger is read-only

def test_bookmarks_ledger_is_never_modified(sd):
    _bookmarks(sd, _entry("https://www.crunchbase.com/a"), _entry("https://www.crunchbase.com/b"))
    path = sd / unreachable.FILENAME
    before = (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    time.sleep(0.01)
    rl.filter_reading(["https://www.crunchbase.com/c", "https://a.example/"], status_dir=sd, now=NOW)
    rl.bot_walled_hosts(status_dir=sd, now=NOW)
    rl.record_outcomes({"https://www.crunchbase.com/a": "ok", "https://www.crunchbase.com/c": "auth"},
                       status_dir=sd, now=NOW)
    assert (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) == before


# ------------------------------------------------------------- locking

def _hold(sd):
    handle = (sd / rl.LOCK_NAME).open("a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def _lock_is_free(sd):
    with (sd / rl.LOCK_NAME).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
        return True


def test_outside_job_takes_and_releases_the_job_lock(sd, monkeypatch):
    seen = []
    real_save = unreachable.Ledger.save

    def save(self):
        seen.append(_lock_is_free(sd))       # a second fd must conflict while we hold it
        real_save(self)
    monkeypatch.setattr(unreachable.Ledger, "save", save)
    rl.record_outcomes({"https://a.example/": "auth"}, status_dir=sd, now=NOW)
    assert seen == [False]
    assert _lock_is_free(sd)
    assert unreachable.url_key("https://a.example/") in _entries(sd)


def test_outside_job_with_lock_busy_skips_without_hanging(sd, monkeypatch, caplog):
    monkeypatch.setattr(rl, "LOCK_WAIT_S", 0.3)
    holder = _hold(sd)
    try:
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=rl.__name__):
            rl.record_outcomes({"https://a.example/": "auth"}, status_dir=sd, now=NOW)
        assert time.monotonic() - started < 3
    finally:
        holder.close()
    assert _entries(sd) == {}
    assert any(rl.LOCK_NAME in r.getMessage() for r in caplog.records)


def test_inside_job_does_not_relock_when_wrapper_holds_it(sd, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_WRAPPED", "1")
    monkeypatch.setattr(rl, "LOCK_WAIT_S", 30.0)          # a re-lock attempt would hang the test
    holder = _hold(sd)                                     # stands in for jobs.run_job's fd
    try:
        started = time.monotonic()
        rl.record_outcomes({"https://a.example/": "auth"}, status_dir=sd, now=NOW)
        assert time.monotonic() - started < 3
        assert unreachable.url_key("https://a.example/") in _entries(sd)
        assert not _lock_is_free(sd)                       # the wrapper's lock is untouched
    finally:
        holder.close()


def test_lock_name_matches_job_runner():
    import inspect
    from src.operations import jobs
    assert 'f"{name}.lock"' in inspect.getsource(jobs.run_job)
    assert rl.LOCK_NAME == f"{rl.JOB}.lock"
