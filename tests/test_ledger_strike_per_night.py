"""At most one unreachable-ledger strike per URL per local calendar day, and
sign-in host parity between scripts/scrape_bookmarks.py and browser_fetch.

2026-10-10 / 10-13: every failure-class cool-off in logs/jobs/bookmarks.unreachable.json
came from same-day manual re-runs (second-brain-triage/2026-10-15/bookmarks-gaps-probe.md
§4). Hermetic: no network, node, Chrome or real ledger."""
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.capture import reading_ledger as rl
from src.capture import unreachable
from src.capture.unreachable import COOL_OFF, DEFERRAL_LIMIT, THRESHOLD, Ledger, url_key

URL = "https://dead.example/page"
REPO = pathlib.Path(__file__).resolve().parents[1]


def local(y, m, d, hh=10, mm=0):
    """An aware datetime at local wall-clock time (the zone the day rule uses)."""
    return datetime(y, m, d, hh, mm).astimezone()


DAY1 = local(2026, 10, 13, 8, 11)


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "jobs" / unreachable.FILENAME)


# --------------------------------------------------------------------------- Ledger


class TestOneStrikePerDay:
    @pytest.mark.parametrize("cls", unreachable.FAILURE_CLASSES)
    def test_same_day_repeats_are_one_strike(self, ledger, cls):
        times = [DAY1 + timedelta(hours=h) for h in (0, 1, 8, 8.5, 9)]   # the 10-13 re-runs
        for moment in times:
            entry = ledger.record(URL, cls, now=moment)
        assert entry["consecutive_failures"] == 1 and entry["attempts"] == 1
        assert entry["skip_until"] is None and not ledger.should_skip(URL, now=times[-1])
        assert entry["last_strike_at"] == times[0].isoformat()
        assert entry["last_attempt_at"] == times[-1].isoformat()

    def test_same_day_repeat_updates_the_reason(self, ledger):
        ledger.record(URL, "timeout", now=DAY1)
        entry = ledger.record(URL, "auth", now=DAY1 + timedelta(hours=3))
        assert entry["status"] == "auth" and entry["consecutive_failures"] == 1

    @pytest.mark.parametrize("cls", unreachable.FAILURE_CLASSES)
    def test_three_different_days_cool_off(self, ledger, cls):
        for day in range(THRESHOLD - 1):
            for hour in (0, 6, 9):                       # re-runs on each day add nothing
                ledger.record(URL, cls, now=DAY1 + timedelta(days=day, hours=hour))
            assert not ledger.should_skip(URL, now=DAY1 + timedelta(days=day, hours=10))
        third = DAY1 + timedelta(days=THRESHOLD - 1)
        entry = ledger.record(URL, cls, now=third)
        assert entry["consecutive_failures"] == THRESHOLD and entry["attempts"] == THRESHOLD
        assert ledger.should_skip(URL, now=third + timedelta(days=1))
        assert ledger.skip_until(URL) == third + COOL_OFF
        # A repeat later that day neither extends nor clears the cool-off.
        ledger.record(URL, cls, now=third + timedelta(hours=5))
        assert ledger.skip_until(URL) == third + COOL_OFF

    def test_the_probe_scenario_no_longer_cools_off(self, ledger):
        """10-10: runs at 08:00, 17:30, 19:08 UTC; 10-13: five runs in ~9 h."""
        utc = timezone.utc
        runs = [datetime(2026, 10, 10, 8, 0, tzinfo=utc), datetime(2026, 10, 10, 17, 30, tzinfo=utc),
                datetime(2026, 10, 10, 19, 8, tzinfo=utc), datetime(2026, 10, 13, 15, 11, tzinfo=utc),
                datetime(2026, 10, 13, 16, 15, tzinfo=utc), datetime(2026, 10, 13, 23, 47, tzinfo=utc),
                datetime(2026, 10, 13, 23, 50, tzinfo=utc), datetime(2026, 10, 13, 23, 51, tzinfo=utc)]
        days = {r.astimezone().date() for r in runs}
        for moment in runs:
            entry = ledger.record(URL, "error", now=moment)
        assert entry["consecutive_failures"] == len(days)
        if len(days) < THRESHOLD:                        # true in US/EU zones: two local days
            assert not ledger.should_skip(URL, now=runs[-1])

    def test_calendar_day_boundary_counts_twice(self, ledger):
        """The rule is the local calendar day, not a rolling window."""
        ledger.record(URL, "error", now=local(2026, 10, 13, 23, 50))
        entry = ledger.record(URL, "error", now=local(2026, 10, 14, 0, 10))
        assert entry["consecutive_failures"] == 2

    def test_success_clears_after_same_day_strikes(self, ledger):
        ledger.record(URL, "auth", now=DAY1)
        ledger.record(URL, "auth", now=DAY1 + timedelta(hours=1))
        assert ledger.record(URL, "ok", now=DAY1 + timedelta(hours=2)) is None
        assert url_key(URL) not in ledger.entries
        entry = ledger.record(URL, "auth", now=DAY1 + timedelta(hours=3))   # a fresh streak
        assert entry["consecutive_failures"] == 1

    def test_cool_off_expiry_retry_failure_cools_again(self, ledger):
        for day in range(THRESHOLD):
            ledger.record(URL, "timeout", now=DAY1 + timedelta(days=day))
        retry = DAY1 + timedelta(days=THRESHOLD - 1) + COOL_OFF + timedelta(hours=1)
        assert not ledger.should_skip(URL, now=retry)
        ledger.record(URL, "timeout", now=retry)
        assert ledger.should_skip(URL, now=retry + timedelta(hours=1))

    def test_too_short_still_cools_on_first_hit_and_ends_the_streak(self, ledger):
        ledger.record(URL, "error", now=DAY1)
        entry = ledger.record(URL, "too_short", now=DAY1 + timedelta(hours=1))
        assert entry["consecutive_failures"] == 0 and "last_strike_at" not in entry
        assert ledger.should_skip(URL, now=DAY1 + timedelta(hours=2))
        entry = ledger.record(URL, "error", now=DAY1 + timedelta(hours=2))   # new streak, same day
        assert entry["consecutive_failures"] == 1

    def test_deferral_strike_is_capped_by_the_day_too(self, ledger):
        ledger.record(URL, "timeout", now=DAY1)
        for i in range(DEFERRAL_LIMIT):
            ledger.record_deferred(URL, now=DAY1 + timedelta(hours=i + 1))
        entry = ledger.entries[url_key(URL)]
        assert entry["consecutive_failures"] == 1 and "deferred_runs" not in entry

    def test_default_clock_is_injectable(self, ledger, monkeypatch):
        for day in range(THRESHOLD):
            monkeypatch.setattr(unreachable, "_now", lambda d=day: DAY1 + timedelta(days=d))
            ledger.record(URL, "error")
        assert ledger.should_skip(URL)


