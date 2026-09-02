"""Behavior tests for the bounded local re-embedding job."""

import os
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/jobs/reembed_local.sh"


def test_reembed_job_invokes_one_bounded_batch(tmp_path: Path):
    calls = tmp_path / "calls"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/bash\nprintf '%s\\n' \"$@\" > \"$REEMBED_CALLS\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = {
        **os.environ,
        "REEMBED_CALLS": str(calls),
        "SECOND_BRAIN_PYTHON": str(fake_python),
        "SECOND_BRAIN_REEMBED_LIMIT": "17",
        "SECOND_BRAIN_REEMBED_LOCK": str(tmp_path / "lock"),
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
        str(SCRIPT.parents[1] / "reembed_memories.py"),
        "--limit",
        "17",
        "--batch-size",
        "32",
    ]
