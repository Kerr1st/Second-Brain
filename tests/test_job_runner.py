"""Scheduler boundary (src/operations/jobs.py) exercised with local synthetic commands only.

No launchd, no osascript (SECOND_BRAIN_NOTIFY=0), no caffeinate, no database.
"""
import fcntl
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.operations.jobs as jobs
from src.operations.jobs import (DEFAULT_FLOOR_S, DEFAULT_TIMEOUT_S, TIMEOUT_FLOORS, resolve_timeout, run_job,
                                 service_backoff, should_notify, timeout_env_name)
from src.operations.liveness import inspect_statuses


@pytest.fixture
def job_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PYTHON", sys.executable)
    monkeypatch.setenv("SECOND_BRAIN_CAFFEINATE", "0")
    monkeypatch.setenv("SECOND_BRAIN_NOTIFY", "0")
    monkeypatch.setenv("SECOND_BRAIN_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path / "status"))
    monkeypatch.setenv("SECOND_BRAIN_JOB_GRACE_S", "1")
    for key in list(os.environ):
        if key.startswith("SECOND_BRAIN_JOB_TIMEOUT"):
            monkeypatch.delenv(key)
    return tmp_path


def script(repo, content, name="job.py"):
    path = repo / name
    path.write_text(content)
    path.chmod(0o700)
    return path


def status(repo, name="test"):
    return json.loads((repo / f"status/{name}.json").read_text())


# --------------------------------------------------------------------- basics

def test_environment_cwd_args_and_exit_reach_durable_status(job_env):
    repo = job_env
    path = script(repo, "import os,sys,json; print(json.dumps({'cwd':os.getcwd(),'path':os.environ['PATH'],"
                        "'profile':os.environ['AWS_PROFILE'],'sb':os.environ['SECOND_BRAIN_PROFILE'],"
                        "'umask':oct(os.umask(0)),'args':sys.argv[1:]})); sys.exit(7)")
    assert run_job(str(path), "test", ["a b", "--flag"], repo=repo, timeout=5) == 7
    value = status(repo)
    assert value["state"] == "failed" and value["exit_code"] == 7
    assert value["consecutive_failures"] == 1 and value["last_success_at"] is None
    log = next((repo / "logs").glob("*.log")).read_text()
    record = json.loads(next(line for line in log.splitlines() if line.startswith("{")))
    assert record["cwd"] == str(repo)
    assert "/usr/local/bin" in record["path"] and "postgresql@17/bin" in record["path"] and "/opt/homebrew/bin" in record["path"]
    assert record["profile"] == "second-brain-jobs" and record["sb"] == "controlled"
    assert record["umask"] == "0o77"
    assert record["args"] == ["a b", "--flag"]
    assert (repo / "status/test.json").stat().st_mode & 0o077 == 0


def test_success_resets_failure_counters_and_records_last_success(job_env):
    path = script(job_env, "raise SystemExit(2)")
    run_job(str(path), "test", repo=job_env, timeout=5)
    first = status(job_env)["first_failure_at"]
    run_job(str(path), "test", repo=job_env, timeout=5)
    assert status(job_env)["consecutive_failures"] == 2
    assert status(job_env)["first_failure_at"] == first
    path.write_text("pass")
    assert run_job(str(path), "test", repo=job_env, timeout=5) == 0
    assert status(job_env)["first_failure_at"] is None
    assert status(job_env)["consecutive_failures"] == 0
    assert status(job_env)["last_success_at"] and status(job_env)["state"] == "succeeded"


def test_locked_job_does_not_replace_running_status(job_env):
    directory = job_env / "status"
    directory.mkdir()
    (directory / "test.json").write_text('{"original": true}')
    with (directory / "test.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_job("absent.py", "test", repo=job_env, timeout=1) == 75
    assert status(job_env) == {"original": True}


def test_missing_script_is_a_recorded_launch_failure(job_env):
    assert run_job("absent.py", "test", repo=job_env, timeout=1) == 127
    assert status(job_env)["state"] == "failed" and status(job_env)["exit_code"] == 127


def test_shell_scripts_run_directly_and_python_under_the_venv_interpreter(job_env):
    sh = script(job_env, "#!/bin/bash\necho SHELL_RAN; exit 0\n", name="job.sh")
    assert run_job(str(sh), "test", repo=job_env, timeout=5) == 0
    assert "SHELL_RAN" in next((job_env / "logs").glob("*.log")).read_text()


# ------------------------------------------------------------------ timeouts

def test_timeout_kills_job_and_fails(job_env):
    path = script(job_env, "import time; time.sleep(30)")
    assert run_job(str(path), "test", repo=job_env, timeout=.1) == 124
    assert status(job_env)["exit_code"] == 124 and status(job_env)["timed_out"] is True


def test_timeout_sends_sigterm_and_waits_the_grace_period_before_sigkill(job_env, monkeypatch):
    """The dream-cycle orchestrator closes its run row on SIGTERM (R3 RunInterrupted).
    The wrapper must give it time: TERM → grace → KILL, not TERM → 5 s → KILL."""
    monkeypatch.setenv("SECOND_BRAIN_JOB_GRACE_S", "8")
    path = script(job_env, "import signal,sys,time\nfrom pathlib import Path\n"
                           "def term(*_):\n    time.sleep(2); Path('completed.marker').write_text('run row closed'); sys.exit(0)\n"
                           "signal.signal(signal.SIGTERM, term)\n"
                           "Path('started.marker').write_text('x')\n"
                           "time.sleep(60)\n")
    started = time.monotonic()
    assert run_job(str(path), "test", repo=job_env, timeout=0.5) == 124
    assert (job_env / "completed.marker").read_text() == "run row closed"
    assert time.monotonic() - started < 8  # finished at the child's pace, well inside the grace
    value = status(job_env)
    assert value["timed_out"] is True and value["stopped_gracefully"] is True and value["grace_s"] == 8.0
    assert "grace 8s" in next((job_env / "logs").glob("*.log")).read_text()


def test_grace_waits_for_the_whole_process_group_not_just_the_leader(job_env, monkeypatch):
    """A bash leader dies on SIGTERM instantly; the Python grandchild it started is the
    one that needs the grace. Killing the group as soon as the leader is reaped would
    orphan the run row."""
    monkeypatch.setenv("SECOND_BRAIN_JOB_GRACE_S", "8")
    child = script(job_env, "import signal,sys,time\nfrom pathlib import Path\n"
                            "def term(*_):\n    time.sleep(2); Path('grandchild.marker').write_text('closed'); sys.exit(0)\n"
                            "signal.signal(signal.SIGTERM, term)\ntime.sleep(60)\n", name="child.py")
    leader = script(job_env, f"#!/bin/bash\n'{sys.executable}' '{child}'\n", name="leader.sh")  # no exec: bash stays the leader
    assert run_job(str(leader), "test", repo=job_env, timeout=0.5) == 124
    assert (job_env / "grandchild.marker").read_text() == "closed"
    assert status(job_env)["stopped_gracefully"] is True


def test_term_ignoring_child_is_killed_after_the_grace(job_env, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_JOB_GRACE_S", "1")
    path = script(job_env, "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                           "Path('child.pid').write_text(str(os.getpid())); time.sleep(60)")
    started = time.monotonic()
    assert run_job(str(path), "test", repo=job_env, timeout=0.5) == 124
    assert 1.0 <= time.monotonic() - started < 8
    assert status(job_env)["stopped_gracefully"] is False
    with pytest.raises(ProcessLookupError):
        os.kill(int((job_env / "child.pid").read_text()), 0)


@pytest.mark.parametrize("name", sorted(TIMEOUT_FLOORS))
def test_default_timeout_never_applies_to_jobs_with_a_documented_floor(name):
    """E-N1: dream-cycle (observed 92 and 204 min) and bookmarks (64 min) would have
    been SIGKILLed by the 3600 s default. Without any env the runner must use the
    floor, never the default."""
    seconds, source = resolve_timeout(name, {})
    assert seconds == TIMEOUT_FLOORS[name] and seconds > DEFAULT_TIMEOUT_S
    assert source == "default-raised-to-floor"


def test_default_timeout_applies_to_short_jobs_and_floor_table_is_sane():
    assert resolve_timeout("capture-slack", {}) == (DEFAULT_TIMEOUT_S, "default")
    assert TIMEOUT_FLOORS["dream-cycle"] >= 6 * 3600 and TIMEOUT_FLOORS["bookmarks"] >= 3 * 3600
    assert TIMEOUT_FLOORS["backup"] >= 2 * 3600 and TIMEOUT_FLOORS["verify"] >= 2 * 3600
    assert DEFAULT_FLOOR_S == DEFAULT_TIMEOUT_S


def test_generic_timeout_is_raised_to_the_floor_but_explicit_per_job_wins():
    assert resolve_timeout("dream-cycle", {"SECOND_BRAIN_JOB_TIMEOUT": "1800"}) == (TIMEOUT_FLOORS["dream-cycle"], "generic-raised-to-floor")
    assert resolve_timeout("dream-cycle", {"SECOND_BRAIN_JOB_TIMEOUT": "30000"}) == (30000.0, "generic")
    assert resolve_timeout("dream-cycle", {"SECOND_BRAIN_JOB_TIMEOUT_DREAM_CYCLE": "600"}) == (600.0, "per-job")
    assert resolve_timeout("dream-cycle", {"SECOND_BRAIN_JOB_TIMEOUT_DREAM_CYCLE": "600"}, explicit=5) == (5.0, "argument")
    assert timeout_env_name("chat-capture") == "SECOND_BRAIN_JOB_TIMEOUT_CHAT_CAPTURE"
    with pytest.raises(ValueError):
        resolve_timeout("backup", {"SECOND_BRAIN_JOB_TIMEOUT_BACKUP": "0"})


def test_dream_cycle_run_without_env_records_the_floor_not_the_default(job_env):
    path = script(job_env, "pass")
    assert run_job(str(path), "dream-cycle", repo=job_env) == 0
    value = status(job_env, "dream-cycle")
    assert value["timeout_s"] == TIMEOUT_FLOORS["dream-cycle"] and value["timeout_source"] == "default-raised-to-floor"


# ------------------------------------------------------------------- signals

@pytest.mark.parametrize("service", [False, True])
def test_signal_stops_term_ignoring_child_before_job_deadline(job_env, monkeypatch, service):
    monkeypatch.setenv("SECOND_BRAIN_HEALTH_INTERVAL", ".01")
    monkeypatch.setattr(jobs, "_healthy", lambda *_: True)
    path = script(job_env, "import os,signal,time; from pathlib import Path; "
                           "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                           "Path('child.pid').write_text(str(os.getpid())); "
                           "os.kill(os.getppid(), signal.SIGTERM); time.sleep(60)")
    started = time.monotonic()
    assert run_job(str(path), "test", repo=job_env, timeout=60, service=service) == 143
    assert time.monotonic() - started < 12
    value = status(job_env)
    assert value["state"] == "interrupted" and value["interrupted_by"] == signal.SIGTERM
    assert inspect_statuses(job_env / "status")[0]["state"] == "interrupted"
    with pytest.raises(ProcessLookupError):
        os.kill(int((job_env / "child.pid").read_text()), 0)


def test_child_cleanup_tolerates_process_exit_race(monkeypatch):
    from unittest.mock import Mock
    child = Mock(pid=123)
    child.poll.return_value = 0
    monkeypatch.setattr(os, "killpg", Mock(side_effect=ProcessLookupError()))
    assert jobs._stop_child(child, grace=0.2) is True


# ------------------------------------------------------------------- service

def test_service_startup_is_bounded_and_unhealthy_is_not_success(job_env, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_HEALTH_INTERVAL", ".01")
    monkeypatch.setenv("SECOND_BRAIN_SERVICE_BACKOFF_MAX_S", "0.01")
    monkeypatch.setattr(jobs, "_healthy", lambda *_: False)
    path = script(job_env, "import time; time.sleep(30)")
    assert run_job(str(path), "test", repo=job_env, timeout=.03, service=True) == 124
    assert status(job_env)["state"] == "failed" and status(job_env)["timeout_source"] == "service-startup"


def test_service_has_no_deadline_while_healthy(job_env, monkeypatch):
    """A batch deadline of 0.2 s must not stop a service that keeps answering its probe."""
    monkeypatch.setenv("SECOND_BRAIN_HEALTH_INTERVAL", ".02")
    monkeypatch.setenv("SECOND_BRAIN_JOB_TIMEOUT_TEST", "0.2")
    monkeypatch.setattr(jobs, "_healthy", lambda *_: True)
    path = script(job_env, "import time; time.sleep(1.5)")
    started = time.monotonic()
    assert run_job(str(path), "test", repo=job_env, service=True) == 1  # exited on its own = failure
    assert time.monotonic() - started >= 1.4
    assert status(job_env)["heartbeat_at"]


def test_service_heartbeats_then_health_failure_is_durable(job_env, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_HEALTH_INTERVAL", ".01")
    monkeypatch.setenv("SECOND_BRAIN_HEALTH_FAILURES", "2")
    monkeypatch.setenv("SECOND_BRAIN_SERVICE_BACKOFF_MAX_S", "0.01")
    health = iter([True, False, False])
    monkeypatch.setattr(jobs, "_healthy", lambda *_: next(health))
    path = script(job_env, "import time; time.sleep(30)")
    assert run_job(str(path), "test", repo=job_env, timeout=.5, service=True) == 1
    value = status(job_env)
    assert value["heartbeat_at"] and value["last_success_at"]
    assert value["state"] == "failed" and value["exit_code"] == 1


def test_service_unexpected_normal_exit_is_failure(job_env, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_HEALTH_INTERVAL", ".01")
    monkeypatch.setenv("SECOND_BRAIN_SERVICE_BACKOFF_MAX_S", "0.01")
    monkeypatch.setattr(jobs, "_healthy", lambda *_: True)
    path = script(job_env, "pass")
    assert run_job(str(path), "test", repo=job_env, timeout=1, service=True) == 1


def test_running_service_needs_fresh_health_not_batch_duration(tmp_path):
    now = datetime.now(timezone.utc)
    value = {"job": "api", "state": "running", "service": True, "started_at": (now - timedelta(days=2)).isoformat(),
             "heartbeat_at": now.isoformat(), "timeout_s": 60, "health_interval_s": 30}
    (tmp_path / "api.json").write_text(json.dumps(value))
    assert inspect_statuses(tmp_path, now=now)[0]["healthy"] is True
    value["heartbeat_at"] = (now - timedelta(minutes=10)).isoformat()
    (tmp_path / "api.json").write_text(json.dumps(value))
    assert inspect_statuses(tmp_path, now=now)[0]["state"] == "stale_service_heartbeat"


def test_unexpected_service_monitor_error_stops_child_and_records_failure(job_env, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_SERVICE_BACKOFF_MAX_S", "0.01")

    def broken_health(*_):
        deadline = time.monotonic() + 5
        while not (job_env / "child.pid").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        raise RuntimeError("synthetic monitor failure")
    monkeypatch.setattr(jobs, "_healthy", broken_health)
    path = script(job_env, "import os,time; from pathlib import Path; Path('child.pid').write_text(str(os.getpid())); time.sleep(60)")
    assert run_job(str(path), "test", repo=job_env, timeout=60, service=True) == 70
    value = status(job_env)
    assert value["state"] == "failed" and value["error_type"] == "RuntimeError"
    with pytest.raises(ProcessLookupError):
        os.kill(int((job_env / "child.pid").read_text()), 0)


def test_service_waits_briefly_for_the_lock_instead_of_failing_on_restart(job_env, monkeypatch):
    """KeepAlive restarts the wrapper the instant the previous one exits; a still-draining
    predecessor must not turn every restart into exit 75 → another restart."""
    import threading
    monkeypatch.setenv("SECOND_BRAIN_HEALTH_INTERVAL", ".01")
    monkeypatch.setenv("SECOND_BRAIN_SERVICE_LOCK_WAIT_S", "5")
    monkeypatch.setenv("SECOND_BRAIN_SERVICE_BACKOFF_MAX_S", "0.01")
    monkeypatch.setattr(jobs, "_healthy", lambda *_: True)
    directory = job_env / "status"
    directory.mkdir()
    handle = (directory / "test.lock").open("w")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    threading.Timer(0.5, lambda: (fcntl.flock(handle, fcntl.LOCK_UN), handle.close())).start()
    path = script(job_env, "pass")
    assert run_job(str(path), "test", repo=job_env, timeout=1, service=True) == 1
    assert status(job_env)["state"] == "failed"  # it ran (and the no-op service exiting is a failure)


def test_batch_job_does_not_wait_for_the_lock(job_env):
    directory = job_env / "status"
    directory.mkdir()
    with (directory / "test.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        assert run_job("absent.py", "test", repo=job_env, timeout=1, service=False) == 75
        assert time.monotonic() - started < 2


def test_service_backoff_grows_and_is_capped():
    assert service_backoff(0) == 0
    assert [service_backoff(n) for n in (1, 2, 3, 4)] == [10, 20, 40, 80]
    assert service_backoff(20) == 300 and service_backoff(20, maximum=60) == 60


def test_failed_service_run_pauses_before_exit_so_keepalive_restarts_back_off(job_env, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_HEALTH_INTERVAL", ".01")
    monkeypatch.setattr(jobs, "_healthy", lambda *_: False)
    pauses = []
    monkeypatch.setattr(jobs, "_pause", pauses.append)
    path = script(job_env, "import time; time.sleep(30)")
    for expected in (10.0, 20.0, 40.0):
        assert run_job(str(path), "test", repo=job_env, timeout=.03, service=True) == 124
    assert pauses == [10.0, 20.0, 40.0]
    path.write_text("import time; time.sleep(30)")
    pauses.clear()
    assert run_job(str(path), "test", repo=job_env, timeout=.03, service=False) == 124
    assert pauses == []  # batch jobs never pause


def test_service_failures_notify_at_most_once_per_interval(job_env, monkeypatch):
    """E-N11: a DB outage makes the service fail → launchd restarts → fail … The
    wrapper must not raise an osascript notification on every cycle."""
    monkeypatch.setenv("SECOND_BRAIN_HEALTH_INTERVAL", ".01")
    monkeypatch.setenv("SECOND_BRAIN_NOTIFY", "1")
    monkeypatch.setenv("SECOND_BRAIN_NOTIFY_INTERVAL_S", "3600")
    monkeypatch.setenv("SECOND_BRAIN_SERVICE_BACKOFF_MAX_S", "0.01")
    monkeypatch.setattr(jobs, "_healthy", lambda *_: False)
    calls = []
    monkeypatch.setattr(jobs.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    path = script(job_env, "import time; time.sleep(30)")
    for _ in range(3):
        assert run_job(str(path), "test", repo=job_env, timeout=.03, service=True) == 124
    assert len([c for c in calls if c[0] == "osascript"]) == 1
    assert status(job_env)["last_notified_at"]
    # Batch jobs keep notifying on every failure (unchanged behaviour).
    for _ in range(2):
        run_job(str(path), "batch", repo=job_env, timeout=.03, service=False)
    assert len([c for c in calls if c[0] == "osascript"]) == 3


def test_should_notify_respects_interval_and_tolerates_missing_or_bad_stamps():
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    assert should_notify({}, now, 1800) is True
    assert should_notify({"last_notified_at": None}, now, 1800) is True
    assert should_notify({"last_notified_at": "garbage"}, now, 1800) is True
    assert should_notify({"last_notified_at": (now - timedelta(minutes=10)).isoformat()}, now, 1800) is False
    assert should_notify({"last_notified_at": (now - timedelta(minutes=31)).isoformat()}, now, 1800) is True


# ------------------------------------------------------------------- liveness glue

def test_liveness_reports_persistent_failure_and_lost_running_process(tmp_path):
    now = datetime.now(timezone.utc)
    (tmp_path / "failed.json").write_text(json.dumps({"job": "bad", "state": "failed", "exit_code": 1,
                                                       "first_failure_at": (now - timedelta(days=3)).isoformat()}))
    (tmp_path / "running.json").write_text(json.dumps({"job": "lost", "state": "running",
                                                        "started_at": (now - timedelta(hours=3)).isoformat(), "timeout_s": 60}))
    results = inspect_statuses(tmp_path, now=now)
    assert results[0]["persistent"] is True and results[0]["healthy"] is False
    assert results[1]["state"] == "overdue_running" and results[1]["healthy"] is False


def test_main_derives_job_name_from_script_stem(job_env, monkeypatch):
    path = script(job_env, "pass", name="qd_sync.py")
    monkeypatch.setattr(jobs, "run_job", lambda s, n, a, **k: (s, n, list(a)))
    assert jobs.main([str(path)]) == (str(path), "qd_sync", [])
    assert jobs.main([str(path), "qd-sync", "--dry-run"]) == (str(path), "qd-sync", ["--dry-run"])