class TestLegacyEntries:
    def _legacy(self, tmp_path, last_attempt, streak=1):
        path = tmp_path / "jobs" / unreachable.FILENAME
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 1, "threshold": 3, "cool_off_days": 30, "urls": {
            url_key(URL): {"domain": "dead.example", "status": "error", "attempts": streak,
                           "consecutive_failures": streak, "first_failed_at": last_attempt.isoformat(),
                           "last_attempt_at": last_attempt.isoformat(), "skip_until": None}}}))
        return path

    def test_legacy_file_loads_and_round_trips(self, tmp_path):
        path = self._legacy(tmp_path, DAY1)
        ledger = Ledger.load(path)
        assert ledger.entries[url_key(URL)]["consecutive_failures"] == 1
        assert not ledger.should_skip(URL, now=DAY1)
        ledger.save()
        data = json.loads(path.read_text())
        assert data["schema_version"] == 1 and "last_strike_at" not in data["urls"][url_key(URL)]

    def test_legacy_last_attempt_today_is_the_last_strike(self, tmp_path):
        ledger = Ledger.load(self._legacy(tmp_path, DAY1, streak=2))
        entry = ledger.record(URL, "error", now=DAY1 + timedelta(hours=4))
        assert entry["consecutive_failures"] == 2 and not ledger.should_skip(URL, now=DAY1 + timedelta(hours=5))

    def test_legacy_last_attempt_yesterday_counts(self, tmp_path):
        ledger = Ledger.load(self._legacy(tmp_path, DAY1 - timedelta(days=1), streak=2))
        entry = ledger.record(URL, "error", now=DAY1)
        assert entry["consecutive_failures"] == 3 and ledger.should_skip(URL, now=DAY1 + timedelta(hours=1))
        assert entry["last_strike_at"] == DAY1.isoformat()


# ------------------------------------------------------------ reading ledger (capture-browser)


@pytest.fixture
def sd(tmp_path):
    return tmp_path


def _reading_entry(sd, url):
    return json.loads((sd / rl.FILENAME).read_text())["urls"][url_key(url)]


