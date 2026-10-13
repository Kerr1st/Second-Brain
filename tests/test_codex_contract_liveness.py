"""Liveness backstop for Codex semantic-contract misses (2026-10-13).

Contract misses no longer fail codex-capture, so a sudden systematic break must
still surface: scripts/capture_codex.py records each run's
``semantic_contract_failed`` in logs/jobs/codex-capture.backlog.json and
``[backlog.codex-capture]`` in config/liveness.toml turns 2 consecutive runs
with misses into an advisory (never a required failure).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.operations import backlog, liveness

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
JOB = "codex-capture"


@pytest.fixture(scope="module")
def policy():
    return liveness.load_policy(REPO / "config/liveness.toml")


def _codex(directory, policy):
    (finding,) = [f for f in liveness.backlog_findings(directory, policy) if f["subject"] == JOB]
    return finding


def test_codex_contract_advisory_is_configured_with_a_reason(policy):
    entry = policy["backlog"][JOB]
    assert entry["consecutive_runs"] == 2
    assert "semantic_contract_failed" in entry["reason"]
    assert "failures[].message" in entry["reason"]


def test_two_consecutive_runs_with_contract_misses_raise_an_advisory_only(tmp_path, policy):
    assert _codex(tmp_path, policy)["state"] == "no_state"
    backlog.record_run(JOB, 1, tmp_path, now=NOW)
    first = _codex(tmp_path, policy)
    assert first["healthy"] and first["consecutive_deferred_runs"] == 1

    backlog.record_run(JOB, 4, tmp_path, now=NOW + timedelta(hours=6))
    second = _codex(tmp_path, policy)
    assert not second["healthy"] and second["state"] == "persistent_backlog"
    assert second["severity"] == "advisory" and not liveness.required_failure(second)
    assert second["deferred"] == 4 and second["advisory_reason"] == policy["backlog"][JOB]["reason"]

    report = liveness.write_report(
        tmp_path / "liveness-report.json", liveness.run(policy, tmp_path, now=NOW, skip_db=True), NOW
    )
    assert report["healthy"] is True and JOB in report["advisories"]
    assert JOB not in report["required_failures"]


def test_a_clean_run_clears_the_advisory(tmp_path, policy):
    for hours in (0, 6):
        backlog.record_run(JOB, 2, tmp_path, now=NOW + timedelta(hours=hours))
    assert not _codex(tmp_path, policy)["healthy"]
    backlog.record_run(JOB, 0, tmp_path, now=NOW + timedelta(hours=12))
    assert _codex(tmp_path, policy)["healthy"]


def test_the_codex_sidecar_is_not_swept_as_a_job_status(tmp_path):
    path = backlog.state_path(JOB, tmp_path)
    assert path.name == "codex-capture.backlog.json"
    assert path.name.endswith(liveness.SIDECAR_SUFFIXES)
    backlog.record_run(JOB, 1, tmp_path, now=NOW)
    assert liveness.inspect_statuses(tmp_path, now=NOW) == []
