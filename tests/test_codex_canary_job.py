"""Behavior tests for the one-task Codex operational canary job."""

import os
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/jobs/codex_capture_canary.sh"


def test_canary_job_refuses_to_run_without_an_allowlisted_task():
    env = os.environ.copy()
    env.pop("CODEX_CAPTURE_CANARY_TASK_ID", None)

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "CODEX_CAPTURE_CANARY_TASK_ID is required" in result.stderr


def test_canary_job_invokes_capture_for_only_the_allowlisted_task(tmp_path: Path):
    calls = tmp_path / "calls"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/bash\nprintf '%s\\n' \"$@\" > \"$CANARY_CALLS\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    task_id = "01a014fd-89cf-73c0-a4ef-001e0f89d231"
    env = {
        **os.environ,
        "CANARY_CALLS": str(calls),
        "CODEX_CAPTURE_CANARY_TASK_ID": task_id,
        "SECOND_BRAIN_PYTHON": str(fake_python),
    }

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert calls.read_text(encoding="utf-8").splitlines() == [
        str(SCRIPT.parents[1] / "capture_codex.py"),
        "--task-id",
        task_id,
    ]


def test_canary_job_waits_without_noise_when_local_embedding_is_unavailable(
    tmp_path: Path,
):
    calls = tmp_path / "calls"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/bash\n"
        "if [[ \"$1\" == \"-c\" ]]; then exit 75; fi\n"
        "printf '%s\\n' \"$@\" > \"$CANARY_CALLS\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = {
        **os.environ,
        "CANARY_CALLS": str(calls),
        "CODEX_CAPTURE_CANARY_TASK_ID": "01a014fd-89cf-73c0-a4ef-001e0f89d231",
        "SECOND_BRAIN_PYTHON": str(fake_python),
    }

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "waiting_for_local_embedding" in result.stdout
    assert not calls.exists()
