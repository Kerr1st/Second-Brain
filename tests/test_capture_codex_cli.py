"""The command is a thin wrapper around the public capture function."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from scripts.capture_codex import main
from src.capture.codex import CaptureReport, TaskFailure


NOW = datetime(2026, 7, 18, 18, 0, tzinfo=UTC)


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
                ),
            ),
        )

    assert main([], now=NOW, capture=capture) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failures"] == [
        {
            "error_type": "RuntimeError",
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
