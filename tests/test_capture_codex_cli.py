"""The command is a thin wrapper around the public capture function."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.capture_codex import main
from src.capture.codex import CaptureReport, TaskFailure
from src.operations import backlog


NOW = datetime(2026, 7, 18, 18, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def status_dir(tmp_path, monkeypatch):
    """main() records the codex-capture backlog sidecar; keep it out of logs/."""
    directory = tmp_path / "jobs"
    monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(directory))
    return directory


def test_cli_passes_modes_to_the_one_capture_path(capsys):
    calls = []

    def capture(now, **kwargs):
        calls.append((now, kwargs))
        return CaptureReport(enumerated=3, eligible=1, dry_run=True)

    exit_code = main(
        ["--task-id", "real-task-id", "--backfill", "--dry-run"],
        now=NOW,
        capture=capture,
    )

    assert exit_code == 0
    assert calls == [
        (
            NOW,
            {
                "task_id": "real-task-id",
                "backfill": True,
                "dry_run": True,
            },
        )
    ]
    assert json.loads(capsys.readouterr().out)["eligible"] == 1


def test_cli_returns_failure_when_any_task_fails(capsys):
    def capture(now, **kwargs):
        return CaptureReport(
            failed=1,
            failures=(
                TaskFailure(
                    task_id="real-task-id",
                    stage="semantic",
                    error_type="RuntimeError",
                    message="endpoint unreachable",
                ),
            ),
        )

    assert main([], now=NOW, capture=capture) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failures"] == [
        {
            "error_type": "RuntimeError",
            "message": "endpoint unreachable",
            "stage": "semantic",
            "task_id": "real-task-id",
        }
    ]


def test_cli_exits_zero_when_only_semantic_contract_abandonments_failed(capsys):
    def capture(now, **kwargs):
        return CaptureReport(
            failed=2,
            semantic_abandoned=2,
            semantic_contract_failed=2,
            failures=tuple(
                TaskFailure(task_id=f"t{i}", stage="semantic", error_type="SemanticResultError")
                for i in range(2)
            ),
        )

    assert main([], now=NOW, capture=capture) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == 2                       # backward-compatible total
    assert report["semantic_contract_failed"] == 2
    assert "infrastructure_failed" not in report       # derived, not a new serialized field


def test_cli_exits_non_zero_when_infrastructure_failures_accompany_abandonments(capsys):
    def capture(now, **kwargs):
        return CaptureReport(failed=3, semantic_contract_failed=2)

    assert main([], now=NOW, capture=capture) == 1
    assert json.loads(capsys.readouterr().out)["semantic_contract_failed"] == 2


def test_cli_exits_zero_when_contract_misses_will_still_be_retried(capsys):
    """2026-10-13: a retryable SemanticResultError no longer fails the run."""
    def capture(now, **kwargs):
        return CaptureReport(
            failed=1, semantic_contract_failed=1, semantic_contract_retrying=1,
            failures=(TaskFailure("t", "semantic", "SemanticResultError",
                                  "memory fields have invalid types"),),
        )

    assert main([], now=NOW, capture=capture) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["semantic_contract_retrying"] == 1
    assert report["semantic_contract_abandoned"] == 0
    assert report["failures"][0]["message"] == "memory fields have invalid types"


def _misses(count):
    def capture(now, **kwargs):
        return CaptureReport(failed=count, semantic_contract_failed=count,
                             semantic_contract_retrying=count)
    return capture


def test_cli_records_contract_misses_and_warns_at_the_liveness_threshold(capsys, status_dir):
    limit = backlog.threshold("codex-capture")
    assert limit == 2
    assert main([], now=NOW, capture=_misses(2)) == 0
    first = capsys.readouterr()
    assert "WARNING" not in first.err
    assert backlog.read_state("codex-capture", status_dir)["consecutive_deferred_runs"] == 1
    assert main([], now=NOW, capture=_misses(1)) == 0
    second = capsys.readouterr()
    assert "semantic contract misses persist" in second.err and "2 consecutive" in second.err
    json.loads(second.out)  # stdout stays exactly the JSON summary
    state = backlog.read_state("codex-capture", status_dir)
    assert (state["consecutive_deferred_runs"], state["deferred"]) == (2, 1)
    assert main([], now=NOW, capture=_clean(1)) == 0
    assert backlog.read_state("codex-capture", status_dir)["consecutive_deferred_runs"] == 0


# --- review E1: the advisory must not clear itself ---------------------------
def _clean(processed):
    def capture(now, **kwargs):
        return CaptureReport(semantic_processed=processed)
    return capture


def _abandons(count):
    def capture(now, **kwargs):
        return CaptureReport(failed=count, semantic_abandoned=count, semantic_contract_failed=count,
                             semantic_contract_abandoned=count)
    return capture


def _quiet(now, **kwargs):
    """No model call: nothing new, or every batch already abandoned."""
    return CaptureReport(enumerated=4, unchanged=4, semantic_abandoned=1)


def _outage(now, **kwargs):
    return CaptureReport(failed=1, failures=(TaskFailure("t", "semantic", "ConnectionError"),))


def _budget_stopped(now, **kwargs):
    return CaptureReport(failed=1, semantic_skipped_budget=3,
                         failures=(TaskFailure("t", "semantic_budget", "SpendCapExceeded"),))


def _spend_incomplete(now, **kwargs):
    return CaptureReport(semantic_processed=2, semantic_spend_incomplete=True)


def _outage_with_misses(now, **kwargs):
    return CaptureReport(failed=2, semantic_contract_failed=1, semantic_contract_retrying=1)


def _advisory(status_dir):
    from src.operations import liveness
    policy = liveness.load_policy(Path(__file__).resolve().parents[1] / "config/liveness.toml")
    (finding,) = [f for f in liveness.backlog_findings(status_dir, policy) if f["subject"] == "codex-capture"]
    return finding


def test_advisory_survives_abandonment_and_a_quiet_run_until_a_clean_processed_run(capsys, status_dir):
    for capture in (_misses(2), _misses(1), _abandons(1), _quiet):
        assert main([], now=NOW, capture=capture) == 0
    capsys.readouterr()
    finding = _advisory(status_dir)
    assert not finding["healthy"] and finding["state"] == "persistent_backlog"
    state = backlog.read_state("codex-capture", status_dir)
    assert state["consecutive_deferred_runs"] == 3          # quiet run left it alone
    assert state["deferred"] == 1                           # misses of the last streak run
    # A clean run that made no model call still proves nothing.
    assert main([], now=NOW, capture=_clean(0)) == 0
    assert not _advisory(status_dir)["healthy"]
    # Only a run that processed a batch with no misses clears it.
    assert main([], now=NOW, capture=_clean(1)) == 0
    finding = _advisory(status_dir)
    assert finding["healthy"] and finding["consecutive_deferred_runs"] == 0
    assert backlog.read_state("codex-capture", status_dir)["first_deferred_at"] is None


@pytest.mark.parametrize("capture", [_outage, _budget_stopped, _spend_incomplete, _outage_with_misses])
def test_outage_budget_and_incomplete_spend_runs_leave_the_state_unchanged(capsys, status_dir, capture):
    for streak_run in (_misses(1), _misses(3)):
        main([], now=NOW, capture=streak_run)
    before = backlog.state_path("codex-capture", status_dir).read_text()
    main([], now=NOW, capture=capture)
    assert backlog.state_path("codex-capture", status_dir).read_text() == before
    assert not _advisory(status_dir)["healthy"]


def test_a_processed_run_with_misses_increments_not_resets(capsys, status_dir):
    def mixed(now, **kwargs):
        return CaptureReport(semantic_processed=3, failed=1, semantic_contract_failed=1,
                             semantic_contract_retrying=1)
    for _ in range(2):
        assert main([], now=NOW, capture=mixed) == 0
    assert backlog.read_state("codex-capture", status_dir)["consecutive_deferred_runs"] == 2


def test_no_state_is_written_by_a_run_that_does_not_move_the_streak(capsys, status_dir):
    assert main([], now=NOW, capture=_quiet) == 0
    assert not backlog.state_path("codex-capture", status_dir).exists()


@pytest.mark.parametrize("argv", [["--dry-run"], ["--task-id", "one-task"]])
def test_cli_dry_and_single_task_runs_leave_the_streak_alone(capsys, status_dir, argv):
    backlog.record_run("codex-capture", 3, status_dir)
    assert main(argv, now=NOW, capture=_misses(0)) == 0
    assert backlog.read_state("codex-capture", status_dir)["consecutive_deferred_runs"] == 1


def test_cli_streak_bookkeeping_failure_never_changes_the_exit(capsys, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(backlog, "record_run", broken)
    assert main([], now=NOW, capture=_misses(1)) == 0
    assert "could not record" in capsys.readouterr().err
