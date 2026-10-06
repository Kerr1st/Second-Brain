"""Liveness predicates (src/operations/liveness.py + config/liveness.toml).

E-N6/N7: the 74ba02d liveness channels had no tests (removing them left the suite
green) and would have been red from day one for known-disabled producers. Every
predicate here is exercised with a deliberately broken fixture, both as a pure
function and end to end against the disposable test database.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.operations import liveness
from src.operations.liveness import (dream_cycle_findings, evaluate_channels, inspect_statuses, load_policy,
                                     required_failure, tolerance_days, zero_output_findings)

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def policy():
    return load_policy(REPO / "config/liveness.toml")


# ------------------------------------------------------------------ policy pins

def test_production_channel_thresholds_are_unchanged_from_r1(policy):
    """verify_liveness.sh at 1071a81/R1 (2026-09-23 thresholds). Moving them into
    TOML must not move the numbers."""
    expected = {
        "quick_desktop_doc": (3, "retired"), "quick_desktop_synth": (21, "required"), "web_articles": (9, "required"),
        "cli_chat": (4, "required"), "youtube": (9, "required"), "outlook_email": (3, "required"),
        "outlook_calendar": (5, "required"), "slack": (4, "required"), "browser_history": (4, "required"),
        "codex_task": (7, "required"),
    }
    for label, (days, severity) in expected.items():
        assert (policy["channels"][label]["max_age_days"], policy["channels"][label]["severity"]) == (days, severity), label
    assert policy["channels"]["quick_desktop_synth"]["predicate"] == \
        "source_type LIKE 'quick_desktop_%' AND source_type NOT IN ('quick_desktop_doc','quick_desktop_session_event')"


def test_known_disabled_producers_are_advisory_by_explicit_config_with_a_reason(policy):
    advisory = {label for label, c in policy["channels"].items() if c["severity"] == "advisory"}
    assert advisory == {"distilled_chat", "weekly_digest"}
    for label in advisory:
        assert policy["channels"][label]["reason"].strip()
    assert policy["channels"]["ide_chat"] == {"predicate": "source_type = 'kiro_ide_chat'", "max_age_days": 7, "severity": "required"}
    assert "distilled_chat" in policy["zero_output"]["ignore"] and "weekly_digest" in policy["zero_output"]["ignore"]


def test_policy_rejects_a_non_required_channel_without_a_reason(tmp_path):
    bad = tmp_path / "liveness.toml"
    bad.write_text('[channels.x]\npredicate = "source_type = \'x\'"\nmax_age_days = 3\nseverity = "advisory"\n')
    with pytest.raises(ValueError, match="reason"):
        load_policy(bad)
    bad.write_text('[channels.x]\npredicate = "source_type = \'x\'"\nmax_age_days = 0\nseverity = "required"\n')
    with pytest.raises(ValueError, match="max_age_days"):
        load_policy(bad)


# ------------------------------------------------------------- channel ages

def test_silent_required_channel_is_a_required_failure(policy):
    ages = {label: 0 for label in policy["channels"]}
    ages["slack"] = 99
    findings = {f["subject"]: f for f in evaluate_channels(ages, policy)}
    assert required_failure(findings["slack"]) and findings["slack"]["state"] == "silent"
    assert "slack SILENT — last capture 99d ago (max 4d)" in findings["slack"]["message"]
    assert findings["outlook_email"]["healthy"] and findings["outlook_email"]["state"] == "healthy"


def test_silent_advisory_channel_is_reported_but_never_fails(policy):
    """E-N6: distill is operator-disabled and weekly digest has failed every Monday;
    both must stay visible as ⚠, never ❌."""
    ages = {label: 0 for label in policy["channels"]}
    ages["distilled_chat"] = 14
    ages["weekly_digest"] = liveness.NEVER
    findings = {f["subject"]: f for f in evaluate_channels(ages, policy)}
    assert findings["distilled_chat"]["healthy"] is False and not required_failure(findings["distilled_chat"])
    assert "advisory" in findings["distilled_chat"]["message"] and "launchctl-disabled" in findings["distilled_chat"]["message"]
    assert findings["weekly_digest"]["state"] == "never_produced" and not required_failure(findings["weekly_digest"])
    assert not any(required_failure(f) for f in findings.values())


def test_retired_channel_stays_visible_without_failing_and_query_failure_is_required(policy):
    ages = {label: 0 for label in policy["channels"]}
    ages["quick_desktop_doc"] = 400
    ages["codex_task"] = None
    findings = {f["subject"]: f for f in evaluate_channels(ages, policy)}
    assert findings["quick_desktop_doc"]["healthy"] and findings["quick_desktop_doc"]["state"] == "retired"
    assert "400d ago" in findings["quick_desktop_doc"]["message"]
    assert required_failure(findings["codex_task"]) and findings["codex_task"]["state"] == "query_failed"


# -------------------------------------------------------------- zero output

def test_tolerance_learns_the_channel_cadence():
    daily = [date(2026, 9, d) for d in range(1, 30)]
    assert tolerance_days(daily, floor=2) == 2
    bursty = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 9), date(2026, 9, 23)]   # gaps 1, 7, 14
    assert tolerance_days(bursty, floor=2) == 15
    assert tolerance_days([date(2026, 9, 1)], floor=7) == 7 and tolerance_days([], floor=7) == 7


def test_zero_output_catches_a_dead_unenumerated_channel(policy):
    """kiro_ide_chat died 2026-05-17 and nobody noticed because it was not in the
    enumerated list. The generic rule flags any producer that stopped."""
    today = date(2026, 10, 6)
    last_seen = {"api_browser": date(2026, 8, 1), "slack": date(2026, 10, 5)}
    days_seen = {"api_browser": [date(2026, 7, d) for d in range(10, 31)], "slack": [date(2026, 9, d) for d in range(1, 30)]}
    findings = {f["subject"]: f for f in zero_output_findings(today, last_seen, days_seen, enumerated={"slack"}, policy=policy)}
    assert required_failure(findings["api_browser"]) and findings["api_browser"]["age_days"] == 66
    assert "NOT an enumerated channel" in findings["api_browser"]["message"]
    assert findings["slack"]["healthy"] and findings["slack"]["severity"] == "advisory"


def test_zero_output_flags_the_slack_rate_limit_gap_as_advisory_for_an_enumerated_channel(policy):
    """2026-10-03/04: Slack exited 3 twice; the fixed 4 d threshold was still green at
    09:00 on 10-05. A daily channel with zero rows for 3 days is abnormal for ITS cadence."""
    today = date(2026, 10, 5)
    last_seen = {"slack": date(2026, 10, 2)}
    days_seen = {"slack": [date(2026, 9, d) for d in range(1, 31)] + [date(2026, 10, 1), date(2026, 10, 2)]}
    (finding,) = zero_output_findings(today, last_seen, days_seen, enumerated={"slack"}, policy=policy)
    assert finding["healthy"] is False and finding["severity"] == "advisory" and not required_failure(finding)
    assert finding["age_days"] == 3 and finding["tolerance_days"] == 2


def test_zero_output_respects_the_ignore_list_and_the_required_floor(policy):
    today = date(2026, 10, 6)
    last_seen = {"distilled_chat": date(2026, 9, 22), "codex_task": date(2026, 10, 1), "manual": date(2025, 1, 1)}
    days_seen = {"codex_task": [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]}
    findings = {f["subject"]: f for f in zero_output_findings(today, last_seen, days_seen, enumerated=set(), policy=policy)}
    assert set(findings) == {"codex_task"}                        # ignored producers never appear
    assert findings["codex_task"]["healthy"] and findings["codex_task"]["tolerance_days"] == 7   # floor for unenumerated


# -------------------------------------------------------------- dream cycle

def _run(days_ago, generated, completed=True):
    started = NOW - timedelta(days=days_ago, hours=7)
    return {"started_at": started, "completed_at": started + timedelta(hours=1) if completed else None,
            "candidates_generated": generated}


def test_dream_cycle_completed_at_freshness(policy):
    healthy = {f["subject"]: f for f in dream_cycle_findings([_run(1, 4), _run(3, 2)], NOW, policy)}
    assert healthy["completed_at"]["healthy"] and healthy["candidates"]["healthy"]
    stalled = {f["subject"]: f for f in dream_cycle_findings([_run(1, 4, completed=False), _run(9, 3)], NOW, policy)}
    assert required_failure(stalled["completed_at"]) and stalled["completed_at"]["age_days"] == 9
    (never,) = dream_cycle_findings([_run(1, 4, completed=False)], NOW, policy)
    assert required_failure(never) and never["state"] == "never_completed"


def test_dream_cycle_zero_candidate_runs_are_detected(policy):
    """A completed run with 0 candidates (2026-09-30) counted as healthy in 74ba02d."""
    one = {f["subject"]: f for f in dream_cycle_findings([_run(1, 0), _run(3, 5)], NOW, policy)}
    assert one["candidates"]["healthy"] is False and one["candidates"]["severity"] == "advisory"
    two = {f["subject"]: f for f in dream_cycle_findings([_run(1, 0), _run(3, 0), _run(5, 6)], NOW, policy)}
    assert required_failure(two["candidates"]) and two["candidates"]["zero_candidate_streak"] == 2
    # an uncompleted (crashed) newest run does not hide the streak of the completed ones
    crash = {f["subject"]: f for f in dream_cycle_findings([_run(1, None, completed=False), _run(3, 0), _run(5, 0)], NOW, policy)}
    assert required_failure(crash["candidates"])


# ------------------------------------------------------------ status sweep

def _status(path, **value):
    path.write_text(json.dumps(value))


def test_status_sweep_reports_last_exit_non_zero_and_stale_status_files(tmp_path, policy):
    _status(tmp_path / "backup.json", job="backup", state="failed", exit_code=69, consecutive_failures=2,
            first_failure_at=(NOW - timedelta(hours=7)).isoformat(), finished_at=(NOW - timedelta(hours=7)).isoformat())
    _status(tmp_path / "qd-sync.json", job="qd-sync", state="succeeded", exit_code=0,
            started_at=(NOW - timedelta(hours=20)).isoformat(), finished_at=(NOW - timedelta(hours=20)).isoformat())
    _status(tmp_path / "youtube.json", job="youtube", state="succeeded", exit_code=0,
            started_at=(NOW - timedelta(hours=8)).isoformat(), finished_at=(NOW - timedelta(hours=8)).isoformat())
    results = {r["job"]: r for r in inspect_statuses(tmp_path, now=NOW, failure_days=policy["jobs"]["failure_days"],
                                                     expected_interval_hours=policy["jobs"]["expected_interval_hours"])}
    assert results["backup"]["healthy"] is False and results["backup"]["severity"] == "required"
    assert "last exit 69" in results["backup"]["message"]
    assert results["qd-sync"]["state"] == "stale_status" and results["qd-sync"]["healthy"] is False
    assert "expected every 6h" in results["qd-sync"]["message"]
    assert results["youtube"]["healthy"] is True
    missing = {job for job, r in results.items() if r["state"] == "missing"}
    assert "dream-cycle" in missing and results["dream-cycle"]["severity"] == "advisory" and results["dream-cycle"]["healthy"]


def test_failure_days_one_turns_a_fresh_failure_into_an_advisory(tmp_path):
    _status(tmp_path / "x.json", job="x", state="failed", exit_code=1, consecutive_failures=1,
            first_failure_at=(NOW - timedelta(hours=7)).isoformat())
    (fresh,) = inspect_statuses(tmp_path, now=NOW, failure_days=1)
    assert fresh["healthy"] is False and fresh["severity"] == "advisory" and fresh["persistent"] is False
    (old,) = inspect_statuses(tmp_path, now=NOW + timedelta(days=1), failure_days=1)
    assert old["severity"] == "required"


def test_status_sweep_ignores_its_own_report_and_flags_unreadable_files(tmp_path):
    (tmp_path / "liveness-report.json").write_text("{}")
    (tmp_path / "broken.json").write_text("not json")
    results = inspect_statuses(tmp_path, now=NOW)
    assert [r["job"] for r in results] == ["broken"] and results[0]["state"] == "invalid_status"


# ------------------------------------------------------- end to end (database)

def _seed(conn, source_type, days_ago, count=1):
    with conn.cursor() as cur:
        for i in range(count):
            cur.execute("INSERT INTO memories (type, title, content, source_type, source_url, created_at) "
                        "VALUES ('source', %s, 'synthetic', %s, %s, now() - make_interval(days => %s))",
                        (f"{source_type} {days_ago} {i}", source_type, f"test://{source_type}/{days_ago}/{i}", days_ago))


def _seed_all_fresh(conn):
    for source_type in ("quick_desktop_decision", "article", "kiro_ide_chat", "kiro_cli_chat", "youtube", "outlook_email",
                        "outlook_calendar", "slack", "browser_history", "codex_task"):
        for d in range(0, 20):
            _seed(conn, source_type, d)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dream_cycle_runs (run_type, started_at, completed_at, candidates_generated) "
                    "VALUES ('scheduled', now() - interval '1 day', now() - interval '23 hours', 3)")


def test_fresh_database_and_empty_status_dir_are_green_apart_from_known_disabled_advisories(test_conn, tmp_path, policy):
    _seed_all_fresh(test_conn)
    findings = liveness.run(policy, tmp_path, conn=test_conn, now=datetime.now(timezone.utc))
    report = liveness.write_report(tmp_path / "liveness-report.json", findings, datetime.now(timezone.utc))
    assert report["healthy"] is True, report["required_failures"]
    assert set(report["advisories"]) >= {"distilled_chat", "weekly_digest"}     # watched, never red
    assert json.loads((tmp_path / "liveness-report.json").read_text())["healthy"] is True


def test_deliberately_broken_channels_fail_the_sweep_end_to_end(test_conn, tmp_path, policy):
    """One fixture per predicate: a silent required channel, a dead unenumerated
    producer, a stalled dream cycle, a failed job and a stale status file."""
    _seed_all_fresh(test_conn)
    with test_conn.cursor() as cur:
        cur.execute("DELETE FROM memories WHERE source_type = 'slack'")                 # silent required channel
        cur.execute("UPDATE dream_cycle_runs SET completed_at = completed_at - interval '20 days', "
                    "started_at = started_at - interval '20 days'")                        # stalled dream cycle
    _seed(test_conn, "slack", 30)
    _seed(test_conn, "api_browser", 40)                                                 # dead, not enumerated
    _status(tmp_path / "backup.json", job="backup", state="failed", exit_code=1, consecutive_failures=1,
            first_failure_at=(NOW - timedelta(hours=2)).isoformat())
    now = datetime.now(timezone.utc)
    findings = liveness.run(policy, tmp_path, conn=test_conn, now=now)
    report = liveness.write_report(tmp_path / "liveness-report.json", findings, now)
    assert report["healthy"] is False
    assert {"slack", "api_browser", "completed_at", "backup"} <= set(report["required_failures"])
    by = {(f["check"], f["subject"]): f for f in findings}
    assert by[("channel", "slack")]["age_days"] == 30 and by[("channel", "slack")]["state"] == "silent"
    assert by[("zero_output", "api_browser")]["enumerated"] is False
    assert by[("dream_cycle", "completed_at")]["state"] == "stalled"
    assert by[("channel", "ide_chat")]["healthy"] is True                                # R4 revived it
    assert by[("channel", "distilled_chat")]["severity"] == "advisory"


def test_enumerated_detection_uses_the_real_predicates_against_the_database(test_conn, tmp_path, policy):
    _seed(test_conn, "quick_desktop_feed", 1)
    _seed(test_conn, "quick_desktop_doc", 1)
    _seed(test_conn, "api_email", 1)
    _, _, enumerated = liveness.fetch_output_history(test_conn, policy)
    assert "quick_desktop_feed" in enumerated           # matched by the LIKE predicate
    assert "quick_desktop_doc" not in enumerated        # retired channel's predicate is excluded
    assert "api_email" not in enumerated


def test_cli_writes_the_durable_report_and_exits_one_on_a_required_failure(tmp_path, policy, capsys):
    _status(tmp_path / "youtube.json", job="youtube", state="failed", exit_code=1, consecutive_failures=3,
            first_failure_at=(NOW - timedelta(days=2)).isoformat())
    rc = liveness.main(["--skip-db", "--status-dir", str(tmp_path), "--config", str(REPO / "config/liveness.toml")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "❌ [job:youtube] job youtube last exit 1" in out
    report = json.loads((tmp_path / "liveness-report.json").read_text())
    assert report["healthy"] is False and "youtube" in report["required_failures"]
    assert (tmp_path / "liveness-report.json").stat().st_mode & 0o077 == 0
    rc = liveness.main(["--skip-db", "--status-dir", str(tmp_path / "empty"), "--config", str(REPO / "config/liveness.toml")])
    assert rc == 0
