"""Behavior tests for normal scheduled Codex Task capture.

Two expectations changed on 2026-09-22 when this code was ported onto local main
(see scripts/jobs/codex_capture.sh header and docs/PLAN-capture-repair.md):

* The job no longer gates itself on a local Ollama embedding server. The original
  printed {"status":"waiting_for_local_embedding"} and exited 0 when the server was
  absent — a silent no-op that reports success, which is how two other capture
  channels stayed broken unnoticed for days. There is no Ollama on this machine and
  src/embeddings.py is Titan-only, so the job now runs and fails loudly instead.
* The LaunchAgent runs every 6 hours rather than hourly, because a task whose
  derived-memory contract keeps failing is retried on every run at ~$0.10 of Bedrock
  Opus 5 per retry.
"""

import os
from pathlib import Path
import plistlib
import subprocess

import pytest


@pytest.fixture(autouse=True)
def isolated_capture_paths(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    probe = tmp_path / "pg-ready-stub"
    probe.write_text("#!/bin/sh\nexit 0\n")
    probe.chmod(0o700)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("PG_ISREADY_BIN", str(probe))


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/jobs/codex_capture.sh"
PLIST = (
    Path(__file__).resolve().parents[1]
    / "scheduling/com.second-brain.codex-capture.plist"
)


def _fake_python(tmp_path: Path, exit_code: int = 0) -> Path:
    """A stub interpreter that records the script it was asked to run."""
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/bash\n"
        f"if [[ \"$1\" == \"-c\" ]]; then exit {exit_code}; fi\n"
        "printf '%s\\n' \"$@\" > \"$CAPTURE_CALLS\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    return fake_python


def test_capture_job_processes_all_eligible_active_tasks_without_backfill(
    tmp_path: Path,
):
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "CAPTURE_CALLS": str(calls),
        "SECOND_BRAIN_PYTHON": str(_fake_python(tmp_path)),
    }

    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0
    assert calls.read_text(encoding="utf-8").splitlines() == [
        str(SCRIPT.parents[1] / "capture_codex.py"),
    ]
    assert "--backfill" not in calls.read_text(encoding="utf-8")


def test_capture_job_runs_on_bedrock_titan_embeddings_not_a_local_server(
    tmp_path: Path,
):
    """No local-embedding gate: the job must run, not silently declare success."""
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "CAPTURE_CALLS": str(calls),
        "SECOND_BRAIN_PYTHON": str(_fake_python(tmp_path)),
    }
    env.pop("OLLAMA_BASE_URL", None)

    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0
    assert "waiting_for_local_embedding" not in result.stdout
    assert calls.exists(), "the capture script must run even with no local embedding server"
    # No Ollama *code* (the header comment explains why it was removed).
    script = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(l for l in script.splitlines() if not l.lstrip().startswith("#"))
    assert "OLLAMA" not in code and "ollama" not in code
    assert "EMBEDDING_PROVIDER" not in code
    assert 'SECOND_BRAIN_PROFILE="${SECOND_BRAIN_PROFILE:-controlled}"' in code


def test_capture_job_fails_loudly_when_codex_home_is_missing(tmp_path: Path):
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "CAPTURE_CALLS": str(calls),
        "SECOND_BRAIN_PYTHON": str(_fake_python(tmp_path)),
        "CODEX_HOME": str(tmp_path / "absent"),
    }

    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "CODEX_HOME" in result.stderr
    assert not calls.exists()


def test_launch_agent_runs_general_capture_every_six_hours_without_backfill():
    with PLIST.open("rb") as handle:
        job = plistlib.load(handle)

    assert job["Label"] == "com.second-brain.codex-capture"
    assert job["StartInterval"] == 21600
    # R5: through the shared wrapper (lock, 2 h deadline, logs/jobs/codex-capture.json).
    assert job["ProgramArguments"] == [
        "/bin/bash",
        "/Users/kerrfir/second-brain/scripts/jobs/job_wrapper.sh",
        "/Users/kerrfir/second-brain/scripts/jobs/codex_capture.sh",
        "codex-capture",
    ]
    assert int(job["EnvironmentVariables"]["SECOND_BRAIN_JOB_TIMEOUT_CODEX_CAPTURE"]) >= 7200
    assert "--backfill" not in job["ProgramArguments"]
    assert "CODEX_CAPTURE_CANARY_TASK_ID" not in job.get("EnvironmentVariables", {})
    # The corpus must not reach Kiro or Fable: pin the privacy-compatible profile.
    assert job["EnvironmentVariables"]["SECOND_BRAIN_PROFILE"] == "controlled"
    for path_key in ("StandardOutPath", "StandardErrorPath", "WorkingDirectory"):
        assert job[path_key].startswith("/Users/kerrfir/second-brain")
