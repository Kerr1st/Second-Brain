"""scheduling/*.plist contracts (E-N1, E-N4, E-N5, F-034).

Every committed plist is what launchd will load (15 are symlinked straight from
this directory; the runbook replaces the two remaining copies with symlinks), so
the per-job deadline, the wrapper routing and the existence of every target are
pinned here rather than discovered at 02:00.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from src.operations.jobs import DEFAULT_GRACE_S, TIMEOUT_FLOORS, timeout_env_name, timeout_floor

REPO = Path(__file__).resolve().parents[1]
INSTALLED_ROOT = "/Users/kerrfir/second-brain"
SCHEDULING = REPO / "scheduling"
WRAPPER = f"{INSTALLED_ROOT}/scripts/jobs/job_wrapper.sh"

EXPECTED_LABELS = {
    "backup", "bookmarks", "capture-api", "capture-browser", "capture-outlook", "capture-slack", "chat-extract",
    "codex-capture", "credentials", "distill", "dream-cycle", "ingest", "liveness", "qd-sync", "verify",
    "weekly-digest", "youtube",
}
# label → wrapper job name (4th ProgramArguments element) → status file logs/jobs/<name>.json
JOB_NAMES = {
    "backup": "backup", "bookmarks": "bookmarks", "capture-api": "capture-api", "capture-browser": "capture-browser",
    "capture-outlook": "capture-outlook", "capture-slack": "capture-slack", "chat-extract": "chat-capture",
    "codex-capture": "codex-capture", "credentials": "credentials", "distill": "distill", "dream-cycle": "dream-cycle",
    "ingest": "crawlee-ingest", "liveness": "liveness", "qd-sync": "qd-sync", "verify": "verify",
    "weekly-digest": "weekly-digest", "youtube": "youtube",
}


def _plists() -> dict[str, dict]:
    out = {}
    for path in sorted(SCHEDULING.glob("com.second-brain.*.plist")):
        with path.open("rb") as fh:
            out[path.name.removeprefix("com.second-brain.").removesuffix(".plist")] = plistlib.load(fh)
    return out


PLISTS = _plists()


def _repo_path(installed: str) -> Path:
    assert installed.startswith(INSTALLED_ROOT + "/"), installed
    return REPO / installed[len(INSTALLED_ROOT) + 1:]


def test_exactly_the_installed_label_set_is_committed_and_codex_canary_is_gone():
    assert set(PLISTS) == EXPECTED_LABELS
    assert not (SCHEDULING / "com.second-brain.codex-canary.plist").exists()
    assert not (REPO / "scripts/jobs/codex_capture_canary.sh").exists()
    for label, plist in PLISTS.items():
        assert plist["Label"] == f"com.second-brain.{label}"
        assert "/path/to/" not in str(plist)


@pytest.mark.parametrize("label", sorted(EXPECTED_LABELS))
def test_every_plist_runs_through_the_wrapper_with_its_job_name(label):
    args = PLISTS[label]["ProgramArguments"]
    assert args[:2] == ["/bin/bash", WRAPPER], f"{label}: {args}"
    assert args[3] == JOB_NAMES[label], f"{label}: job name {args[3]!r}"
    assert len(args) == 4


@pytest.mark.parametrize("label", sorted(EXPECTED_LABELS))
def test_every_program_argument_target_exists_in_the_repo(label):
    for arg in PLISTS[label]["ProgramArguments"]:
        if arg.startswith(INSTALLED_ROOT + "/"):
            target = _repo_path(arg)
            assert target.is_file(), f"{label}: {arg} → {target} missing"
            if target.suffix == ".sh":
                assert target.stat().st_mode & 0o100, f"{label}: {target} not executable"


@pytest.mark.parametrize("label", sorted(EXPECTED_LABELS - {"capture-api"}))
def test_every_batch_plist_defines_a_timeout_at_or_above_its_documented_floor(label):
    """E-N1: the 3600 s default killed nothing only because no plist used the new
    wrapper yet. Every batch job states its deadline, and the long ones state one
    at least as large as the floor derived from observed durations."""
    name = JOB_NAMES[label]
    env = PLISTS[label].get("EnvironmentVariables", {})
    key = timeout_env_name(name)
    assert key in env, f"{label}: {key} missing from EnvironmentVariables"
    assert int(env[key]) >= timeout_floor(name), f"{label}: {env[key]} < floor {timeout_floor(name)}"
    assert "SECOND_BRAIN_JOB_SERVICE" not in env


def test_long_jobs_have_floors_well_above_the_observed_maximum():
    env = {label: PLISTS[label]["EnvironmentVariables"] for label in PLISTS}
    assert int(env["dream-cycle"]["SECOND_BRAIN_JOB_TIMEOUT_DREAM_CYCLE"]) >= 6 * 3600    # observed 204 min
    assert int(env["bookmarks"]["SECOND_BRAIN_JOB_TIMEOUT_BOOKMARKS"]) >= 3 * 3600         # observed 64 min
    assert int(env["backup"]["SECOND_BRAIN_JOB_TIMEOUT_BACKUP"]) >= 2 * 3600               # observed 31 min
    assert int(env["verify"]["SECOND_BRAIN_JOB_TIMEOUT_VERIFY"]) >= 2 * 3600               # full restore + 1.1 GB download
    assert set(TIMEOUT_FLOORS) <= set(JOB_NAMES.values())


def test_dream_cycle_keeps_mon_wed_fri_0200_and_gives_launchd_time_for_the_grace():
    plist = PLISTS["dream-cycle"]
    assert plist["StartCalendarInterval"] == [{"Weekday": d, "Hour": 2, "Minute": 0} for d in (1, 3, 5)]
    grace = float(plist["EnvironmentVariables"].get("SECOND_BRAIN_JOB_GRACE_S", DEFAULT_GRACE_S))
    # launchd SIGKILLs the job ExitTimeOut seconds after bootout; the wrapper's
    # TERM → grace → KILL must fit inside it or the orchestrator's RunInterrupted
    # path is cut short on every reload.
    assert plist["ExitTimeOut"] >= grace + 20


def test_capture_api_is_a_wrapped_keepalive_service_without_a_batch_deadline():
    plist = PLISTS["capture-api"]
    env = plist["EnvironmentVariables"]
    assert plist["KeepAlive"] is True
    assert env["SECOND_BRAIN_JOB_SERVICE"] == "1"
    assert env["SECOND_BRAIN_HEALTH_URL"].startswith("http://127.0.0.1:")
    assert float(env["SECOND_BRAIN_SERVICE_STARTUP_TIMEOUT_S"]) >= 60
    assert float(env["SECOND_BRAIN_NOTIFY_INTERVAL_S"]) >= 600
    assert not any(k.startswith("SECOND_BRAIN_JOB_TIMEOUT") for k in env), "a service has no batch deadline"
    assert "CAPTURE_API_TOKEN" not in env


def test_qd_sync_and_codex_capture_keep_their_cadence_and_the_jobs_profile():
    qd = PLISTS["qd-sync"]
    assert qd["StartInterval"] == 3600 and qd["RunAtLoad"] is False
    codex = PLISTS["codex-capture"]
    assert codex["StartInterval"] == 21600 and codex["RunAtLoad"] is False
    # E-N4: the adapter relies on the wrapper for AWS_PROFILE; the plist states it too.
    assert codex["EnvironmentVariables"]["AWS_PROFILE"] == "second-brain-jobs"
    assert codex["EnvironmentVariables"]["SECOND_BRAIN_PROFILE"] == "controlled"
    assert codex["EnvironmentVariables"]["CODEX_HOME"] == "/Users/kerrfir/.codex"


def test_schedules_match_the_readme_table():
    """scheduling/README.md is regenerated from the plists; the cadence column must agree."""
    readme = (SCHEDULING / "README.md").read_text(encoding="utf-8")
    for label in EXPECTED_LABELS:
        assert f"`com.second-brain.{label}`" in readme, label
    assert "Mon/Wed/Fri 02:00" in readme
    for label, plist in PLISTS.items():
        for key in plist.get("EnvironmentVariables", {}):
            if key.startswith("SECOND_BRAIN_JOB_TIMEOUT_"):
                assert plist["EnvironmentVariables"][key] in readme, f"{label}: timeout value not documented"
    assert "SECOND_BRAIN_JOB_TIMEOUT_<NAME>" in readme and "SECOND_BRAIN_JOB_GRACE_S" in readme
