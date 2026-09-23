"""Behavior tests for the two monitors that are the pipeline's safety net.

Both jobs exited 1 every night for weeks, and *because* they always failed nobody
read them — which is how Slack (Midway auth) and Outlook (grasp transport) stayed
broken for days. The failures were not the ones being reported:

* ``verify_credentials.sh`` ran under launchd with PATH=/usr/bin:/bin:/usr/sbin:/sbin
  and the AWS CLI lives at /usr/local/bin/aws, so ``aws`` exited 127 (command not
  found) and a *missing tool* was reported as an *expired credential*. The same run
  also failed the job on ``aws:sso-default`` — an interactive admin SSO session that
  lapses by design and that no unattended job uses.
* ``verify_liveness.sh`` was right that the two Quick channels were silent, but its
  thresholds (3 d / 5 d) were unreachable: the upstream Quick export had stopped, so
  no threshold over our own table could ever be satisfied or diagnose the cause.

So these tests pin the distinctions that make the monitors readable again: OK vs
EXPIRED vs TOOL-MISSING, REQUIRED vs ADVISORY, and ❌ vs ⚠ with an exit code that
follows only the ❌ column. Everything is stubbed — no AWS call, no MCP server, no
database, no Quick corpus is touched.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


REPO = Path(__file__).resolve().parents[1]
CREDENTIALS = REPO / "scripts/jobs/verify_credentials.sh"
LIVENESS = REPO / "scripts/jobs/verify_liveness.sh"


def _script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_aws(tmp_path: Path) -> Path:
    """Stub AWS CLI: exits 1 for any profile named in $FAKE_AWS_FAIL_PROFILES."""
    return _script(
        tmp_path / "aws",
        "#!/bin/bash\n"
        'for p in ${FAKE_AWS_FAIL_PROFILES:-}; do\n'
        '  for a in "$@"; do [[ "$a" == "$p" ]] && exit 1; done\n'
        "done\n"
        "exit 0\n",
    )


def _fake_python(tmp_path: Path, exit_code: int = 0) -> Path:
    """Stub interpreter for the grasp/slack heredoc probes — never imports src."""
    return _script(
        tmp_path / "python",
        f"#!/bin/bash\ncat >/dev/null\nexit {exit_code}\n",
    )


def _fake_notifier(tmp_path: Path) -> Path:
    """Stub osascript that records every notification the job raises."""
    return _script(
        tmp_path / "osascript",
        f'#!/bin/bash\necho "$@" >> "{tmp_path / "notifications"}"\nexit 0\n',
    )


def _notifications(tmp_path: Path) -> list[str]:
    log = tmp_path / "notifications"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _run_credentials(tmp_path: Path, *args: str, **env_overrides) -> subprocess.CompletedProcess:
    _fake_notifier(tmp_path)
    env = {
        **os.environ,
        # The notifier stub must win over /usr/bin/osascript; the script prepends its
        # own directories but appends the inherited PATH after them.
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "AWS_BIN": str(_fake_aws(tmp_path)),
        "SECOND_BRAIN_PYTHON": str(_fake_python(tmp_path)),
        "FAKE_AWS_FAIL_PROFILES": "",
        **env_overrides,
    }
    return subprocess.run(
        ["bash", str(CREDENTIALS), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _fake_psql(tmp_path: Path) -> Path:
    """Stub psql returning an age in days, chosen by substring match on the SQL.

    $FAKE_PSQL_AGES is JSON: {"__default__": 0, "'slack'": 99}. Written in Python so
    the SQL predicates (quotes, %, spaces) need no shell escaping.
    """
    return _script(
        tmp_path / "psql",
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        'rules = json.loads(os.environ.get("FAKE_PSQL_AGES", "{}"))\n'
        "sql = sys.argv[-1]\n"
        'age = rules.get("__default__", 0)\n'
        "for key, value in rules.items():\n"
        '    if key != "__default__" and key in sql:\n'
        "        age = value\n"
        "print(age)\n",
    )


def _quick_db(tmp_path: Path, age_days: float, name: str = "knowledge_v1.db") -> Path:
    """A stand-in for the upstream Quick sqlite file. Only its mtime is ever read."""
    db = tmp_path / name
    db.write_bytes(b"")  # never opened by the monitor — stat(2) only
    stamp = time.time() - age_days * 86400
    os.utime(db, (stamp, stamp))
    return db


def _run_liveness(tmp_path: Path, ages=None, **env_overrides) -> subprocess.CompletedProcess:
    _fake_notifier(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PSQL_BIN": str(_fake_psql(tmp_path)),
        "PG_ISREADY_BIN": str(_script(tmp_path / "pg_isready", "#!/bin/bash\nexit 0\n")),
        "FAKE_PSQL_AGES": json.dumps({"__default__": 0, **(ages or {})}),
        # A stub interpreter, so a missing QD_DB_PATH can never reach src/qd_profile.
        "SECOND_BRAIN_PYTHON": str(_fake_python(tmp_path)),
        # A fresh upstream stub unless the test supplies its own (minting it here
        # would otherwise reset a deliberately-aged file's mtime).
        "QD_DB_PATH": env_overrides.pop(
            "QD_DB_PATH", str(_quick_db(tmp_path, 0, "upstream-fresh.db"))
        ),
        **env_overrides,
    }
    return subprocess.run(
        ["bash", str(LIVENESS)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# --------------------------------------------------------------------------- #
# verify_credentials.sh
# --------------------------------------------------------------------------- #


def test_missing_tool_is_reported_distinctly_and_is_not_called_an_expired_credential(
    tmp_path: Path,
):
    """The regression that made this job cry wolf: exit 127 read as "EXPIRED"."""
    result = _run_credentials(tmp_path, AWS_BIN=str(tmp_path / "no-such-aws"))

    assert result.returncode == 0, result.stdout
    assert "tool not found on PATH" in result.stdout
    assert "check NOT RUN" in result.stdout
    assert "EXPIRED/BROKEN" not in result.stdout
    assert "❌" not in result.stdout
    # The hint must name the real cause so the next reader does not re-derive it.
    assert "PATH=/usr/bin:/bin:/usr/sbin:/sbin" in result.stdout
    assert _notifications(tmp_path) == []


def test_broken_required_credential_fails_the_job_and_notifies(tmp_path: Path):
    result = _run_credentials(tmp_path, FAKE_AWS_FAIL_PROFILES="second-brain-jobs")

    assert result.returncode != 0
    assert "❌ aws:second-brain-jobs EXPIRED/BROKEN" in result.stdout
    assert "=== REQUIRED EXPIRED/BROKEN: aws:second-brain-jobs ===" in result.stdout
    assert len(_notifications(tmp_path)) == 1


def test_expired_advisory_credential_warns_but_keeps_the_job_green(tmp_path: Path):
    """aws:sso-default is an interactive session; no unattended job uses it."""
    result = _run_credentials(tmp_path, FAKE_AWS_FAIL_PROFILES="default")

    assert result.returncode == 0, result.stdout
    assert "⚠ aws:sso-default expired" in result.stdout
    assert "advisory, NOT failing the job" in result.stdout
    assert "REQUIRED EXPIRED/BROKEN" not in result.stdout
    assert "aws sso login --sso-session kiro-admin-session" in result.stdout
    # Only REQUIRED breakage is worth waking someone up for.
    assert _notifications(tmp_path) == []


def test_a_broken_required_credential_still_fails_when_an_advisory_one_also_lapsed(
    tmp_path: Path,
):
    result = _run_credentials(tmp_path, FAKE_AWS_FAIL_PROFILES="default second-brain-jobs")

    assert result.returncode != 0
    assert "⚠ aws:sso-default expired" in result.stdout
    assert "❌ aws:second-brain-jobs EXPIRED/BROKEN" in result.stdout


def test_usr_local_bin_is_on_the_path_under_a_launchd_environment(tmp_path: Path):
    """launchd's PATH lacks /usr/local/bin, which is where the AWS CLI lives."""
    result = subprocess.run(
        ["/bin/bash", str(CREDENTIALS), "--print-tools"],
        env={"HOME": os.path.expanduser("~"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    tools = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    assert "/usr/local/bin" in tools["PATH"].split(":")
    if Path("/usr/local/bin/aws").exists():
        assert tools["aws"] != "MISSING", (
            "the AWS CLI must resolve under launchd's minimal PATH"
        )
    # `timeout` is Homebrew coreutils, not a macOS builtin: if it ever disappears
    # every check returns 127, so its resolution is worth reporting too.
    assert "timeout" in tools


# --------------------------------------------------------------------------- #
# verify_liveness.sh
# --------------------------------------------------------------------------- #


def test_all_channels_fresh_exits_zero(tmp_path: Path):
    result = _run_liveness(tmp_path)

    assert result.returncode == 0, result.stdout
    assert "=== All monitored channels live ===" in result.stdout
    assert "❌" not in result.stdout
    assert "✅ codex_task ok" in result.stdout
    assert "✅ quick_upstream ok" in result.stdout
    assert _notifications(tmp_path) == []


@pytest.mark.parametrize(
    "predicate,label",
    [
        ("'slack'", "slack"),
        ("'kiro_cli_chat'", "cli_chat"),
        ("'outlook_email'", "outlook_email"),
        # Codex capture went live 2026-09-22 and was unmonitored until now.
        ("'codex_task'", "codex_task"),
    ],
)
def test_a_silent_required_channel_fails_the_job_and_notifies(
    tmp_path: Path, predicate: str, label: str
):
    result = _run_liveness(tmp_path, ages={predicate: 99})

    assert result.returncode != 0, result.stdout
    assert f"❌ {label} SILENT — last capture 99d ago" in result.stdout
    assert f"=== SILENT CHANNELS: {label}(99d) ===" in result.stdout
    assert len(_notifications(tmp_path)) == 1


def test_the_retired_quick_doc_channel_stays_visible_without_failing(tmp_path: Path):
    """Dormant since 2026-06-04 — the loudest source of the old nightly false alarm."""
    result = _run_liveness(tmp_path, ages={"= 'quick_desktop_doc'": 400})

    assert result.returncode == 0, result.stdout
    assert "⚠ quick_desktop_doc retired" in result.stdout
    assert "400d ago" in result.stdout  # still reported, not silently deleted
    assert "❌" not in result.stdout
    assert _notifications(tmp_path) == []


def test_quick_synthesis_threshold_tolerates_the_observed_upstream_gaps(tmp_path: Path):
    """Observed inter-capture gaps are 11 d and 14 d; the old 5 d could never pass."""
    fresh = _run_liveness(tmp_path, ages={"LIKE 'quick_desktop_%'": 14})
    stalled = _run_liveness(tmp_path, ages={"LIKE 'quick_desktop_%'": 60})

    assert fresh.returncode == 0, fresh.stdout
    assert "✅ quick_desktop_synth ok (14d" in fresh.stdout
    # ...but a genuine multi-week ingest stall is still caught.
    assert stalled.returncode != 0
    assert "❌ quick_desktop_synth SILENT" in stalled.stdout


def test_a_stale_upstream_quick_export_is_reported_as_such_not_as_our_breakage(
    tmp_path: Path,
):
    """"Quick stopped exporting" is visible, and advisory: not ours to repair."""
    result = _run_liveness(tmp_path, QD_DB_PATH=str(_quick_db(tmp_path, 40)))

    assert result.returncode == 0, result.stdout
    assert "⚠ quick_upstream STALE — Quick has written nothing for 40d" in result.stdout
    assert "idle by cause, not broken" in result.stdout
    assert "advisory, NOT failing the job" in result.stdout
    assert _notifications(tmp_path) == []


def test_an_unresolvable_upstream_quick_store_is_our_bug_and_fails(tmp_path: Path):
    """A missing file means the profile moved and src/qd_profile.py needs updating."""
    result = _run_liveness(tmp_path, QD_DB_PATH=str(tmp_path / "gone" / "knowledge_v1.db"))

    assert result.returncode != 0
    assert "❌ quick_upstream UNRESOLVED" in result.stdout
    assert "src/qd_profile.py" in result.stdout
    assert len(_notifications(tmp_path)) == 1


def test_the_upstream_check_never_reads_the_quick_corpus(tmp_path: Path):
    """Someone's knowledge store: stat(2) only. A non-sqlite file must still pass."""
    db = tmp_path / "knowledge_v1.db"
    db.write_text("not a database", encoding="utf-8")
    os.utime(db, (time.time(), time.time()))

    result = _run_liveness(tmp_path, QD_DB_PATH=str(db))

    assert result.returncode == 0, result.stdout
    assert "✅ quick_upstream ok" in result.stdout
