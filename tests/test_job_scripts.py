"""scripts/jobs/*.sh adapters run with every external tool stubbed (no database,
no gpg, no network, no brew): backup.sh / verify_backup.sh wiring, the PostgreSQL
preflight (exit 69, never `brew services start`), and the dream-cycle /
weekly-digest / capture-api launchers.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
JOBS = REPO / "scripts/jobs"


def _script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _recorder(bin_dir: Path, name: str, log: Path, *, extra: str = "", rc: int = 0) -> Path:
    """A stub tool that appends its argv (JSON) to ``log`` and exits ``rc``."""
    return _script(
        bin_dir / name,
        f"#!/bin/sh\n{extra}{shlex.quote(sys.executable)} - {shlex.quote(name)} {shlex.quote(str(log))} \"$@\" <<'PYTHON'\n"
        "import json, sys\n"
        "name, log, *argv = sys.argv[1:]\n"
        "with open(log, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps([name, *argv]) + '\\n')\n"
        f"PYTHON\nexit {rc}\n",
    )


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


@pytest.fixture
def toolbox(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.jsonl"
    for tool in ("gpg", "rclone", "pg_dump", "pg_restore", "initdb", "pg_ctl"):
        _recorder(bin_dir, tool, log)
    _recorder(bin_dir, "brew", log)                      # must never be called
    _recorder(bin_dir, "pg_isready", log, rc=0)
    _recorder(bin_dir, "python", log, extra="")         # SECOND_BRAIN_PYTHON stand-in
    return {"bin": bin_dir, "log": log, "env": {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "SECOND_BRAIN_TOOL_PATH": str(bin_dir),
        "SECOND_BRAIN_PYTHON": str(bin_dir / "python"),
        "PG_ISREADY_BIN": str(bin_dir / "pg_isready"),
        "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
    }}


def _run(script: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", str(script), *args], env=env, capture_output=True, text=True, check=False)


# ---------------------------------------------------------------- preflight

@pytest.mark.parametrize("script", ["backup.sh", "dream_cycle_scheduled.sh", "weekly_digest.sh"])
def test_postgres_down_is_exit_69_with_a_named_cause_and_no_brew_autostart(toolbox, script):
    """E-N10/N11: the old scripts ran `brew services start postgresql@17` silently."""
    _recorder(toolbox["bin"], "pg_isready", toolbox["log"], rc=2)
    result = _run(JOBS / script, toolbox["env"])
    assert result.returncode == 69, result.stdout + result.stderr
    assert "PostgreSQL is not reachable" in result.stderr and "not auto-started" in result.stderr
    assert "brew services restart postgresql@17" in result.stderr        # the fix hint
    names = [c[0] for c in _calls(toolbox["log"])]
    assert "brew" not in names and "python" not in names and "pg_dump" not in names


def test_no_job_script_auto_starts_postgres():
    for script in JOBS.glob("*.sh"):
        code = "\n".join(l for l in script.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
        assert "brew services start" not in code, script.name


def test_preflight_honours_db_env_and_pg_isready_override(tmp_path, toolbox):
    env = {**toolbox["env"], "DB_HOST": "/tmp/sock", "DB_PORT": "5999", "DB_USER": "sb_x"}
    result = subprocess.run(["/bin/bash", "-c", f". {JOBS / '_preflight.sh'}; require_postgres; echo reached"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0 and "reached" in result.stdout
    (call,) = [c for c in _calls(toolbox["log"]) if c[0] == "pg_isready"]
    assert call[1:] == ["-h", "/tmp/sock", "-p", "5999", "-U", "sb_x"]


# ------------------------------------------------------------------ backup.sh

def test_backup_sh_delegates_to_the_bundle_tool_and_never_calls_rclone_itself(toolbox):
    result = _run(JOBS / "backup.sh", toolbox["env"], "--remote", "synthetic:x")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(toolbox["log"])
    (py,) = [c for c in calls if c[0] == "python"]
    assert py[1].endswith("scripts/maintenance/backup_bundle.py")
    assert py[2:] == ["create", "--repo", str(REPO), "--remote", "synthetic:x"]
    assert not [c for c in calls if c[0] in ("rclone", "rsync", "gpg")]
    text = (JOBS / "backup.sh").read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"^\s*rclone\b", code, re.M) and "rsync" not in code and "kb-sources" not in code


# ----------------------------------------------------------- verify_backup.sh

def _verify_env(toolbox, tmp_path, listing: str, bundle_rc: int = 0):
    """rclone lsf prints ``listing``; the python stub runs the REAL select-latest
    (stdlib only) and stubs verify / verify-legacy, recording their argv."""
    log = toolbox["log"]
    _recorder(toolbox["bin"], "rclone", log,
              extra=f'if [ "$1" = lsf ]; then printf %s {shlex.quote(listing)}; fi\n'
                    'if [ "$1" = copyto ]; then : > "$3"; fi\n')
    _script(toolbox["bin"] / "python",
            "#!/bin/bash\n"
            f'LOG={shlex.quote(str(log))}\n'
            'case " $* " in\n'
            f'  *" select-latest "*) exec {shlex.quote(sys.executable)} "$@" ;;\n'
            '  *" verify "*|*" verify-legacy "*)\n'
            f'     {shlex.quote(sys.executable)} -c \'import json,sys; print(json.dumps(["python", *sys.argv[1:]]))\' "$@" >> "$LOG"\n'
            f'     echo \'{{"payload_verified": true}}\'; exit {bundle_rc} ;;\n'
            f'  *) exec {shlex.quote(sys.executable)} "$@" ;;\n'
            'esac\n')
    return {**toolbox["env"], "SECOND_BRAIN_BACKUP_REMOTE": "synthetic:remote"}


def _stamp(hours_ago: float, kind: str) -> str:
    t = datetime.now(timezone.utc).timestamp() - hours_ago * 3600
    if kind == "bundle":
        return datetime.fromtimestamp(t, timezone.utc).strftime("second-brain_%Y%m%dT%H%M%S000000Z.tar.gpg")
    return datetime.fromtimestamp(t).strftime("memory_bank_%Y%m%d_%H%M%S.dump.gpg")


def test_verify_picks_the_newest_bundle_and_runs_the_full_restore(toolbox, tmp_path):
    newest = _stamp(5, 'bundle')
    listing = f"{_stamp(30, 'legacy')}\n{newest}\nmemories_20261001_020005.json.gpg\n"
    env = _verify_env(toolbox, tmp_path, listing)
    result = _run(JOBS / "verify_backup.sh", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Verification passed (bundle, fresh)" in result.stdout
    calls = _calls(toolbox["log"])
    (lsf,) = [c for c in calls if c[0] == "rclone" and c[1] == "lsf"]
    assert "--max-depth" in lsf and lsf[lsf.index("--include") + 1] == "*.gpg" and "--files-only" in lsf
    (copy,) = [c for c in calls if c[0] == "rclone" and c[1] == "copyto"]
    assert copy[2] == "synthetic:remote/" + newest
    (verify,) = [c for c in calls if c[0] == "python" and "verify" in c]
    assert verify[verify.index("verify") + 1].endswith(newest) and "--restore" in verify


def test_verify_falls_back_to_the_legacy_dump_check_when_the_newest_is_a_dump(toolbox, tmp_path):
    newest = _stamp(5, 'legacy')
    listing = f"{newest}\n{_stamp(40, 'bundle')}\n"
    env = _verify_env(toolbox, tmp_path, listing)
    result = _run(JOBS / "verify_backup.sh", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Verification passed (legacy_dump, fresh)" in result.stdout
    (verify,) = [c for c in _calls(toolbox["log"]) if c[0] == "python" and "verify-legacy" in c]
    assert verify[verify.index("verify-legacy") + 1].endswith(newest) and "--restore" not in verify


def test_verify_fails_when_the_newest_remote_backup_is_stale(toolbox, tmp_path):
    """E-N9: a Drive copy that stopped updating must not pass because it decrypts."""
    listing = f"{_stamp(50, 'bundle')}\n{_stamp(80, 'legacy')}\n"
    env = _verify_env(toolbox, tmp_path, listing)
    result = _run(JOBS / "verify_backup.sh", env)
    assert result.returncode == 1, result.stdout
    assert "freshness check failed" in result.stdout and '"fresh": false' in result.stdout
    assert not [c for c in _calls(toolbox["log"]) if c[0] == "rclone" and c[1] == "copyto"], "nothing downloaded when stale"
    assert _run(JOBS / "verify_backup.sh", env, "--max-age-hours", "100").returncode == 0


def test_verify_fails_when_the_remote_has_no_encrypted_backup_or_the_restore_fails(toolbox, tmp_path):
    env = _verify_env(toolbox, tmp_path, "config/\nkb-sources/\n")
    assert _run(JOBS / "verify_backup.sh", env).returncode == 1
    env = _verify_env(toolbox, tmp_path, f"{_stamp(2, 'bundle')}\n", bundle_rc=1)
    result = _run(JOBS / "verify_backup.sh", env)
    assert result.returncode == 1 and "VERIFICATION FAILED (bundle)" in result.stdout


# -------------------------------------------------------- dream-cycle launcher

def test_dream_cycle_launcher_execs_python_as_the_group_leader_with_the_run_profile(toolbox):
    result = _run(JOBS / "dream_cycle_scheduled.sh", toolbox["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    (py,) = [c for c in _calls(toolbox["log"]) if c[0] == "python"]
    assert py[1:] == ["scripts/dream_cycle_run.py", "--run-type", "scheduled"]
    code = "\n".join(l for l in (JOBS / "dream_cycle_scheduled.sh").read_text().splitlines() if not l.lstrip().startswith("#"))
    assert 'exec "${SECOND_BRAIN_PYTHON' in code, "bash must not stay between the wrapper's SIGTERM and the orchestrator"
    assert "express_push" not in code
    assert 'SECOND_BRAIN_RUN_COST_CAP_USD="${SECOND_BRAIN_RUN_COST_CAP_USD:-9}"' in code
    assert 'SECOND_BRAIN_PROFILE="${SECOND_BRAIN_PROFILE:-controlled}"' in code


# ------------------------------------------------------------ capture_api.sh

class TestCaptureApiLauncherFailsClosed:
    """The launcher must refuse to start the server when .env / the token is
    missing — never fall through to an unauthenticated service."""

    def _run_launcher_in(self, tmp_path: Path, env_body: str | None):
        script = (JOBS / "capture_api.sh").read_text().replace(
            'REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"', f'REPO_ROOT="{tmp_path}"')
        (tmp_path / "run.sh").write_text(script)
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        stub = tmp_path / ".venv" / "bin" / "python"
        stub.write_text('#!/bin/bash\necho "STARTED with token set: ${CAPTURE_API_TOKEN:+yes} args: $*"\n')
        stub.chmod(0o755)
        if env_body is not None:
            (tmp_path / ".env").write_text(env_body)
        env = {k: v for k, v in os.environ.items() if k != "SECOND_BRAIN_PYTHON"}
        return subprocess.run(["bash", str(tmp_path / "run.sh")], capture_output=True, text=True, env={**env, "CAPTURE_API_TOKEN": ""})

    def test_missing_env_file_exits_nonzero(self, tmp_path):
        res = self._run_launcher_in(tmp_path, env_body=None)
        assert res.returncode == 78 and "not found" in res.stderr
        assert "STARTED" not in res.stdout

    def test_env_without_token_exits_nonzero(self, tmp_path):
        res = self._run_launcher_in(tmp_path, env_body="OTHER=1\n")
        assert res.returncode == 78 and "not set" in res.stderr

    def test_env_with_token_starts_server_with_token_exported(self, tmp_path):
        res = self._run_launcher_in(tmp_path, env_body="CAPTURE_API_TOKEN=abc123\n")
        assert res.returncode == 0
        assert "STARTED with token set: yes args: -m src.capture_api" in res.stdout
