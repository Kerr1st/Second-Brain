"""Repo hygiene: secrets must never live in tracked configuration.

On 2026-09-14 a live CAPTURE_API_TOKEN was found inline in the tracked launchd
plist (and had transited GitHub in since-deleted branches). The token was
rotated and moved to the gitignored `.env`; these tests keep it that way.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

_SECRET_KEY = re.compile(r"(TOKEN|SECRET|PASSWORD|API_KEY|PASSPHRASE)", re.IGNORECASE)
_LONG_HEX = re.compile(r"\b[0-9a-f]{40,}\b")


def _tracked(patterns: list[str]) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z", "--", *patterns], cwd=REPO, capture_output=True, text=True, check=True)
    return [REPO / p for p in out.stdout.split("\0") if p and (REPO / p).is_file()]


class TestNoSecretsInTrackedConfig:
    def test_launchd_plists_carry_no_secret_values(self):
        offenders = []
        for plist in _tracked(["scheduling/*.plist"]):
            text = plist.read_text()
            # Any EnvironmentVariables key that smells like a secret with a non-empty literal.
            for m in re.finditer(r"<key>([^<]+)</key>\s*<string>([^<]*)</string>", text):
                key, value = m.group(1), m.group(2).strip()
                if _SECRET_KEY.search(key) and value:
                    offenders.append(f"{plist.relative_to(REPO)}: {key}")
            if _LONG_HEX.search(text):
                offenders.append(f"{plist.relative_to(REPO)}: long hex literal")
        assert not offenders, f"secret-looking values in tracked plists: {offenders}"

    def test_env_file_is_gitignored_and_untracked(self):
        assert not _tracked([".env"]), ".env must never be tracked"
        res = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=REPO)
        assert res.returncode == 0, ".env must be covered by .gitignore"

    def test_capture_api_plist_delegates_secrets_to_launcher(self):
        text = (REPO / "scheduling" / "com.second-brain.capture-api.plist").read_text()
        assert "CAPTURE_API_TOKEN</key>" not in text
        assert "scripts/jobs/capture_api.sh" in text


class TestCaptureApiLauncherFailsClosed:
    """The launcher must refuse to start the server when .env / the token is
    missing — never fall through to an unauthenticated service."""

    def _run_launcher_in(self, tmp_path: Path, env_body: str | None):
        # Exercise the actual portable repository layout in a private fixture.
        import shutil
        target=tmp_path / "scripts" / "jobs" / "capture_api.sh"
        target.parent.mkdir(parents=True)
        shutil.copyfile(REPO / "scripts" / "jobs" / "capture_api.sh", target)
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        stub = tmp_path / ".venv" / "bin" / "python"
        stub.write_text('#!/bin/bash\necho "STARTED with token set: ${CAPTURE_API_TOKEN:+yes}"\n')
        stub.chmod(0o755)
        if env_body is not None:
            (tmp_path / ".env").write_text(env_body)
        return subprocess.run(["bash", str(target)], capture_output=True, text=True,
                              env={**os.environ, "CAPTURE_API_TOKEN": ""})

    def test_missing_env_file_exits_nonzero(self, tmp_path):
        res = self._run_launcher_in(tmp_path, env_body=None)
        assert res.returncode == 78 and "not found" in res.stderr
        assert "STARTED" not in res.stdout

    def test_env_without_token_exits_nonzero(self, tmp_path):
        res = self._run_launcher_in(tmp_path, env_body="OTHER=1\n")
        assert res.returncode == 78 and "not set" in res.stderr

    def test_env_with_token_starts_server_with_token_exported(self, tmp_path):
        res = self._run_launcher_in(tmp_path, env_body="CAPTURE_API_TOKEN=abc123\n")
        assert res.returncode == 0
        assert "STARTED with token set: yes" in res.stdout


class TestDocumentationContracts:
    def test_generated_inventory_matches_source(self):
        import json
        from scripts.docs.inventory import snapshot,markdown
        facts=snapshot(REPO)
        assert json.loads((REPO/'docs/generated/runtime-inventory.json').read_text())==facts
        assert (REPO/'docs/generated/runtime-inventory.md').read_text()==markdown(facts)

    def test_hazards_have_owners_and_closure_evidence(self):
        text=(REPO/'docs/VALIDATION.md').read_text()
        rows=[line.split('|')[1:-1] for line in text.splitlines() if line.startswith('| H-')]
        assert len(rows)>=9
        for row in rows:
            assert len(row)==4 and all(part.strip() for part in row)
            assert row[1].strip() not in {'TBD','Unknown','Unassigned'}

    def test_canonical_links_resolve(self):
        documents=[REPO/'README.md',*(REPO/'docs').glob('*.md')]
        # Audit records and dated execution histories intentionally retain original links.
        for doc in documents:
            if doc.name.startswith(('AUDIT-','REFACTOR-')):continue
            for target in re.findall(r'\]\(([^)]+)\)',doc.read_text()):
                target=target.split('#',1)[0]
                if not target or '://' in target or target.startswith('mailto:'):continue
                assert (doc.parent/target).exists(),f'{doc.name}: {target}'

    def test_fixture_identifiers_and_local_secrets_stay_out(self):
        for path in (REPO/'tests/fixtures/codex').glob('*'):
            if path.suffix not in {'.json','.jsonl'}:continue
            text=path.read_text()
            assert '/Users/kerr' not in text and '@amazon.com' not in text
        assert not _tracked(['.backup-key','*.tar.gpg','*.receipt.json','.maintenance/*','.dream/*'])
