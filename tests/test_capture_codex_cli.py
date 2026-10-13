"""The command is a thin wrapper around the public capture function."""

from __future__ import annotations

import json
from datetime import UTC, datetime

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
    assert main([], now=NOW, capture=_misses(0)) == 0
    assert backlog.read_state("codex-capture", status_dir)["consecutive_deferred_runs"] == 0


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
