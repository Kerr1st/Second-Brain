"""The safe runner refuses live targets before contacting a provider or database."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import boto3
import psycopg2
import pytest


def test_database_connections_require_exact_disposable_endpoint():
    for config in (
        {"host": "127.0.0.1", "dbname": "memory_bank", "user": "memory_bank"},
        {"host": os.environ["DB_HOST"], "dbname": "memory_bank", "user": os.environ["DB_USER"]},
        {"host": os.environ["DB_HOST"], "dbname": os.environ["DB_NAME"], "user": "sb_test_admin"},
        {"host": os.environ["DB_HOST"], "dbname": os.environ["DB_NAME"], "user": os.environ["DB_USER"], "hostaddr": "127.0.0.1"},
    ):
        with pytest.raises(RuntimeError, match="outside disposable"):
            psycopg2.connect(**config)


def test_provider_and_network_access_refused():
    with pytest.raises(RuntimeError, match="provider access"):
        boto3.client("bedrock-runtime")
    with socket.socket() as connection:
        with pytest.raises(RuntimeError, match="Network connections"):
            connection.connect(("192.0.2.1", 443))
    with pytest.raises(RuntimeError, match="External service subprocesses"):
        subprocess.run(["aws", "sts", "get-caller-identity"])


def test_os_policy_also_blocks_network_from_subprocesses():
    # A fresh child has no Python audit hooks. sandbox-exec still refuses the
    # connection before a packet can leave; 192.0.2.1 is a synthetic TEST-NET IP.
    result = subprocess.run([sys.executable, "-c", "import socket; socket.create_connection(('192.0.2.1',443), timeout=0.1)"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr


def test_schema_is_synthetic_and_role_is_not_superuser(test_schema):
    with psycopg2.connect(**test_schema) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user, inet_server_addr(), rolsuper FROM pg_roles WHERE rolname=current_user")
            assert cursor.fetchone() == (os.environ["DB_NAME"], "sb_test", None, False)
            cursor.execute("SHOW hnsw.ef_search")
            assert cursor.fetchone()[0] == "200"
    from src.backends import base
    manifest = json.loads(Path(os.environ["SECOND_BRAIN_TEST_MANIFEST"]).read_text())
    assert base.METRICS_DIR == manifest["metrics"]


def test_plain_pytest_fails_before_collection(tmp_path):
    env = {**os.environ}
    env.pop("SECOND_BRAIN_TEST_MANIFEST")
    result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "tests/test_test_isolation.py", "-p", "no:cacheprovider"], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "direct pytest is disabled" in result.stderr + result.stdout