class TestReadingLedgerInherits:
    def test_same_day_runs_do_not_cool_off(self, sd):
        for hour in range(THRESHOLD + 1):
            rl.record_outcomes({URL: "timeout"}, status_dir=sd, now=DAY1 + timedelta(hours=hour))
        assert _reading_entry(sd, URL)["consecutive_failures"] == 1
        assert rl.filter_reading([URL], status_dir=sd, now=DAY1 + timedelta(hours=5)) == ([URL], {})

    def test_three_nights_cool_off_and_ok_clears(self, sd):
        nights = [local(2026, 10, 13 + i, 1, 15) for i in range(THRESHOLD)]
        for night in nights:
            rl.record_outcomes({URL: "timeout"}, status_dir=sd, now=night)
        assert rl.filter_reading([URL], status_dir=sd, now=nights[-1])[1] == {URL: "cooling_off"}
        later = nights[-1] + COOL_OFF + timedelta(seconds=1)
        rl.record_outcomes({URL: "ok"}, status_dir=sd, now=later)
        assert url_key(URL) not in json.loads((sd / rl.FILENAME).read_text())["urls"]

    def test_crawlee_and_browser_failures_in_one_run_are_one_strike(self, sd):
        rl.record_outcomes({URL: "error"}, status_dir=sd, now=DAY1, unguarded={URL: "auth"})
        entry = _reading_entry(sd, URL)
        assert entry["consecutive_failures"] == 1 and entry["status"] == "auth"

    def test_dynamic_bot_wall_needs_separate_nights(self, sd):
        urls = ["https://wall.example/a", "https://wall.example/b"]
        for hour in range(THRESHOLD):
            rl.record_outcomes({u: "auth" for u in urls}, status_dir=sd, now=DAY1 + timedelta(hours=hour))
        assert "wall.example" not in rl.bot_walled_hosts(status_dir=sd, now=DAY1 + timedelta(hours=4))
        for day in range(1, THRESHOLD):
            rl.record_outcomes({u: "auth" for u in urls}, status_dir=sd, now=DAY1 + timedelta(days=day))
        assert "wall.example" in rl.bot_walled_hosts(status_dir=sd, now=DAY1 + timedelta(days=THRESHOLD))


# ------------------------------------------------------------------- scrape_bookmarks


@pytest.fixture
def sb(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scrape_bookmarks", REPO / "scripts" / "scrape_bookmarks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "CRAWLEE_DIR", str(tmp_path))
    monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("SECOND_BRAIN_LOG_DIR", str(tmp_path / "logs"))
    cookie = tmp_path / "midway-cookie"     # a valid Midway session (never the real ~/.midway/cookie)
    cookie.write_text("#HttpOnly_midway-auth.amazon.com\tFALSE\t/\tTRUE\t4102444800\tsession\tv\n")
    monkeypatch.setattr(mod, "MIDWAY_COOKIE_PATH", str(cookie))

    def _no_browser(urls, budget_s):        # never the real midway-playwright browser
        from src.capture.browser_fetch import PageResult
        return {u: PageResult(u, "deferred", detail="test") for u in urls}
    monkeypatch.setattr(mod, "browser_fetch_pages", _no_browser)
    return mod


class TestBookmarksJob:
    def _setup(self, sb, tmp_path, monkeypatch):
        bookmarks = tmp_path / "Bookmarks"
        bookmarks.write_text(json.dumps({"roots": {"bar": {"type": "folder", "children": [
            {"type": "url", "name": "B", "url": URL}]}}}))
        monkeypatch.setattr(sb, "BOOKMARKS_PATH", str(bookmarks))
        monkeypatch.setattr(sb, "load_indexed_urls", lambda: set())
        seen = []

        def crawlee(urls, timeout=None, outcomes=None, **kw):
            seen.append([u for _, u in urls])
            for _, u in urls:
                outcomes[u] = "error"
            return {"processed": 0, "skipped": 0, "failed": len(urls), "too_short": 0,
                    "timed_out_batches": 0, "deferred": 0}
        monkeypatch.setattr(sb, "scrape_with_crawlee", crawlee)
        return seen

    def test_manual_reruns_the_same_day_do_not_cool_off(self, sb, tmp_path, monkeypatch, capsys):
        seen = self._setup(sb, tmp_path, monkeypatch)
        for hour in range(THRESHOLD + 1):
            monkeypatch.setattr(unreachable, "_now", lambda h=hour: DAY1 + timedelta(hours=h))
            sb.main()
        assert seen == [[URL]] * (THRESHOLD + 1)
        assert "skipped_unreachable (ledger cool-off): 1" not in capsys.readouterr().out
        assert json.loads((tmp_path / "jobs" / unreachable.FILENAME).read_text())["urls"][url_key(URL)][
            "consecutive_failures"] == 1

    def test_three_scheduled_runs_cool_off(self, sb, tmp_path, monkeypatch, capsys):
        seen = self._setup(sb, tmp_path, monkeypatch)
        for week in range(THRESHOLD + 1):
            monkeypatch.setattr(unreachable, "_now", lambda w=week: DAY1 + timedelta(days=7 * w))
            sb.main()
        assert seen == [[URL]] * THRESHOLD
        assert "skipped_unreachable (ledger cool-off): 1" in capsys.readouterr().out


