"""Behavior tests for normal scheduled Codex Task capture."""

import os
from pathlib import Path
import plistlib
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/jobs/codex_capture.sh"
PLIST = (
    Path(__file__).resolve().parents[1]
    / "scheduling/com.second-brain.codex-capture.plist"
)


def test_capture_job_processes_all_eligible_active_tasks_without_backfill(
    tmp_path: Path,
):
    calls = tmp_path / "calls"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/bash\n"
        "if [[ \"$1\" == \"-c\" ]]; then exit 0; fi\n"
        "printf '%s\\n' \"$@\" > \"$CAPTURE_CALLS\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = {
        **os.environ,
        "CAPTURE_CALLS": str(calls),
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
    ]


def test_capture_job_waits_when_local_embedding_is_unavailable(tmp_path: Path):
    calls = tmp_path / "calls"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/bash\n"
        "if [[ \"$1\" == \"-c\" ]]; then exit 75; fi\n"
        "printf '%s\\n' \"$@\" > \"$CAPTURE_CALLS\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = {
        **os.environ,
        "CAPTURE_CALLS": str(calls),
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
    assert result.stdout.strip() == '{"status":"waiting_for_local_embedding"}'
    assert not calls.exists()


def test_hourly_launch_agent_activates_general_capture_without_backfill():
    with PLIST.open("rb") as handle:
        job = plistlib.load(handle)

    assert job["Label"] == "com.second-brain.codex-capture"
    assert job["StartInterval"] == 3600
    assert job["RunAtLoad"] is True
    assert job["ProgramArguments"] == [
        "/bin/bash",
        "/path/to/second-brain/scripts/jobs/codex_capture.sh",
    ]
    assert "CODEX_CAPTURE_CANARY_TASK_ID" not in job.get(
        "EnvironmentVariables", {}
    )
    assert "--backfill" not in job["ProgramArguments"]
