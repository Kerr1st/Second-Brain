"""Precollection guards. Loaded by scripts/test_isolated.py before test imports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

_installed = False


def install():
    global _installed
    if _installed:
        return
    marker = os.environ.get("SECOND_BRAIN_TEST_MANIFEST")
    if not marker:
        raise RuntimeError("Use python scripts/test_isolated.py -- <pytest arguments>; direct pytest is disabled to protect live data.")
    path = Path(marker).resolve()
    manifest = json.loads(path.read_text())
    host = Path(manifest["host"]).resolve()
    if not (path.parent.name.startswith("sbtest-") and path.parent.parent == Path("/private/tmp")
            and host == path.parent / "socket"
            and Path(manifest["data_directory"]).resolve() == path.parent / "data"
            and all(name.startswith("sb_") and name.endswith("_test") for name in manifest["databases"])):
        raise RuntimeError("Invalid disposable test manifest")
    if (os.environ.get("DB_HOST") != str(host) or os.environ.get("DB_NAME") not in manifest["databases"]
            or os.environ.get("TEST_DB_NAME") != os.environ.get("DB_NAME")
            or os.environ.get("DB_USER") != manifest["user"]
            or os.environ.get("DB_MIGRATE_USER") != manifest["owner_user"]
            or os.environ.get("KIRO_LLM_METRICS_DIR") != manifest["metrics"]):
        raise RuntimeError("Test environment does not match disposable manifest")
    # The application role, the owner role that runs migrations, and the two roles of
    # the pre-017 bootstrap scenario. Never the cluster superuser (sb_test_admin).
    users = {manifest["user"], manifest["owner_user"], manifest["bootstrap"]["legacy_user"], manifest["bootstrap"]["owner_user"]}

    import psycopg2
    from psycopg2.extensions import parse_dsn
    original_connect = psycopg2.connect

    def guarded_connect(dsn=None, *args, **kwargs):
        values = parse_dsn(dsn) if dsn else {}
        values.update(kwargs)
        if (values.get("host") != str(host) or str(values.get("port", "5432")) != str(manifest["port"])
                or values.get("dbname", values.get("database")) not in manifest["databases"]
                or values.get("user") not in users or values.get("hostaddr") or values.get("service")):
            raise RuntimeError("Refusing PostgreSQL connection outside disposable test manifest")
        return original_connect(dsn, *args, **kwargs)
    psycopg2.connect = guarded_connect

    import boto3
    import boto3.session
    def refuse_provider(*args, **kwargs):
        raise RuntimeError("Live model/provider access is disabled in the isolated suite")
    boto3.client = refuse_provider
    boto3.resource = refuse_provider
    boto3.session.Session.client = refuse_provider
    boto3.session.Session.resource = refuse_provider

    def audit(event, args):
        if event == "socket.connect":
            address = args[1]
            if not isinstance(address, str) or not address.startswith(str(host) + "/"):
                raise RuntimeError("Network connections are disabled in the isolated suite")
        if event == "subprocess.Popen":
            executable, argv = args[0], args[1]
            name = Path(os.fsdecode(executable)).name
            blocked = {"kiro", "kiro-cli", "claude", "codex", "aws", "ollama", "curl", "wget", "ssh", "scp", "rclone"}
            if name in blocked or (isinstance(argv, (tuple, list)) and argv and Path(os.fsdecode(argv[0])).name in blocked):
                raise RuntimeError("External service subprocesses are disabled in the isolated suite")
    sys.addaudithook(audit)
    _installed = True


install()