# ----------------------------------------------------------------- sign-in host parity


def _hosts(pattern: re.Pattern) -> list[str]:
    """Concrete hosts for a ``^(alt|alt|...)suffix$`` host regex (``[a-z0-9-]+`` -> probe)."""
    match = re.fullmatch(r"\^\((.*)\)(.*)\$", pattern.pattern)
    assert match, pattern.pattern
    alternatives, suffix = match.group(1).split("|"), match.group(2)
    return [(alt + suffix).replace("[a-z0-9-]+", "probe").replace("\\", "") for alt in alternatives]


class TestSignInHostParity:
    @pytest.fixture
    def bf(self):
        from src.capture import browser_fetch
        return browser_fetch

    def test_every_browser_fetch_sign_in_host_is_a_bookmarks_sign_in_host(self, sb, bf):
        hosts = _hosts(bf.AUTH_FINAL_HOSTS) + _hosts(bf.SIGN_IN_FINAL_HOSTS)
        assert "probe.idprism-auth.amazon.com" in hosts and "atoz-login.amazon.work" in hosts
        for host in hosts:
            assert bf.is_sign_in_page(f"https://{host}/x"), host          # the probe is faithful
            assert sb.SIGN_IN_HOSTS.match(host), host

    @pytest.mark.parametrize("host", ["atozworkforce.idprism-auth.amazon.com", "us-east-1.idprism-auth.amazon.com",
                                      "midway-auth.amazon.com", "idp.federate.amazon.com", "atoz-login.amazon.work",
                                      "login.microsoftonline.com", "sentry.amazon.com"])
    def test_known_sign_in_hosts(self, sb, host):
        assert sb.SIGN_IN_HOSTS.match(host)
        assert sb.midway_unsupported(f"https://{host}/anything")

    @pytest.mark.parametrize("host", ["idprism-auth.amazon.com.evil.io", "evil-idprism-auth.amazon.com.io",
                                      "idprism-auth.amazon.com", "w.amazon.com", "atoz.amazon.work"])
    def test_lookalikes_are_not(self, sb, bf, host):
        assert not sb.SIGN_IN_HOSTS.match(host)
        assert bool(sb.SIGN_IN_HOSTS.match(host)) == bf.is_sign_in_page(f"https://{host}/x")

    def test_cookie_path_redirect_to_idprism_is_auth(self, sb):
        resp = SimpleNamespace(status_code=200, url="https://atozworkforce.idprism-auth.amazon.com/",
                               history=[SimpleNamespace(url="https://internal-transfer.talent.a2z.com/x")],
                               content=b"<html><title>A to Z</title></html>")
        assert sb._is_auth_response(resp, "https://internal-transfer.talent.a2z.com/x")

    def test_cookie_path_unredirected_sso_path_is_still_content(self, sb):
        resp = SimpleNamespace(status_code=200, url="https://w.amazon.com/bin/view/SSO/Login", history=[],
                               content=b"<html><title>Wiki</title></html>")
        assert not sb._is_auth_response(resp, "https://w.amazon.com/bin/view/SSO/Login")

    def test_browser_route_final_idprism_is_auth(self, sb, bf):
        res = bf.PageResult("https://internal-transfer.talent.a2z.com/x", "ok", text="x" * 300,
                            final_url="https://atozworkforce.idprism-auth.amazon.com/", title="A to Z")
        assert sb.browser_outcome(res.url, res) == "auth"

    def test_bookmarks_cookie_path_imports_without_the_mcp_sdk(self):
        code = ("import sys, importlib.util; sys.modules['mcp'] = None; "
                "sys.modules['mcp.client'] = None; sys.modules['mcp.client.stdio'] = None; "
                "spec = importlib.util.spec_from_file_location('sb', 'scripts/scrape_bookmarks.py'); "
                "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
                "assert m.midway_unsupported('https://x.idprism-auth.amazon.com/'); "
                "assert 'src.capture.browser_fetch' not in sys.modules; print('ok')")
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr
