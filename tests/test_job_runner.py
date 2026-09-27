"""Scheduler boundary exercised with local synthetic commands only."""
import fcntl
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.operations.jobs import run_job
from src.operations.liveness import inspect_statuses


@pytest.fixture
def job_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PYTHON", sys.executable)
    monkeypatch.setenv("SECOND_BRAIN_CAFFEINATE", "0")
    monkeypatch.setenv("SECOND_BRAIN_NOTIFY", "0")
    monkeypatch.setenv("SECOND_BRAIN_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SECOND_BRAIN_STATUS_DIR", str(tmp_path / "status"))
    return tmp_path


def script(repo, content):
    path = repo / "job.py"
    path.write_text(content)
    return path


def status(repo):
    return json.loads((repo / "status/test.json").read_text())


def test_environment_cwd_args_and_exit_reach_durable_status(job_env):
    repo = job_env
    path = script(repo, "import os,sys,json; print(json.dumps({'cwd':os.getcwd(),'path':os.environ['PATH'],'profile':os.environ['AWS_PROFILE'],'args':sys.argv[1:]})); sys.exit(7)")
    assert run_job(str(path), "test", ["a b", "--flag"], repo=repo, timeout=5) == 7
    value = status(repo)
    assert value["state"] == "failed" and value["exit_code"] == 7
    assert value["consecutive_failures"] == 1 and value["last_success_at"] is None
    log = next((repo / "logs").glob("*.log")).read_text()
    record = json.loads(next(line for line in log.splitlines() if line.startswith("{")))
    assert record['cwd'] == str(repo)
    assert '/usr/local/bin' in record['path'] and 'postgresql@17/bin' in record['path']
    assert record['args'] == ['a b','--flag']


def test_timeout_kills_job_and_fails(job_env):
    path = script(job_env, "import time; time.sleep(30)")
    assert run_job(str(path), "test", repo=job_env, timeout=.1) == 124
    assert status(job_env)['exit_code'] == 124


def test_locked_job_does_not_replace_running_status(job_env):
    directory = job_env / 'status'; directory.mkdir()
    (directory/'test.json').write_text('{"original": true}')
    with (directory/'test.lock').open('w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_job('absent.py','test',repo=job_env,timeout=1) == 75
    assert status(job_env) == {'original':True}


def test_failure_age_preserved_until_success(job_env):
    path = script(job_env, "raise SystemExit(2)")
    run_job(str(path),'test',repo=job_env,timeout=5)
    first = status(job_env)['first_failure_at']
    run_job(str(path),'test',repo=job_env,timeout=5)
    assert status(job_env)['consecutive_failures'] == 2
    assert status(job_env)['first_failure_at'] == first
    path.write_text('pass')
    run_job(str(path),'test',repo=job_env,timeout=5)
    assert status(job_env)['first_failure_at'] is None
    assert status(job_env)['consecutive_failures'] == 0
    assert status(job_env)['last_success_at']


def test_liveness_reports_persistent_failure_and_lost_running_process(tmp_path):
    now = datetime.now(timezone.utc)
    (tmp_path/'failed.json').write_text(json.dumps({'job':'bad','state':'failed','exit_code':1,'first_failure_at':(now-timedelta(days=3)).isoformat()}))
    (tmp_path/'running.json').write_text(json.dumps({'job':'lost','state':'running','started_at':(now-timedelta(hours=3)).isoformat(),'timeout_s':60}))
    results = inspect_statuses(tmp_path, now=now)
    assert results[0]['persistent'] is True and results[0]['healthy'] is False
    assert results[1]['state'] == 'overdue_running' and results[1]['healthy'] is False


def test_service_startup_is_bounded_and_unhealthy_is_not_success(job_env, monkeypatch):
    import src.operations.jobs as jobs
    monkeypatch.setenv('SECOND_BRAIN_HEALTH_INTERVAL', '.01')
    monkeypatch.setattr(jobs, '_healthy', lambda *_: False)
    path = script(job_env, 'import time; time.sleep(30)')
    assert run_job(str(path),'test',repo=job_env,timeout=.03,service=True) == 124
    assert status(job_env)['state'] == 'failed'


def test_service_heartbeats_then_health_failure_is_durable(job_env, monkeypatch):
    import src.operations.jobs as jobs
    monkeypatch.setenv('SECOND_BRAIN_HEALTH_INTERVAL', '.01')
    monkeypatch.setenv('SECOND_BRAIN_HEALTH_FAILURES', '2')
    health = iter([True,False,False])
    monkeypatch.setattr(jobs, '_healthy', lambda *_: next(health))
    path = script(job_env, 'import time; time.sleep(30)')
    assert run_job(str(path),'test',repo=job_env,timeout=.5,service=True) == 1
    value=status(job_env)
    assert value['heartbeat_at'] and value['last_success_at']
    assert value['state'] == 'failed' and value['exit_code'] == 1


def test_service_unexpected_normal_exit_is_failure(job_env, monkeypatch):
    import src.operations.jobs as jobs
    monkeypatch.setenv('SECOND_BRAIN_HEALTH_INTERVAL', '.01')
    monkeypatch.setattr(jobs, '_healthy', lambda *_: True)
    path = script(job_env, 'pass')
    assert run_job(str(path),'test',repo=job_env,timeout=1,service=True) == 1


def test_running_service_needs_fresh_health_not_batch_duration(tmp_path):
    now = datetime.now(timezone.utc)
    value={'job':'api','state':'running','service':True,'started_at':(now-timedelta(days=2)).isoformat(),
           'heartbeat_at':now.isoformat(),'timeout_s':60,'health_interval_s':30}
    (tmp_path/'api.json').write_text(json.dumps(value))
    assert inspect_statuses(tmp_path,now=now)[0]['healthy'] is True
    value['heartbeat_at']=(now-timedelta(minutes=10)).isoformat()
    (tmp_path/'api.json').write_text(json.dumps(value))
    assert inspect_statuses(tmp_path,now=now)[0]['state'] == 'stale_service_heartbeat'


@pytest.mark.parametrize("service", [False, True])
def test_signal_stops_term_ignoring_child_before_job_deadline(job_env, monkeypatch, service):
    import signal
    import time
    import src.operations.jobs as jobs
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


def test_unexpected_service_monitor_error_stops_child_and_records_failure(job_env, monkeypatch):
    import time
    import src.operations.jobs as jobs
    def broken_health(*_):
        deadline = time.monotonic() + 5
        while not (job_env / "child.pid").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        raise RuntimeError("synthetic monitor failure")
    monkeypatch.setattr(jobs, "_healthy", broken_health)
    path = script(job_env, "import os,time; from pathlib import Path; "
                  "Path('child.pid').write_text(str(os.getpid())); time.sleep(60)")
    assert run_job(str(path), "test", repo=job_env, timeout=60, service=True) == 70
    value = status(job_env)
    assert value["state"] == "failed" and value["error_type"] == "RuntimeError"
    with pytest.raises(ProcessLookupError):
        os.kill(int((job_env / "child.pid").read_text()), 0)


def test_child_cleanup_tolerates_process_exit_race(monkeypatch):
    from unittest.mock import Mock
    import src.operations.jobs as jobs
    child = Mock(pid=123)
    def already_exited(*_):
        raise ProcessLookupError()
    monkeypatch.setattr(os, "killpg", already_exited)
    jobs._stop_child(child)
    assert child.wait.call_count == 2
