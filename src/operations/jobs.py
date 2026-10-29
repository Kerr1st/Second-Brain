"""One scheduler boundary for environment, locks, timeouts and durable status.

Every launchd job runs through ``scripts/jobs/job_wrapper.sh`` → ``python -m
src.operations.jobs <script> <job-name> [args…]``.  The runner owns:

* the environment every job needs under launchd's minimal PATH (Homebrew,
  PostgreSQL 17, the venv, ``AWS_PROFILE=second-brain-jobs``, ``umask 077``);
* one ``flock`` per job name (``logs/jobs/<name>.lock``), so a slow run is never
  overlapped by the next schedule tick;
* a wall-clock deadline (``SECOND_BRAIN_JOB_TIMEOUT_<NAME>``) with a SIGTERM →
  grace period → SIGKILL stop so the dream-cycle orchestrator's ``RunInterrupted``
  path can close its run row before the group is killed;
* a durable JSON status record (``logs/jobs/<name>.json``) that liveness reads,
  and the per-day log ``logs/<name>-YYYYMMDD.log``;
* exit-code passthrough (the child's exit is the wrapper's exit).

Timeouts.  The default deadline (3600 s) is wrong for the long jobs, so two
guards exist: the committed plists set ``SECOND_BRAIN_JOB_TIMEOUT_<NAME>`` for
every wrapped job (``tests/test_scheduling_plists.py`` pins them against
``TIMEOUT_FLOORS``), and the runner itself never lets the *default* or the
*generic* ``SECOND_BRAIN_JOB_TIMEOUT`` fall below a job's floor.  An explicit
per-job value always wins (operator intent, e.g. a short manual rehearsal).

Service mode (``SECOND_BRAIN_JOB_SERVICE=1``, the KeepAlive capture API): no
deadline once the service is up; a bounded startup; recurring local health
probes; on failure the wrapper exits so launchd restarts it, but it backs off
and notifies at most once per ``SECOND_BRAIN_NOTIFY_INTERVAL_S`` so a database
outage is one notification, not one per restart.

Network preflight (``SECOND_BRAIN_JOB_NEEDS_NETWORK=1``).  launchd fires the
02:00/06:00 jobs during a *dark (maintenance) wake*; the job starts before the
network stack is up and the first Bedrock call dies with
``EndpointConnectionError`` / ``nodename nor servname provided`` (weekly-digest
every Monday since 2026-09-07, dream-cycle 2026-09-30).  For jobs flagged in
their plist the runner first waits — holding a ``caffeinate -s -i`` assertion so
the Mac does not fall asleep again meanwhile — until
``SECOND_BRAIN_JOB_NETWORK_PROBE_HOST`` resolves (``socket.getaddrinfo`` only, no
HTTP), with backoff, for at most ``SECOND_BRAIN_JOB_NETWORK_WAIT_S``.  The job's
deadline starts *after* that wait.  If the host still does not resolve the job is
not started and the wrapper exits ``68`` ("network unavailable"), recorded as
state ``network_unavailable`` with its own ``consecutive_network_failures``
counter — infrastructure, not a job failure, and liveness reports it as such.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TIMEOUT_S = 3600.0
DEFAULT_GRACE_S = 120.0
DEFAULT_SERVICE_STARTUP_S = 120.0
DEFAULT_NOTIFY_INTERVAL_S = 1800.0
DEFAULT_SERVICE_BACKOFF_MAX_S = 300.0
DEFAULT_NETWORK_WAIT_S = 900.0
DEFAULT_NETWORK_PROBE_HOST = "bedrock-runtime.us-east-1.amazonaws.com"
NETWORK_BACKOFF_MAX_S = 60.0
EXIT_NETWORK_UNAVAILABLE = 68
CAFFEINATE = "/usr/bin/caffeinate"

# Documented minimum deadlines per job name (seconds).  Derived from the observed
# wall-clock durations in /Users/kerrfir/second-brain/logs (first/last line of
# each job log, 2026-09-05 … 2026-10-06):
#   dream-cycle   39, 46, 53, 92, 204 min (Mon/Wed/Fri; observed at 02:00, 12:30 since 2026-10-09) → 6 h
#   bookmarks     39, 64, 64 min (sleep-inflated nights: 9 h, 13 h) → 3 h
#   backup        14–31 min (pg_dump 1.1 GB + gpg + rclone)         → 2 h
#   verify        1–5 min listing only; the new full-restore verify downloads
#                 1.1 GB and runs pg_restore (219 s on the R3 clone)  → 2 h
#   weekly-digest 5–98 min (Bedrock retries; 09-21 ran 06:05→07:43)   → 2 h
#   codex-capture every 6 h, up to 5 metered Opus calls per run        → 2 h
#   distill       disabled; same shape as weekly-digest when re-enabled → 2 h
#   everything else finished in < 6 min on every observed night; the 3600 s
#   default is already ≥ 10x their worst case.
TIMEOUT_FLOORS = {
    "dream-cycle": 6 * 3600,
    "bookmarks": 3 * 3600,
    "backup": 2 * 3600,
    "verify": 2 * 3600,
    "weekly-digest": 2 * 3600,
    "codex-capture": 2 * 3600,
    "distill": 2 * 3600,
}
DEFAULT_FLOOR_S = 3600


def timeout_env_name(name: str) -> str:
    return "SECOND_BRAIN_JOB_TIMEOUT_" + re.sub(r"[^A-Z0-9]", "_", name.upper())


def timeout_floor(name: str) -> float:
    return float(TIMEOUT_FLOORS.get(name, DEFAULT_FLOOR_S))


def resolve_timeout(name: str, env, explicit=None) -> tuple[float, str]:
    """Return (seconds, source).  Explicit per-job values win; the default and the
    generic SECOND_BRAIN_JOB_TIMEOUT are raised to the job's floor so a job the
    floor table knows is long can never be killed by the 3600 s default."""
    if explicit is not None:
        value, source = float(explicit), "argument"
    elif env.get(timeout_env_name(name)):
        value, source = float(env[timeout_env_name(name)]), "per-job"
    elif env.get("SECOND_BRAIN_JOB_TIMEOUT"):
        value, source = float(env["SECOND_BRAIN_JOB_TIMEOUT"]), "generic"
    else:
        value, source = DEFAULT_TIMEOUT_S, "default"
    if not math.isfinite(value) or value <= 0:
        raise ValueError("job timeout must be positive")
    floor = timeout_floor(name)
    if source in ("default", "generic") and value < floor:
        return floor, f"{source}-raised-to-floor"
    return value, source


def _positive_float(env, key, default):
    try:
        value = float(env.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a positive number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key} must be a positive number")
    return value


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
                            "/opt/homebrew/opt/postgresql@17/bin", "/opt/homebrew/bin", "/usr/local/bin",
                            env.get("PATH", "/usr/bin:/bin")])
    env.setdefault("AWS_PROFILE", "second-brain-jobs")
    env.setdefault("SECOND_BRAIN_PROFILE", "controlled")
    env.setdefault("SECOND_BRAIN_PYTHON", str(repo / ".venv/bin/python"))
    env["SECOND_BRAIN_WRAPPED"] = "1"
    return env


class _JobInterrupted(BaseException):
    """Unwind waits immediately so scheduler signals cannot strand a child."""


class _NetworkUnavailable(Exception):
    """The preflight budget is spent and the probe host still does not resolve."""


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _signal_group(child, signum):
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        pass


def _reap(child, timeout):
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def _stop_child(child, grace: float = DEFAULT_GRACE_S) -> bool:
    """SIGTERM the whole group, wait up to ``grace`` for every member to exit,
    then SIGKILL whatever is left.  Returns True when the group ended on its own.

    The wait is on the *group*, not the leader: ``dream_cycle_scheduled.sh``-style
    leaders may die on SIGTERM instantly while the Python child they started is
    still closing its run row.  Killing the group the moment the leader is gone
    would be exactly the orphaned ``completed_at IS NULL`` run the grace exists
    to prevent."""
    _signal_group(child, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        _reap(child, timeout=min(0.05, max(0.0, deadline - time.monotonic())))
        if child.poll() is not None and not _group_alive(child.pid):
            return True
    graceful = child.poll() is not None and not _group_alive(child.pid)
    _signal_group(child, signal.SIGKILL)
    _reap(child, timeout=5)
    for _ in range(100):  # the kill is asynchronous; give the kernel a moment
        if not _group_alive(child.pid):
            break
        time.sleep(0.05)
    return graceful


def _healthy(url: str, timeout: float) -> bool:
    """Only probe the local service; do not forward health through a proxy."""
    from urllib.error import URLError
    from urllib.parse import urlsplit
    from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
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


def _monitor_service(child, status, status_path, env, *, startup_timeout, grace):
    """Startup is bounded; thereafter a fresh successful probe is the heartbeat.
    There is no deadline on a healthy running service."""
    interval = _positive_float(env, "SECOND_BRAIN_HEALTH_INTERVAL", "30")
    probe_timeout = _positive_float(env, "SECOND_BRAIN_HEALTH_TIMEOUT", "3")
    failure_limit = int(env.get("SECOND_BRAIN_HEALTH_FAILURES", "3"))
    if failure_limit < 1:
        raise ValueError("service health controls must be positive")
    url = env.get("SECOND_BRAIN_HEALTH_URL", "http://127.0.0.1:8100/health")
    deadline = time.monotonic() + startup_timeout
    ready, failures = False, 0
    status.update(service=True, health_interval_s=interval, health_timeout_s=probe_timeout,
                  health_failure_limit=failure_limit, heartbeat_at=None, startup_timeout_s=startup_timeout)
    while True:
        code = child.poll()
        if code is not None:
            # An unexpected normal exit is still a stopped scheduled service.
            return code or 1
        try:
            healthy = _healthy(url, probe_timeout)
        except ValueError:
            _stop_child(child, grace)
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
            _stop_child(child, grace)
            return 124 if not ready else 1
        delay = interval if ready else min(interval, max(0.001, deadline - time.monotonic()))
        _reap(child, timeout=delay)


def service_backoff(consecutive_failures: int, maximum: float = DEFAULT_SERVICE_BACKOFF_MAX_S) -> float:
    """Seconds the wrapper pauses before exiting a failed service run, so launchd's
    KeepAlive restart cadence slows down during an outage: 10, 20, 40 … ≤ maximum."""
    if consecutive_failures <= 0:
        return 0.0
    return float(min(maximum, 10.0 * 2 ** (consecutive_failures - 1)))


def should_notify(previous: dict, now: datetime, interval_s: float) -> bool:
    """At most one desktop notification per ``interval_s`` per job (durable via the
    status file), so a KeepAlive restart loop during a DB outage is not a storm."""
    last = previous.get("last_notified_at")
    if not last:
        return True
    try:
        return (now - datetime.fromisoformat(last)).total_seconds() >= interval_s
    except (TypeError, ValueError):
        return True


def _pause(seconds: float):
    if seconds > 0:
        time.sleep(seconds)


def _acquire_lock(lock, *, wait_s: float) -> bool:
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)


# ------------------------------------------------------------ network preflight

def _resolve_host(host: str):
    """DNS only — the cheapest honest signal that the network is up after a dark wake."""
    socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)


def network_backoff(failed_attempts: int, maximum: float = NETWORK_BACKOFF_MAX_S) -> float:
    """Seconds to wait after ``failed_attempts`` resolution failures: 5, 10, 20, 40, 60, 60 …"""
    if failed_attempts <= 0:
        return 0.0
    return float(min(maximum, 5.0 * 2 ** (failed_attempts - 1)))


def wait_for_network(host: str, budget_s: float, *, resolve=None, sleep=None, clock=None, log=None) -> dict:
    """Retry resolving ``host`` with backoff until it resolves or ``budget_s`` is spent.
    Returns ``{"host", "budget_s", "reachable", "attempts", "waited_s", "last_error"}``.
    The hooks exist so tests can use a fake resolver and clock; production uses
    ``socket.getaddrinfo``, ``time.sleep`` and ``time.monotonic``."""
    resolve = resolve or _resolve_host
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    start = clock()
    attempts, last_error = 0, None
    while True:
        attempts += 1
        try:
            resolve(host)
            return {"host": host, "budget_s": budget_s, "reachable": True, "attempts": attempts,
                    "waited_s": round(clock() - start, 3), "last_error": last_error}
        except OSError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        remaining = budget_s - (clock() - start)
        if remaining <= 0:
            return {"host": host, "budget_s": budget_s, "reachable": False, "attempts": attempts,
                    "waited_s": round(clock() - start, 3), "last_error": last_error}
        delay = min(network_backoff(attempts), remaining)
        if log is not None:
            log.write(f"[{_stamp()}] Network preflight: {host} does not resolve ({last_error}); "
                      f"attempt {attempts}, retry in {delay:.0f}s ({remaining:.0f}s of {budget_s:.0f}s left)\n")
            log.flush()
        sleep(delay)


def _hold_awake(env):
    """``caffeinate -s -i`` for the duration of the preflight wait: a dark wake ends
    after a few minutes unless something asserts, and the job has not started yet
    (its own ``caffeinate -i`` wraps only the child)."""
    if env.get("SECOND_BRAIN_CAFFEINATE", "1") == "0" or not Path(CAFFEINATE).exists():
        return None
    try:
        return subprocess.Popen([CAFFEINATE, "-s", "-i"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


def _release_awake(holder):
    if holder is None:
        return
    try:
        holder.terminate()
        holder.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            holder.kill()
        except OSError:
            pass


def run_job(script: str, name: str, args=(), *, repo=None, timeout=None, service=None) -> int:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("invalid job name")
    os.umask(0o077)
    repo = Path(repo or Path(__file__).resolve().parents[2])
    env = environment(repo)
    service = env.get("SECOND_BRAIN_JOB_SERVICE") == "1" if service is None else service
    grace = _positive_float(env, "SECOND_BRAIN_JOB_GRACE_S", str(DEFAULT_GRACE_S))
    if service:
        # A service has no deadline; only its startup is bounded.
        timeout = float(timeout) if timeout is not None else _positive_float(
            env, "SECOND_BRAIN_SERVICE_STARTUP_TIMEOUT_S", str(DEFAULT_SERVICE_STARTUP_S))
        timeout_source = "service-startup"
    else:
        timeout, timeout_source = resolve_timeout(name, env, timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("job timeout must be positive")
    log_dir = Path(env.get("SECOND_BRAIN_LOG_DIR", str(repo / "logs")))
    status_dir = Path(env.get("SECOND_BRAIN_STATUS_DIR", str(log_dir / "jobs")))
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    status_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    status_path = status_dir / f"{name}.json"
    log_path = log_dir / f"{name}-{datetime.now():%Y%m%d}.log"
    with (status_dir / f"{name}.lock").open("a") as lock:
        # A KeepAlive service is restarted by launchd the moment the previous
        # wrapper exits; the previous group may still be draining, so wait a
        # little for the lock instead of failing (and being restarted) at once.
        lock_wait = _positive_float(env, "SECOND_BRAIN_SERVICE_LOCK_WAIT_S", "30") if service else 0.0
        if not _acquire_lock(lock, wait_s=lock_wait):
            # Do not replace the running job's authoritative status.
            print(json.dumps({"job": name, "state": "already_running", "exit_code": 75}))
            return 75
        previous = {}
        if status_path.exists():
            try:
                previous = json.loads(status_path.read_text())
            except (OSError, ValueError):
                pass
        needs_network = env.get("SECOND_BRAIN_JOB_NEEDS_NETWORK") == "1"
        network_wait_s = _positive_float(env, "SECOND_BRAIN_JOB_NETWORK_WAIT_S", str(DEFAULT_NETWORK_WAIT_S))
        probe_host = env.get("SECOND_BRAIN_JOB_NETWORK_PROBE_HOST", DEFAULT_NETWORK_PROBE_HOST).strip() or DEFAULT_NETWORK_PROBE_HOST
        status = {"schema_version": 1, "job": name, "state": "running", "started_at": _stamp(),
                  "pid": os.getpid(), "timeout_s": timeout, "timeout_source": timeout_source,
                  "grace_s": grace, "exit_code": None,
                  "needs_network": needs_network,
                  "last_success_at": previous.get("last_success_at"),
                  "consecutive_failures": previous.get("consecutive_failures", 0),
                  "first_failure_at": previous.get("first_failure_at"),
                  "consecutive_network_failures": previous.get("consecutive_network_failures", 0),
                  "last_network_failure_at": previous.get("last_network_failure_at"),
                  "last_notified_at": previous.get("last_notified_at")}
        _atomic_json(status_path, status)
        path = Path(script)
        if not path.is_absolute():
            path = repo / path
        command = [env["SECOND_BRAIN_PYTHON"], str(path)] if path.suffix == ".py" else [str(path)]
        command += list(args)
        if env.get("SECOND_BRAIN_CAFFEINATE", "1") != "0" and Path(CAFFEINATE).exists():
            command = [CAFFEINATE, "-i", *command]
        started = time.monotonic()
        child = None
        interrupted = None
        stopping = False
        network_unavailable = False

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
                log.write(f"[{status['started_at']}] Starting {name} (timeout {timeout:.0f}s via {timeout_source}, grace {grace:.0f}s)\n")
                log.flush()
                try:
                    if not path.exists():
                        # A plist pointing at a deleted/renamed script (E-N5) must read as
                        # "launch failed", not as the interpreter's own exit 2.
                        raise FileNotFoundError(str(path))
                    if needs_network:
                        # Dark-wake guard: do not start (and do not start the deadline)
                        # until the network is up; stay awake while waiting.
                        holder = _hold_awake(env)
                        try:
                            preflight = wait_for_network(probe_host, network_wait_s, log=log)
                        finally:
                            _release_awake(holder)
                        status.update(network_probe_host=probe_host, network_wait_budget_s=network_wait_s,
                                      network_attempts=preflight["attempts"], network_waited_s=preflight["waited_s"],
                                      network_reachable=preflight["reachable"])
                        if not preflight["reachable"]:
                            network_unavailable = True
                            log.write(f"[{_stamp()}] Network unavailable: {probe_host} did not resolve within "
                                      f"{network_wait_s:.0f}s ({preflight['attempts']} attempts; {preflight['last_error']}); "
                                      f"job not started\n")
                            raise _NetworkUnavailable()
                        log.write(f"[{_stamp()}] Network preflight: {probe_host} resolved after {preflight['attempts']} attempt(s), "
                                  f"{preflight['waited_s']:.0f}s; deadline {timeout:.0f}s starts now\n")
                        log.flush()
                        _atomic_json(status_path, status)
                    started = time.monotonic()
                    child = subprocess.Popen(command, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT,
                                             start_new_session=True)
                    try:
                        code = (_monitor_service(child, status, status_path, env, startup_timeout=timeout, grace=grace)
                                if service else child.wait(timeout=timeout))
                        if interrupted:
                            code = 128 + interrupted
                        elif code < 0:
                            code = 128 - code
                    except subprocess.TimeoutExpired:
                        log.write(f"[{_stamp()}] Deadline {timeout:.0f}s reached: SIGTERM, grace {grace:.0f}s, then SIGKILL\n")
                        log.flush()
                        graceful = _stop_child(child, grace)
                        status["timed_out"] = True
                        status["stopped_gracefully"] = graceful
                        code = 124
                    except (ValueError, TypeError):
                        _stop_child(child, grace)
                        code = 78
                except _NetworkUnavailable:
                    code = EXIT_NETWORK_UNAVAILABLE
                except OSError as exc:
                    log.write(f"Launch failed: {type(exc).__name__}: {path}\n")
                    code = 127
                log.write(f"[{_stamp()}] {'OK' if code == 0 else 'FAILED'} (exit {code})"
                          f"{': network unavailable' if code == EXIT_NETWORK_UNAVAILABLE and network_unavailable else ''}\n")
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
                    _stop_child(child, grace)
            except Exception as exc:
                status["cleanup_error_type"] = type(exc).__name__
                code = 70
            finally:
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
        if interrupted:
            code = 128 + interrupted
            status["interrupted_by"] = interrupted
            # The SIGTERM/SIGINT reached the wrapper itself (launchd at shutdown,
            # `launchctl kickstart -k`, Ctrl-C): the job did not fail.  The
            # _JobInterrupted unwind left the `with log` block before its terminal
            # line, so write one now that the group has been stopped.
            try:
                with log_path.open("a") as log:
                    log.write(f"[{_stamp()}] INTERRUPTED (signal {interrupted}; exit {code})\n")
            except OSError:
                pass
        network_unavailable = network_unavailable and code == EXIT_NETWORK_UNAVAILABLE and not interrupted
        status.update(state="interrupted" if interrupted else "network_unavailable" if network_unavailable
                      else "succeeded" if code == 0 else "failed", exit_code=code,
                      finished_at=_stamp(), duration_s=round(time.monotonic() - started, 3))
        if network_unavailable:
            # Infrastructure, not the job: its own counter, the job's failure streak untouched.
            status["consecutive_network_failures"] += 1
            status["last_network_failure_at"] = status["finished_at"]
        elif code:
            status["consecutive_failures"] += 1
            status["first_failure_at"] = status["first_failure_at"] or status["finished_at"]
        else:
            status.update(consecutive_failures=0, first_failure_at=None, last_success_at=status["finished_at"])
        if status.get("network_reachable") or not needs_network:
            status["consecutive_network_failures"] = 0
        # Headless status is primary. Optional desktop notification cannot change exit.
        # A network-unavailable start is reported by the liveness sweep (advisory, ❌
        # when it repeats), not by a 06:00 notification nobody is awake for.
        # An external interruption (state "interrupted": macOS shutdown SIGTERMs the
        # KeepAlive capture-api) is not a failure either; liveness still flags a batch
        # job that stays interrupted past failure_days.  The wrapper's own deadline
        # kill (124) and real non-zero exits notify as before.
        notify_interval = _positive_float(env, "SECOND_BRAIN_NOTIFY_INTERVAL_S", str(DEFAULT_NOTIFY_INTERVAL_S))
        notify = bool(code) and not interrupted and not network_unavailable and env.get("SECOND_BRAIN_NOTIFY", "1") != "0" and (
            not service or should_notify(previous, datetime.now(timezone.utc), notify_interval))
        if notify:
            status["last_notified_at"] = _stamp()
        _atomic_json(status_path, status)
        print(json.dumps(status, sort_keys=True), flush=True)
        if notify:
            try:
                subprocess.run(["osascript", "-e", 'on run argv\ndisplay notification (item 1 of argv) with title "Second Brain"\nend run',
                                f"{name} failed (exit {code}); inspect logs/jobs/{name}.json and the job log."],
                               env=env, capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        cutoff = time.time() - 30 * 86400
        for old_log in log_dir.glob("*.log"):
            if old_log.is_file() and old_log.stat().st_mtime < cutoff:
                old_log.unlink()
        if service and code and not interrupted:
            maximum = _positive_float(env, "SECOND_BRAIN_SERVICE_BACKOFF_MAX_S", str(DEFAULT_SERVICE_BACKOFF_MAX_S))
            _pause(service_backoff(status["consecutive_failures"], maximum))
        return code


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one Second Brain job under the scheduler boundary.")
    parser.add_argument("script")
    parser.add_argument("job", nargs="?")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    return run_job(args.script, args.job or Path(args.script).stem, args.args)


if __name__ == "__main__":
    sys.exit(main())
