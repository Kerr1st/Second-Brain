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

R5 moved the channel policy into ``config/liveness.toml`` evaluated by
``src.operations.liveness`` (tested in ``tests/test_liveness_policy.py``); the shell
adapter is tested here with that module stubbed: it must fail when the policy
module fails (the 74ba02d integration was stubbed to ``exit 0`` and untested),
pass its DB target through from the environment, keep the upstream Quick stat
check, and notify only on ❌.

So these tests pin the distinctions that make the monitors readable again: OK vs
EXPIRED vs TOOL-MISSING, REQUIRED vs ADVISORY, and ❌ vs ⚠ with an exit code that
follows only the ❌ column. Everything is stubbed — no AWS call, no MCP server, no
database, no Quick corpus is touched.
"""

import json
import os
from pathlib import Path
import shlex
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
    """Stub AWS CLI: fails for any profile named in $FAKE_AWS_FAIL_PROFILES.

    A failing call prints $FAKE_AWS_OUTPUT to stderr and exits $FAKE_AWS_RC (default 1).
    """
    return _script(
        tmp_path / "aws",
        "#!/bin/bash\n"
        'for p in ${FAKE_AWS_FAIL_PROFILES:-}; do\n'
        '  for a in "$@"; do if [[ "$a" == "$p" ]]; then\n'
        '    [ -n "${FAKE_AWS_OUTPUT:-}" ] && printf \'%s\\n\' "$FAKE_AWS_OUTPUT" >&2\n'
        '    exit "${FAKE_AWS_RC:-1}"\n'
        '  fi; done\n'
        "done\n"
        "echo \'{\"Account\": \"123456789012\"}\'\n"
        "exit 0\n",
    )


def _fake_python(tmp_path: Path, exit_code: int = 0) -> Path:
    """Stub interpreter for the grasp/slack heredoc probes — never imports src.

    Prints $FAKE_PY_OUTPUT to stderr and exits $FAKE_PY_RC (default ``exit_code``).
    """
    return _script(
        tmp_path / "python",
        "#!/bin/bash\ncat >/dev/null\n"
        '[ -n "${FAKE_PY_OUTPUT:-}" ] && printf \'%s\\n\' "$FAKE_PY_OUTPUT" >&2\n'
        f'exit "${{FAKE_PY_RC:-{exit_code}}}"\n',
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


def _scratch_dir(tmp_path: Path) -> Path:
    d = tmp_path / "scratch"
    d.mkdir(exist_ok=True)
    return d


def _reason_after(stdout: str, marker: str) -> str:
    """The `reason:` line logged directly under the line containing ``marker``."""
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        if marker in line:
            assert i + 1 < len(lines), stdout
            nxt = lines[i + 1]
            assert "   reason: " in nxt, f"no reason line under {marker!r}:\n{stdout}"
            return nxt.split("   reason: ", 1)[1]
    raise AssertionError(f"{marker!r} not in output:\n{stdout}")


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
        # Hermetic: never probe the real Roles Anywhere certificate, and keep the
        # probe scratch file somewhere a test can prove it was cleaned up.
        "SECOND_BRAIN_RA_CERT": str(tmp_path / "no-such-cert.pem"),
        "TMPDIR": str(_scratch_dir(tmp_path)),
        **env_overrides,
    }
    return subprocess.run(
        ["bash", str(CREDENTIALS), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )




def _policy_stub(tmp_path: Path) -> Path:
    """Stub for `python -m src.operations.liveness` (and the qd_profile one-liner).

    $FAKE_POLICY_LINES: text printed verbatim; $FAKE_POLICY_RC: exit code. Records
    its argv and the DB_* environment it saw in $FAKE_POLICY_LOG so the adapter's
    env passthrough can be asserted. Never imports src."""
    return _script(
        tmp_path / "python",
        "#!/bin/bash\n"
        'if [ "${1:-}" = "-c" ]; then exit 0; fi\n'
        'printf \'%s\\n\' "ARGS $*" "DB_HOST=${DB_HOST:-}" "DB_PORT=${DB_PORT:-}" "DB_NAME=${DB_NAME:-}" "DB_USER=${DB_USER:-}" >> "$FAKE_POLICY_LOG"\n'
        'printf \'%s\' "${FAKE_POLICY_LINES:-}"\n'
        'exit "${FAKE_POLICY_RC:-0}"\n',
    )


def _quick_db(tmp_path: Path, age_days: float, name: str = "knowledge_v1.db") -> Path:
    """A stand-in for the upstream Quick sqlite file. Only its mtime is ever read."""
    db = tmp_path / name
    db.write_bytes(b"")  # never opened by the monitor — stat(2) only
    stamp = time.time() - age_days * 86400
    os.utime(db, (stamp, stamp))
    return db


def _run_liveness(tmp_path: Path, lines: str = "", rc: int = 0, pg_ready: bool = True, **env_overrides) -> subprocess.CompletedProcess:
    _fake_notifier(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PG_ISREADY_BIN": str(_script(tmp_path / "pg_isready", f"#!/bin/bash\nexit {0 if pg_ready else 2}\n")),
        "SECOND_BRAIN_PYTHON": str(_policy_stub(tmp_path)),
        "SECOND_BRAIN_STATUS_DIR": str(tmp_path / "status"),
        "FAKE_POLICY_LOG": str(tmp_path / "policy.log"),
        "FAKE_POLICY_LINES": lines,
        "FAKE_POLICY_RC": str(rc),
        # A fresh upstream stub unless the test supplies its own (minting it here
        # would otherwise reset a deliberately-aged file's mtime).
        "QD_DB_PATH": env_overrides.pop("QD_DB_PATH", str(_quick_db(tmp_path, 0, "upstream-fresh.db"))),
        **env_overrides,
    }
    return subprocess.run(["bash", str(LIVENESS)], env=env, capture_output=True, text=True, check=False)


def _policy_log(tmp_path: Path) -> list[str]:
    log = tmp_path / "policy.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


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



# --- failure reasons (2026-10-13 triage: "exit 1" could not tell an expired token
# --- from a hung server or a bug, because probe output went to /dev/null) --------


def test_all_green_output_is_unchanged_and_the_scratch_file_is_removed(tmp_path: Path):
    """The ✅ lines are parsed by people and monitors; success must add nothing."""
    result = _run_credentials(tmp_path)

    assert result.returncode == 0, result.stdout
    body = [line.split("] ", 1)[1] for line in result.stdout.splitlines()]
    assert body == [
        "✅ aws:second-brain-jobs",
        "✅ aws:sso-default",
        "✅ graph:grasp-mcp",
        "✅ slack:slack-mcp",
        "=== all required credentials valid ===",
    ]
    assert "reason:" not in result.stdout
    assert "123456789012" not in result.stdout  # a probe's stdout is never echoed
    assert list(_scratch_dir(tmp_path).iterdir()) == []


@pytest.mark.parametrize(
    ("rc", "output", "cls"),
    [
        (255, "An error occurred (ExpiredToken) when calling the GetCallerIdentity operation: "
              "The security token included in the request is expired", "auth"),
        (255, "Unable to locate credentials. You can configure credentials by running \"aws configure\".", "auth"),
        (124, "", "timeout"),
        (137, "", "timeout"),
        (255, "Could not connect to the endpoint URL: \"https://sts.amazonaws.com/\"", "transport"),
        (1, "something unexpected happened", "other"),
    ],
)
def test_a_failed_aws_probe_logs_a_classified_reason(tmp_path: Path, rc, output, cls):
    result = _run_credentials(
        tmp_path, FAKE_AWS_FAIL_PROFILES="second-brain-jobs",
        FAKE_AWS_RC=str(rc), FAKE_AWS_OUTPUT=output,
    )

    assert result.returncode == 1
    # The status line and summary keep their exact shape (exit code included).
    assert f"❌ aws:second-brain-jobs EXPIRED/BROKEN (exit {rc})" in result.stdout
    assert "=== REQUIRED EXPIRED/BROKEN: aws:second-brain-jobs ===" in result.stdout
    reason = _reason_after(result.stdout, "❌ aws:second-brain-jobs")
    assert reason.startswith(f"{cls}: "), reason
    if output:
        assert output[:40] in reason
    else:
        assert reason == f"{cls}: (no output)"
    assert result.stdout.count("reason:") == 1  # one line per failed probe, none for ✅
    assert list(_scratch_dir(tmp_path).iterdir()) == []


@pytest.mark.parametrize(
    ("rc", "output", "cls"),
    [
        (3, "McpAuthError: get_emails: Authentication failed — your session has expired", "auth"),
        (4, "McpError: Connection closed", "transport"),
        (1, "AssertionError: get_emails: unexpected reply (keys: ['error'])", "other"),
        (1, "FileNotFoundError: [Errno 2] No such file or directory: '/x/grasp-mcp'", "tool-missing"),
    ],
)
def test_a_failed_mcp_probe_logs_its_class_from_the_capture_exit_codes(
    tmp_path: Path, rc, output, cls,
):
    result = _run_credentials(tmp_path, FAKE_PY_RC=str(rc), FAKE_PY_OUTPUT=output)

    assert result.returncode == 1
    for label in ("graph:grasp-mcp", "slack:slack-mcp"):
        assert f"❌ {label} EXPIRED/BROKEN (exit {rc})" in result.stdout
        assert _reason_after(result.stdout, f"❌ {label}") == f"{cls}: {output}"


@pytest.mark.parametrize(("rc", "not_cls"), [(3, "auth"), (4, "transport")])
def test_aws_exit_codes_3_and_4_are_not_read_as_the_mcp_probes_auth_or_transport(
    tmp_path: Path, rc, not_cls,
):
    """3/4 are EXIT_AUTH/EXIT_TRANSPORT only for the MCP probes; for aws they mean
    nothing special, so an uninformative message stays 'other'."""
    result = _run_credentials(
        tmp_path, FAKE_AWS_FAIL_PROFILES="second-brain-jobs",
        FAKE_AWS_RC=str(rc), FAKE_AWS_OUTPUT="something unexpected happened",
    )

    assert f"❌ aws:second-brain-jobs EXPIRED/BROKEN (exit {rc})" in result.stdout
    reason = _reason_after(result.stdout, "❌ aws:second-brain-jobs")
    assert not reason.startswith(f"{not_cls}: "), reason
    assert reason == "other: something unexpected happened"


def test_the_reason_is_the_last_meaningful_line_capped_at_160_characters(tmp_path: Path):
    output = "\n".join([
        "Traceback (most recent call last):",
        '  File "<stdin>", line 9, in <module>',
        "    ^^^^^^^^^^",
        "RuntimeError: " + "x " * 200,
        "",
    ])
    result = _run_credentials(tmp_path, FAKE_PY_RC="1", FAKE_PY_OUTPUT=output)

    reason = _reason_after(result.stdout, "❌ graph:grasp-mcp")
    cls, message = reason.split(": ", 1)
    assert cls == "other"
    assert message.startswith("RuntimeError: x x")
    assert len(message) <= 160
    assert "Traceback" not in result.stdout and 'File "' not in result.stdout


def test_secrets_in_probe_output_are_redacted_and_full_output_is_never_printed(tmp_path: Path):
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTIzNDU2Nzg5MA"
    hexrun = "0123456789abcdef0123456789abcdef"
    earlier = "debug: refresh_token=ZZEARLIERSECRET9 cookie dump follows"
    last = (f"invalid_grant: Bearer {jwt} token=s3cr3tValue&x=1 sig {hexrun} "
            "Cookie: midway=abc123; owner user@example.com AKIAABCDEFGHIJKLMNOP")
    result = _run_credentials(
        tmp_path, FAKE_AWS_FAIL_PROFILES="second-brain-jobs",
        FAKE_AWS_RC="255", FAKE_AWS_OUTPUT=f"{earlier}\n{last}",
    )

    reason = _reason_after(result.stdout, "❌ aws:second-brain-jobs")
    assert reason.startswith("auth: invalid_grant: Bearer [REDACTED]"), reason
    for secret in (jwt, "eyJhbGciOiJIUzI1NiJ9", "s3cr3tValue", hexrun, "abc123",
                   "AKIAABCDEFGHIJKLMNOP", "ZZEARLIERSECRET9"):
        assert secret not in result.stdout, secret
    assert "token=[REDACTED]" in reason
    assert "Cookie: [REDACTED]" in reason
    assert "debug:" not in result.stdout  # only the last line is ever logged


def test_an_email_address_and_ordinary_words_survive_redaction(tmp_path: Path):
    output = "AccessDenied: user someone.else@example.com is not authorized for sts:GetCallerIdentity"
    result = _run_credentials(
        tmp_path, FAKE_AWS_FAIL_PROFILES="second-brain-jobs",
        FAKE_AWS_RC="254", FAKE_AWS_OUTPUT=output,
    )

    assert _reason_after(result.stdout, "❌ aws:second-brain-jobs") == f"auth: {output}"


# Holes reproduced in the 2026-10-13 review against the real sanitize: each of these
# passed through unredacted before the xox/JWT rules, the ' quote and %.!* in $run.
@pytest.mark.parametrize(
    ("secret", "leaks"),
    [
        ("d=xoxd-abc%2Fdef%2Bghi%3D%3Djkl", ("xoxd", "abc%2F", "def%2B", "jkl")),
        ("{'refresh_token': 'M.C107_BAY.0.U.-Cnabc!def*ghi'}", ("M.C107", "BAY", "Cnabc", "def*ghi")),
        ("eyJhbGciOiJub25lIn0.eyJzdWIiOjF9.", ("eyJ", "hbGciOiJub25lIn0", "zdWIiOjF9")),
        ("xoxc-123-456-abc", ("xoxc", "123-456", "abc")),
    ],
    ids=["url-encoded-xoxd-cookie", "python-repr-refresh-token", "short-jwt", "short-xoxc"],
)
def test_review_redaction_holes_are_closed_and_an_email_still_survives(tmp_path: Path, secret, leaks):
    output = f"RuntimeError: rejected {secret} for someone.else@example.com"
    result = _run_credentials(
        tmp_path, FAKE_AWS_FAIL_PROFILES="second-brain-jobs",
        FAKE_AWS_RC="1", FAKE_AWS_OUTPUT=output,
    )

    reason = _reason_after(result.stdout, "❌ aws:second-brain-jobs")
    assert "[REDACTED]" in reason, reason
    for fragment in leaks:
        assert fragment not in result.stdout, (fragment, reason)
    assert reason.split(": ", 1)[1].startswith("RuntimeError: rejected "), reason
    assert reason.endswith(" for someone.else@example.com"), reason


def test_an_advisory_failure_also_explains_itself(tmp_path: Path):
    result = _run_credentials(
        tmp_path, FAKE_AWS_FAIL_PROFILES="default", FAKE_AWS_RC="255",
        FAKE_AWS_OUTPUT="Error when retrieving token from sso: Token has expired and refresh failed",
    )

    assert result.returncode == 0, result.stdout
    assert _reason_after(result.stdout, "⚠ aws:sso-default expired").startswith("auth: Error when retrieving")


def test_the_real_mcp_probe_classifies_a_dead_server_as_transport(tmp_path: Path):
    """Runs the script's own heredoc probes (real src.capture.mcp_client) against a fake
    server binary that exits at once — the shape of the 2026-09-21 grasp outage."""
    dead = _script(tmp_path / "dead-mcp", "#!/bin/bash\necho 'server going away' >&2\nexit 1\n")
    result = _run_credentials(
        tmp_path,
        SECOND_BRAIN_PYTHON=sys.executable,
        GRASP_MCP=str(dead),
        SLACK_MCP=str(tmp_path / "no-such-slack-mcp"),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "❌ graph:grasp-mcp EXPIRED/BROKEN (exit 4)" in result.stdout
    assert _reason_after(result.stdout, "❌ graph:grasp-mcp").startswith("transport: ")
    assert "❌ slack:slack-mcp EXPIRED/BROKEN (exit 1)" in result.stdout
    assert _reason_after(result.stdout, "❌ slack:slack-mcp").startswith("tool-missing: FileNotFoundError")
    assert list(_scratch_dir(tmp_path).iterdir()) == []




# --------------------------------------------------------------------------- #
# verify_liveness.sh (adapter around src.operations.liveness)
# --------------------------------------------------------------------------- #


def test_all_green_exits_zero_and_echoes_the_policy_lines(tmp_path: Path):
    result = _run_liveness(tmp_path, lines="✅ [channel:slack] ok (0d / max 4d)\n✅ [job:backup] job backup succeeded (exit 0)\n")

    assert result.returncode == 0, result.stdout
    assert "=== All monitored channels live ===" in result.stdout
    assert "✅ [channel:slack] ok" in result.stdout
    assert "✅ quick_upstream ok" in result.stdout
    assert "❌" not in result.stdout
    assert _notifications(tmp_path) == []
    assert any(a.startswith("ARGS -m src.operations.liveness --status-dir") for a in _policy_log(tmp_path))


def test_a_required_policy_failure_fails_the_job_and_notifies(tmp_path: Path):
    """The 74ba02d adapter stubbed this call to exit 0 in tests (E mutation: 14/14 still
    passed with the integration removed). A ❌ from the policy module must reach the
    exit code, the summary line and the notification."""
    result = _run_liveness(
        tmp_path, rc=1,
        lines="❌ [channel:slack] SILENT — last capture 99d ago (max 4d)\n⚠ [channel:distilled_chat] silent 14d — advisory\n"
              "❌ [job:backup] job backup last exit 69 (1 consecutive, first failure 0.3d ago)\n",
    )

    assert result.returncode != 0, result.stdout
    assert "❌ [channel:slack] SILENT — last capture 99d ago" in result.stdout
    assert "=== SILENT CHANNELS: channel:slack job:backup ===" in result.stdout
    assert "durable report:" in result.stdout and "liveness-report.json" in result.stdout
    assert "advisory, NOT failing the job: distilled_chat" in result.stdout
    assert len(_notifications(tmp_path)) == 1


def test_advisory_only_output_keeps_the_job_green(tmp_path: Path):
    """E-N6: known-disabled producers (distill, weekly digest) are ⚠, never ❌."""
    result = _run_liveness(
        tmp_path, rc=0,
        lines="⚠ [channel:distilled_chat] silent 14d (max 7d) — advisory: launchctl-disabled\n"
              "⚠ [channel:weekly_digest] silent 99999d (max 14d) — advisory\n⚠ [zero_output:slack] produced zero rows for 3d\n",
    )

    assert result.returncode == 0, result.stdout
    assert "=== advisory, NOT failing the job: distilled_chat weekly_digest slack ===" in result.stdout
    assert _notifications(tmp_path) == []


def test_policy_module_crash_is_a_failure_not_a_silent_pass(tmp_path: Path):
    result = _run_liveness(tmp_path, rc=3, lines="Traceback: synthetic crash\n")

    assert result.returncode != 0
    assert "=== SILENT CHANNELS: policy-exit-3 ===" in result.stdout
    assert len(_notifications(tmp_path)) == 1


def test_unreachable_database_fails_but_still_sweeps_job_statuses(tmp_path: Path):
    result = _run_liveness(tmp_path, pg_ready=False, lines="⚠ [job:backup] job backup: no status file yet\n")

    assert result.returncode != 0
    assert "❌ database not reachable" in result.stdout
    assert "database(unreachable)" in result.stdout
    assert any("--skip-db" in a for a in _policy_log(tmp_path)), "the status sweep must still run without the DB"


def test_a_stale_upstream_quick_export_is_reported_as_such_not_as_our_breakage(tmp_path: Path):
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


def test_liveness_database_target_comes_from_env_not_hardcoded_production(tmp_path: Path):
    """The monitor must pass DB_HOST/DB_PORT/DB_NAME/DB_USER through so a test (or a
    staging host) can never make it query the production database by default
    path. 1071a81 hard-coded ``-h 127.0.0.1 -p 5432 -d memory_bank``."""
    result = _run_liveness(tmp_path, DB_HOST=str(tmp_path / "socket"), DB_PORT="5999",
                           DB_NAME="sb_liveness_test", DB_USER="sb_test")

    assert result.returncode == 0, result.stdout
    seen = dict(line.split("=", 1) for line in _policy_log(tmp_path) if "=" in line)
    assert seen == {"DB_HOST": str(tmp_path / "socket"), "DB_PORT": "5999", "DB_NAME": "sb_liveness_test", "DB_USER": "sb_test"}
    # Defaults must stay the production target so the installed job is unchanged.
    script = LIVENESS.read_text(encoding="utf-8")
    assert 'DB_HOST="${DB_HOST:-127.0.0.1}"' in script
    assert 'DB_PORT="${DB_PORT:-5432}"' in script
    assert 'DB_NAME="${DB_NAME:-memory_bank}"' in script
    assert "-h 127.0.0.1" not in script and "-p 5432" not in script
    assert "psql" not in "\n".join(l for l in script.splitlines() if not l.lstrip().startswith("#"))
