"""scripts/jobs/backup.sh / verify_backup.sh — only ciphertext ever leaves the machine.

Until 2026-10-05 the nightly backup `rclone sync`ed ``backups/kb-sources/`` (the
personal knowledge corpus) and ``backups/config/`` (docs, migrations, Kiro
skills) to Google Drive in plaintext, next to the encrypted dump. These tests
run the real script with every external tool stubbed (no database, no gpg, no
network) and record each rclone/rsync call the job makes, then assert that
every remote-bound transfer is restricted to ``*.gpg``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
BACKUP = REPO / "scripts/jobs/backup.sh"
VERIFY = REPO / "scripts/jobs/verify_backup.sh"

# rclone subcommands that move data toward a remote.
RCLONE_TRANSFER_CMDS = {"copy", "copyto", "sync", "move", "moveto", "rcat", "bisync", "copyurl"}


def _script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _recorder(tmp_path: Path, name: str, log: Path, *, extra: str = "") -> Path:
    """A stub tool that appends its argv (JSON) to ``log`` and exits 0."""
    return _script(
        tmp_path / name,
        f"#!/bin/sh\n{extra}exec {shlex.quote(sys.executable)} - {shlex.quote(name)} {shlex.quote(str(log))} \"$@\" <<'PYTHON'\n"
        "import json, sys\n"
        "name, log, *argv = sys.argv[1:]\n"
        "with open(log, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps([name, *argv]) + '\\n')\n"
        "PYTHON\n",
    )


def _run_backup(tmp_path: Path) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.jsonl"
    home = tmp_path / "home"
    (home / "second-brain").mkdir(parents=True)
    (home / "second-brain" / ".backup-key").write_text("synthetic-key\n", encoding="utf-8")
    backup_dir = tmp_path / "backups"

    # Database tools: pg_isready ok; pg_dump/psql emit a byte so the dump files exist.
    _script(bin_dir / "pg_isready", "#!/bin/sh\nexit 0\n")
    _script(bin_dir / "pg_dump", "#!/bin/sh\necho dump\n")
    _script(bin_dir / "psql", "#!/bin/sh\necho 42\n")
    _script(bin_dir / "brew", "#!/bin/sh\nexit 0\n")
    # gpg: write "<file>.gpg" so the later rm and rclone see the expected layout.
    _script(bin_dir / "gpg", '#!/bin/sh\nfor a in "$@"; do f="$a"; done\ncp "$f" "$f.gpg"\n')
    # Record every remote-capable transfer tool. rclone lsf must also echo the
    # file it is asked about so the verification branch is exercised.
    _recorder(tmp_path / "bin", "rsync", calls)
    _recorder(
        tmp_path / "bin", "rclone", calls,
        extra='prev=""; inc=""\n'
              'for a in "$@"; do if [ "$prev" = "--include" ]; then inc="$a"; fi; prev="$a"; done\n'
              'if [ "$1" = lsf ]; then echo "$inc"; fi\n',
    )
    env = {
        "PATH": "/usr/bin:/bin",
        # backup.sh prepends the Homebrew tool dirs itself; this hook puts the
        # stubs ahead of them so the real pg_isready/gpg/rclone never run.
        "SECOND_BRAIN_TOOL_PATH": str(bin_dir),
        "HOME": str(home),
        "BACKUP_DIR": str(backup_dir),
        "TMPDIR": str(tmp_path),
    }
    result = subprocess.run(["bash", str(BACKUP)], env=env, capture_output=True, text=True, check=False)
    recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()] if calls.exists() else []
    return result, recorded


def test_backup_job_transfers_only_gpg_ciphertext_to_the_remote(tmp_path: Path):
    result, calls = _run_backup(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Backup complete" in result.stdout

    rclone_calls = [c[1:] for c in calls if c[0] == "rclone"]
    assert rclone_calls, "backup.sh never called rclone"
    transfers = [argv for argv in rclone_calls if argv and argv[0] in RCLONE_TRANSFER_CMDS]
    assert transfers, "expected at least the encrypted upload"
    for argv in transfers:
        assert argv[0] == "copy", f"only `rclone copy` of ciphertext is allowed, got: {argv}"
        assert "--include" in argv and argv[argv.index("--include") + 1] == "*.gpg", argv
        joined = " ".join(argv)
        assert "kb-sources" not in joined and "config" not in joined, argv
    # No `rclone sync` of any kind, and nothing remote-bound mentions the plaintext trees.
    assert not [a for a in rclone_calls if a and a[0] == "sync"]
    for argv in rclone_calls:
        for arg in argv:
            if ":" in arg and not arg.startswith("-"):  # a remote path
                assert "kb-sources" not in arg and "config" not in arg, argv
    # rsync is local-only (the KB copy stays on disk): no remote (host:) targets.
    for argv in (c[1:] for c in calls if c[0] == "rsync"):
        assert not any(re.match(r"^[^/\s]+:", a) for a in argv if not a.startswith("-")), argv


def test_backup_script_text_has_no_plaintext_remote_sync():
    """Static pin, independent of the stubbed run: no `rclone sync` left, and
    the only `rclone copy` is the *.gpg-filtered one."""
    text = BACKUP.read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"^\s*rclone\s+sync\b", code, re.M)
    for m in re.finditer(r"^\s*rclone\s+(copy|copyto|move|moveto|sync)\b.*$", code, re.M):
        assert '--include "*.gpg"' in m.group(0), m.group(0)
    assert "gdrive:memory-bank-backups/kb-sources" not in code
    assert "gdrive:memory-bank-backups/config" not in code
    # Local copies of KB sources and config are still taken.
    assert "kb-sources" in code and "$BACKUP_DIR/config" in code
    # The encrypted upload and its verification are unchanged.
    assert 'rclone copy "$BACKUP_DIR/" gdrive:memory-bank-backups/ --include "*.gpg"' in code
    assert 'rclone lsf gdrive:memory-bank-backups/ --files-only --include "$REMOTE_FILE"' in code


def test_verify_backup_does_not_expect_remote_plaintext_trees():
    text = VERIFY.read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "kb-sources" not in code and "/config/" not in code
    assert '--include "*.dump.gpg"' in code
