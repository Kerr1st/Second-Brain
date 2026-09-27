#!/usr/bin/env python3
"""Run pytest against fresh synthetic databases, without provider/network access.

Requires PostgreSQL 17 + pgvector, the project's Python dependencies, and macOS
sandbox-exec. The suite never inherits credentials or uses an existing cluster.
Pass pytest arguments after ``--``. A failed run keeps its temporary evidence;
the PostgreSQL process is stopped in all cases, including interrupted tests.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DATABASES = ("sb_suite_test", "sb_migration_fresh_test", "sb_migration_failure_test", "sb_migration_concurrent_test")


def main() -> int:
    pg_bin = Path(os.environ.get("SECOND_BRAIN_TEST_PG_BIN", "/opt/homebrew/opt/postgresql@17/bin"))
    for name in ("initdb", "pg_ctl", "psql"):
        if not (pg_bin / name).is_file():
            raise SystemExit(f"Missing PostgreSQL tool: {pg_bin / name}")
    sandbox = Path("/usr/bin/sandbox-exec")
    if not sandbox.is_file():
        raise SystemExit("This runner requires macOS sandbox-exec; no unguarded fallback.")
    area = Path(tempfile.mkdtemp(prefix="sbtest-", dir="/private/tmp"))
    data, sockets, home = area / "data", area / "socket", area / "home"
    sockets.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    (home / ".codex").mkdir()
    (area / "tmp").mkdir()
    (area / "metrics").mkdir()
    env = {
        "PATH": f"{pg_bin}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home), "TMPDIR": str(area / "tmp"),
        "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
        "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "AWS_CONFIG_FILE": str(area / "absent-aws-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(area / "absent-aws-credentials"),
        "AWS_EC2_METADATA_DISABLED": "true", "AWS_DEFAULT_REGION": "us-east-1",
        "DB_HOST": str(sockets), "DB_PORT": "5432", "DB_USER": "sb_test",
        "DB_NAME": DATABASES[0], "DB_PASSWORD": "", "TEST_DB_NAME": DATABASES[0],
        "PGPASSFILE": str(area / "absent-pgpass"),
        "CODEX_HOME": str(home / ".codex"), "SECOND_BRAIN_PROFILE": "controlled",
        "KIRO_LLM_METRICS_DIR": str(area / "metrics"),
        "HYPOTHESIS_STORAGE_DIRECTORY": str(area / "hypothesis"),
    }
    def run(command, **kwargs):
        return subprocess.run(command, env=env, cwd=ROOT, check=True, **kwargs)

    started = False
    rc = 1
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"Interrupted by signal {signum}")
    previous_sigterm = signal.signal(signal.SIGTERM, interrupted)
    try:
        with (area / "bootstrap.log").open("w") as log:
            run([str(pg_bin / "initdb"), "-D", str(data), "-U", "sb_test_admin", "--auth-local=trust", "--auth-host=reject", "--no-locale", "-E", "UTF8"], stdout=log, stderr=subprocess.STDOUT)
            with (data / "postgresql.conf").open("a") as config:
                config.write(f"\nlisten_addresses = ''\nunix_socket_directories = '{sockets}'\n")
            run([str(pg_bin / "pg_ctl"), "-D", str(data), "-l", str(area / "postgres.log"), "-w", "start"], stdout=log, stderr=subprocess.STDOUT)
            started = True
            psql = [str(pg_bin / "psql"), "-X", "-v", "ON_ERROR_STOP=1", "-h", str(sockets), "-p", "5432", "-U", "sb_test_admin"]
            run(psql + ["-d", "postgres", "-c", "CREATE ROLE sb_test LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;"], stdout=log, stderr=subprocess.STDOUT)
            for database in DATABASES:
                run(psql + ["-d", "postgres", "-c", f'CREATE DATABASE "{database}" OWNER sb_test;'], stdout=log, stderr=subprocess.STDOUT)
                run(psql + ["-d", database, "-c", "CREATE EXTENSION vector;"], stdout=log, stderr=subprocess.STDOUT)
            actual = run(psql + ["-d", "postgres", "-Atc", "SHOW data_directory"], capture_output=True, text=True).stdout.strip()
            assert Path(actual).resolve() == data.resolve(), "Unexpected PostgreSQL identity"
        marker = area / "disposable.json"
        marker.write_text(json.dumps({"host": str(sockets), "port": 5432, "user": "sb_test", "databases": DATABASES, "data_directory": str(data), "metrics": str(area / "metrics")}))
        env["SECOND_BRAIN_TEST_MANIFEST"] = str(marker)
        # OS policy also covers psql and commands started by shell-script tests.
        # Only this cluster's private Unix socket permits outbound connections.
        policy = area / "offline.sb"
        policy.write_text('(version 1)\n(allow default)\n(deny network-outbound)\n'
                          f'(allow network-outbound (remote unix-socket (path-prefix "{sockets}")))\n')
        args = sys.argv[1:]
        if args[:1] == ["--"]:
            args = args[1:]
        command = [str(sandbox), "-f", str(policy), sys.executable, "-m", "pytest",
                   "-p", "tests._isolation", "-p", "hypothesis.extra.pytestplugin",
                   "-p", "no:cacheprovider", "-o", "addopts=", *args]
        print(f"Isolated test evidence: {area}", flush=True)
        rc = subprocess.run(command, cwd=ROOT, env=env).returncode
        return rc
    finally:
        if started or (data / "postmaster.pid").exists():
            stopped = subprocess.run([str(pg_bin / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"], env=env, capture_output=True, text=True)
            if stopped.returncode:
                print(f"ERROR: could not stop disposable cluster {data}: {stopped.stderr}", file=sys.stderr)
                raise RuntimeError("Disposable PostgreSQL shutdown failed")
        signal.signal(signal.SIGTERM, previous_sigterm)
        print(f"Disposable cluster stopped. Evidence retained: {area}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
