"""One scheduler boundary for environment, locks, timeouts and durable status."""
from __future__ import annotations
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict):
    fd, temporary = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def environment(repo: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = ":".join([str(repo / ".venv/bin"), str(Path.home() / ".toolbox/bin"),
        "/opt/homebrew/opt/postgresql@17/bin", "/opt/homebrew/bin", "/usr/local/bin", env.get("PATH", "/usr/bin:/bin")])
    env.setdefault("AWS_PROFILE", "second-brain-jobs")
    env.setdefault("SECOND_BRAIN_PROFILE", "controlled")
    env.setdefault("SECOND_BRAIN_PYTHON", str(repo / ".venv/bin/python"))
    env["SECOND_BRAIN_WRAPPED"] = "1"
    return env


class _JobInterrupted(BaseException):
    """Unwind waits immediately so scheduler signals cannot strand a child."""


def _signal_group(child, signum):
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        pass


def _stop_child(child):
    # Clean the group even when the group leader exited before its descendants.
    _signal_group(child, signal.SIGTERM)
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    finally:
        _signal_group(child, signal.SIGKILL)
    child.wait(timeout=5)


def _healthy(url: str, timeout: float) -> bool:
    """Only probe the local service; do not forward health through a proxy."""
    from urllib.parse import urlsplit
    from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler
    from urllib.error import URLError
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1", "localhost"} or parts.username or parts.password:
        raise ValueError("service health URL must be local HTTP")
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(url, timeout=timeout) as response:
            payload = json.loads(response.read(4096))
            return response.status == 200 and isinstance(payload, dict) and payload.get("status") == "healthy"
    except (OSError, URLError, ValueError):
        return False


def _monitor_service(child, status, status_path, env, *, startup_timeout):
    """Startup is bounded; thereafter a fresh successful probe is the heartbeat."""
    interval = float(env.get("SECOND_BRAIN_HEALTH_INTERVAL", "30"))
    probe_timeout = float(env.get("SECOND_BRAIN_HEALTH_TIMEOUT", "3"))
    failure_limit = int(env.get("SECOND_BRAIN_HEALTH_FAILURES", "3"))
    if not math.isfinite(interval) or not math.isfinite(probe_timeout) or interval <= 0 or probe_timeout <= 0 or failure_limit < 1:
        raise ValueError("service health controls must be positive")
    url = env.get("SECOND_BRAIN_HEALTH_URL", "http://127.0.0.1:8100/health")
    deadline = time.monotonic() + startup_timeout
    ready, failures = False, 0
    status.update(service=True, health_interval_s=interval, health_timeout_s=probe_timeout,
                  health_failure_limit=failure_limit, heartbeat_at=None)
    while True:
        code = child.poll()
        if code is not None:
            # An unexpected normal exit is still a stopped scheduled service.
            return code or 1
        try:
            healthy = _healthy(url, probe_timeout)
        except ValueError:
            _stop_child(child)
            return 78
        if healthy:
            ready, failures = True, 0
            status.update(heartbeat_at=_stamp(), health_state="healthy", last_success_at=_stamp(),
                          consecutive_failures=0, first_failure_at=None)
        else:
            failures += 1
            status["health_state"] = "unhealthy" if ready else "starting"
        _atomic_json(status_path, status)
        if (not ready and time.monotonic() >= deadline) or (ready and failures >= failure_limit):
            _stop_child(child)
            return 124 if not ready else 1
        delay = interval if ready else min(interval, max(0.001, deadline-time.monotonic()))
        try:
            child.wait(timeout=delay)
        except subprocess.TimeoutExpired:
            pass


def run_job(script: str, name: str, args=(), *, repo=None, timeout=None, service=None) -> int:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("invalid job name")
    os.umask(0o077)
    repo = Path(repo or Path(__file__).resolve().parents[2])
    env = environment(repo)
    service = env.get("SECOND_BRAIN_JOB_SERVICE") == "1" if service is None else service
    timeout = float(timeout if timeout is not None else env.get(
        "SECOND_BRAIN_JOB_TIMEOUT_" + re.sub(r"[^A-Z0-9]", "_", name.upper()),
        env.get("SECOND_BRAIN_JOB_TIMEOUT", "3600")))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("job timeout must be positive")
    log_dir = Path(env.get("SECOND_BRAIN_LOG_DIR", str(repo / "logs")))
    status_dir = Path(env.get("SECOND_BRAIN_STATUS_DIR", str(log_dir / "jobs")))
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    status_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    status_path = status_dir / f"{name}.json"
    log_path = log_dir / f"{name}-{datetime.now():%Y%m%d}.log"
    with (status_dir / f"{name}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Do not replace the running job's authoritative status.
            print(json.dumps({"job": name, "state": "already_running", "exit_code": 75}))
            return 75
        previous = {}
        if status_path.exists():
            try:
                previous = json.loads(status_path.read_text())
            except (OSError, ValueError):
                pass
        status = {"schema_version": 1, "job": name, "state": "running", "started_at": _stamp(),
                  "pid": os.getpid(), "timeout_s": timeout, "exit_code": None,
                  "last_success_at": previous.get("last_success_at"),
                  "consecutive_failures": previous.get("consecutive_failures", 0),
                  "first_failure_at": previous.get("first_failure_at")}
        _atomic_json(status_path, status)
        path = Path(script)
        if not path.is_absolute():
            path = repo / path
        command = [env["SECOND_BRAIN_PYTHON"], str(path)] if path.suffix == ".py" else [str(path)]
        command += list(args)
        if env.get("SECOND_BRAIN_CAFFEINATE", "1") != "0" and Path("/usr/bin/caffeinate").exists():
            command = ["/usr/bin/caffeinate", "-i", *command]
        started = time.monotonic()
        child = None
        interrupted = None
        stopping = False
        def stop(signum, _frame):
            nonlocal interrupted
            if interrupted is None:
                interrupted = signum
                if not stopping:
                    raise _JobInterrupted()
        old_handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        code = 70
        try:
            with log_path.open("a") as log:
                log.write(f"[{status['started_at']}] Starting {name}\n")
                log.flush()
                try:
                    child = subprocess.Popen(command, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    try:
                        code = (_monitor_service(child, status, status_path, env, startup_timeout=timeout)
                                if service else child.wait(timeout=timeout))
                        if interrupted:
                            code = 128 + interrupted
                        elif code < 0:
                            code = 128 - code
                    except subprocess.TimeoutExpired:
                        _stop_child(child)
                        code = 124
                    except (ValueError, TypeError):
                        _stop_child(child)
                        code = 78
                except OSError as exc:
                    log.write(f"Launch failed: {type(exc).__name__}\n")
                    code = 127
                log.write(f"[{_stamp()}] {'OK' if code == 0 else 'FAILED'} (exit {code})\n")
        except _JobInterrupted:
            code = 128 + interrupted
            status["interrupted_by"] = interrupted
        except Exception as exc:
            # Scheduler boundary: every unexpected monitor/logging error must
            # stop its owned process group and leave a durable failure record.
            status["error_type"] = type(exc).__name__
            code = 70
        finally:
            stopping = True
            try:
                if child is not None:
                    _stop_child(child)
            except Exception as exc:
                status["cleanup_error_type"] = type(exc).__name__
                code = 70
            finally:
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
        if interrupted:
            code = 128 + interrupted
            status["interrupted_by"] = interrupted
        status.update(state="interrupted" if interrupted else "succeeded" if code == 0 else "failed", exit_code=code,
                      finished_at=_stamp(), duration_s=round(time.monotonic()-started, 3))
        if code:
            status["consecutive_failures"] += 1
            status["first_failure_at"] = status["first_failure_at"] or status["finished_at"]
        else:
            status.update(consecutive_failures=0, first_failure_at=None, last_success_at=status["finished_at"])
        _atomic_json(status_path, status)
        print(json.dumps(status, sort_keys=True), flush=True)
        # Headless status is primary. Optional desktop notification cannot change exit.
        if code and env.get("SECOND_BRAIN_NOTIFY", "1") != "0":
            try:
                subprocess.run(["osascript", "-e", 'on run argv\ndisplay notification (item 1 of argv) with title "Second Brain"\nend run',
                                f"{name} failed (exit {code}); inspect job status/log."],
                               env=env, capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        cutoff = time.time() - 30 * 86400
        for old_log in log_dir.glob("*.log"):
            if old_log.is_file() and old_log.stat().st_mtime < cutoff:
                old_log.unlink()
        return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("script")
    parser.add_argument("job", nargs="?")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    return run_job(args.script, args.job or Path(args.script).stem, args.args)


if __name__ == "__main__":
    raise SystemExit(main())
