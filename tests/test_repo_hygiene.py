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


class TestDocumentationContracts:
    """R7 documentation guardrails (PLAN-refactor Phase 6/7): counted facts are
    generated from source, hazards have owners, canonical links resolve, and the
    live documents carry no personal identifiers."""

    # Live documentation = what START-HERE points at. Dated archives and the two
    # audit records keep their original text and are deliberately not scanned.
    LIVE_DOC_GLOBS = ("README.md", "scheduling/README.md", "tests/README.md", "docs/*.md",
                      "docs/generated/*", ".kiro/specs/*/*")
    HISTORICAL_PREFIXES = ("AUDIT-",)

    def _live_docs(self) -> list[Path]:
        docs = []
        for pattern in self.LIVE_DOC_GLOBS:
            # `git ls-files -- 'docs/*.md'` recurses; keep the top level of docs/ only.
            for p in _tracked([pattern]):
                rel = p.relative_to(REPO).as_posix()
                if not p.is_file() or p.name.startswith(self.HISTORICAL_PREFIXES):
                    continue
                if rel.startswith(("docs/archive/", "docs/audit-")):
                    continue
                docs.append(p)
        assert docs, "no live documentation tracked"
        return sorted(set(docs))

    def test_generated_inventory_matches_source(self):
        import json
        import sys
        sys.path.insert(0, str(REPO))
        from scripts.docs.inventory import markdown, snapshot
        facts = snapshot(REPO)
        assert json.loads((REPO / "docs/generated/runtime-inventory.json").read_text()) == facts, \
            "docs/generated/runtime-inventory.json drifted: run .venv/bin/python scripts/docs/inventory.py"
        assert (REPO / "docs/generated/runtime-inventory.md").read_text() == markdown(facts)
        # The generated files must never carry the installed Mac's home path (plist arguments are relativized).
        assert "/Users/" not in (REPO / "docs/generated/runtime-inventory.json").read_text()
        assert facts["default_profile"] == "controlled" and facts["migration_high_water"] == "018_retire_knowledge_graph.sql"

    def test_hazards_have_owners_and_closure_evidence(self):
        text = (REPO / "docs/VALIDATION.md").read_text()
        rows = [line.split("|")[1:-1] for line in text.splitlines() if line.startswith("| H-")]
        assert len(rows) >= 9, "VALIDATION.md must keep the owned-hazards table"
        ids = [row[0].strip() for row in rows]
        assert len(ids) == len(set(ids)), "duplicate hazard ids"
        for row in rows:
            assert len(row) == 4 and all(part.strip() for part in row), row
            assert row[1].strip() not in {"TBD", "Unknown", "Unassigned", "—"}, row
        for required in ("H-ISOLATION", "H-ROUTING", "H-RECOVERY", "H-PRIVILEGE", "H-GRAPH-DATA", "H-EFFECTIVENESS"):
            assert required in ids, required

    def test_canonical_links_resolve(self):
        documents = [REPO / "README.md", REPO / "scheduling/README.md", *sorted((REPO / "docs").glob("*.md")),
                     *sorted((REPO / ".kiro/specs").glob("*/*.md")),
                     *sorted((REPO / "docs/archive").glob("*/README*.md"))]
        broken = []
        for doc in documents:
            if doc.name.startswith(self.HISTORICAL_PREFIXES):
                continue
            for target in re.findall(r"\]\(([^)]+)\)", doc.read_text()):
                target = target.split("#", 1)[0]
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                if not (doc.parent / target).exists():
                    broken.append(f"{doc.relative_to(REPO)}: {target}")
        assert not broken, broken

    def test_live_docs_carry_no_personal_identifiers(self):
        """No e-mail addresses of the operator's employer domain and no absolute home
        directory paths in the live documents (the installed Mac's paths belong in the
        private release notes). Archived snapshots are exempt and tracked as-is."""
        offenders = []
        for doc in self._live_docs():
            text = doc.read_text(errors="replace")
            for needle in ("@amazon.com", "/Users/"):
                if needle in text:
                    line = next(i + 1 for i, l in enumerate(text.splitlines()) if needle in l)
                    offenders.append(f"{doc.relative_to(REPO)}:{line}: {needle}")
        assert not offenders, offenders

    def test_retired_vocabulary_stays_out_of_live_docs(self):
        """One name per concept: the panel is seats + a three-of-four rule; the
        fault-tolerance analogy lives only in the archives."""
        pattern = re.compile(r"byzantine|\bBFT\b|\bquorum\b", re.IGNORECASE)
        offenders = [f"{doc.relative_to(REPO)}" for doc in self._live_docs() if pattern.search(doc.read_text(errors="replace"))]
        assert not offenders, offenders

    def test_graph_tables_are_not_described_as_live(self):
        """After migration 018 no live guide may present the knowledge-graph tables
        as part of the running system (archives keep the history)."""
        live_claims = re.compile(r"(dormant|imported) knowledge graph|JSON exports? \(memories, entities|Slack graph\)|graph stage", re.IGNORECASE)
        offenders = [f"{doc.relative_to(REPO)}" for doc in self._live_docs() if live_claims.search(doc.read_text(errors="replace"))]
        assert not offenders, offenders
