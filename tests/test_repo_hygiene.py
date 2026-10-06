"""Repo hygiene: secrets must never live in tracked configuration.

On 2026-09-14 a live CAPTURE_API_TOKEN was found inline in the tracked launchd
plist. The token was rotated and moved to the gitignored `.env`; these tests keep
it that way for every path launchd or a job can read: the committed plists, the
job scripts and adapters, the policy files under config/, and — when this test
runs on the installed Mac — the plists actually installed under
~/Library/LaunchAgents (symlinks or copies; F-141).
"""

from __future__ import annotations

import os
import pwd
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

_SECRET_KEY = re.compile(r"(TOKEN|SECRET|PASSWORD|API_KEY|PASSPHRASE)", re.IGNORECASE)
_LONG_HEX = re.compile(r"\b[0-9a-f]{40,}\b")
_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|PASSPHRASE)[A-Z0-9_]*)=(['\"]?)([^'\"\s$]{8,})\2\s*$", re.M)


def _tracked(patterns: list[str]) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "--", *patterns], cwd=REPO, capture_output=True, text=True, check=True)
    return [REPO / p for p in out.stdout.split() if p]


def _plist_offenders(plist: Path, text: str) -> list[str]:
    offenders = []
    # Any EnvironmentVariables key that smells like a secret with a non-empty literal.
    for m in re.finditer(r"<key>([^<]+)</key>\s*<string>([^<]*)</string>", text):
        key, value = m.group(1), m.group(2).strip()
        if _SECRET_KEY.search(key) and value:
            offenders.append(f"{plist}: {key}")
    if _LONG_HEX.search(text):
        offenders.append(f"{plist}: long hex literal")
    return offenders


class TestNoSecretsInTrackedConfig:
    def test_launchd_plists_carry_no_secret_values(self):
        offenders = []
        for plist in _tracked(["scheduling/*.plist"]):
            offenders += _plist_offenders(plist.relative_to(REPO), plist.read_text())
        assert not offenders, f"secret-looking values in tracked plists: {offenders}"

    def test_job_scripts_and_config_carry_no_literal_secrets(self):
        """F-141: the plists were the only scanned path; the adapters, the maintenance
        scripts and the TOML/config files are read by the same jobs."""
        offenders = []
        for path in _tracked(["scripts/jobs/*", "scripts/maintenance/*.py", "config/*", "migrations/migrate.sh"]):
            if not path.is_file():
                continue
            text = path.read_text(errors="replace")
            for m in _ASSIGNMENT.finditer(text):
                offenders.append(f"{path.relative_to(REPO)}: {m.group(1)}={m.group(3)[:4]}…")
            if _LONG_HEX.search(text):
                offenders.append(f"{path.relative_to(REPO)}: long hex literal")
        assert not offenders, f"secret-looking literals: {offenders}"

    def test_installed_plists_carry_no_secret_values_and_point_at_the_checkout(self):
        """Runs only where the jobs are installed; scans copies and symlinks alike (a
        copy silently keeps an old definition after a checkout — the R5 runbook
        replaces the two remaining copies with symlinks)."""
        # The isolated runner redirects $HOME; the installed agents live in the account home.
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        installed = sorted(home.glob("Library/LaunchAgents/com.second-brain.*.plist"))
        if not installed:
            pytest.skip("no installed Second Brain LaunchAgents on this host")
        offenders = []
        for plist in installed:
            offenders += _plist_offenders(plist, plist.read_text())
        assert not offenders, f"secret-looking values in installed plists: {offenders}"

    def test_env_file_is_gitignored_and_untracked(self):
        assert not _tracked([".env"]), ".env must never be tracked"
        res = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=REPO)
        assert res.returncode == 0, ".env must be covered by .gitignore"
        for artefact in ("backups/x.tar.gpg", "backups/x.receipt.json", "logs/jobs/x.json", ".backup-key"):
            assert subprocess.run(["git", "check-ignore", "-q", artefact], cwd=REPO).returncode == 0, artefact

    def test_capture_api_plist_delegates_secrets_to_launcher(self):
        text = (REPO / "scheduling" / "com.second-brain.capture-api.plist").read_text()
        assert "CAPTURE_API_TOKEN</key>" not in text
        assert "scripts/jobs/capture_api.sh" in text
