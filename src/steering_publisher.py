"""Reviewable, conflict-safe publication of approved rules to AGENTS.md."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from src.db import get_memory


@dataclass(frozen=True, slots=True)
class PublicationPreview:
    rule_id: str
    path: str
    current_digest: str
    proposed_content: str
    diff: str
    changed: bool


@dataclass(frozen=True, slots=True)
class PublicationResult:
    rule_id: str
    path: str
    changed: bool
    backup_path: str | None
    published_digest: str


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _metadata(memory: dict) -> dict:
    value = memory.get("metadata") or {}
    return json.loads(value) if isinstance(value, str) else dict(value)


def _approved_rule(rule_id: str) -> tuple[dict, dict]:
    rule = get_memory(rule_id)
    if (
        rule is None
        or rule.get("type") != "steering_rule"
        or rule.get("status") != "active"
    ):
        raise ValueError("only an active approved Steering Rule can be published")
    metadata = _metadata(rule)
    if metadata.get("authority") != "approved" or metadata.get("lifecycle") != "active":
        raise ValueError("the Steering Rule is not approved and active")
    return rule, metadata


def _markers(rule_id: str) -> tuple[str, str]:
    return (
        f"<!-- second-brain:steering-rule:{rule_id}:start -->",
        f"<!-- second-brain:steering-rule:{rule_id}:end -->",
    )


def _remove_managed_block(content: str, rule_id: str) -> str:
    start, end = _markers(rule_id)
    pattern = re.compile(
        rf"\n?{re.escape(start)}.*?{re.escape(end)}\n?",
        flags=re.DOTALL,
    )
    return pattern.sub("\n", content)


def _render_block(rule_id: str, rule: dict, metadata: dict) -> str:
    start, end = _markers(rule_id)
    version = int(metadata.get("rule_version", 1))
    scope = metadata.get("authority_scope", "project")
    applicability = json.dumps(
        metadata.get("applicability") or {}, sort_keys=True, separators=(",", ":")
    )
    return "\n".join(
        (
            start,
            f"### {rule['title']} (v{version})",
            "",
            rule["content"].strip(),
            "",
            f"Scope: `{scope}`. Applicability: `{applicability}`.",
            f"Evidence: Second Brain Steering Rule `{rule_id}`.",
            end,
        )
    )


def _proposed_content(current: str, rule_id: str, rule: dict, metadata: dict) -> str:
    content = current
    supersedes = metadata.get("supersedes_rule_id")
    if supersedes:
        content = _remove_managed_block(content, supersedes)
    content = _remove_managed_block(content, rule_id).rstrip()
    heading = "## Approved Second Brain steering rules"
    block = _render_block(rule_id, rule, metadata)
    if heading not in content:
        content = f"{content}\n\n{heading}\n\n{block}\n"
    else:
        content = f"{content}\n\n{block}\n"
    return content


def _validate_path(path: Path) -> None:
    if path.name != "AGENTS.md":
        raise ValueError("the AGENTS publisher can write only an AGENTS.md file")
    if path.is_symlink():
        raise ValueError("refusing to publish through a symbolic link")


def preview_agents_rule(rule_id: str, path: str | Path) -> PublicationPreview:
    """Return the exact diff and stale-write digest without modifying the file."""
    target = Path(path)
    _validate_path(target)
    rule, metadata = _approved_rule(rule_id)
    current = target.read_text(encoding="utf-8") if target.exists() else ""
    proposed = _proposed_content(current, rule_id, rule, metadata)
    diff = "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=str(target),
            tofile=str(target),
        )
    )
    return PublicationPreview(
        rule_id=rule_id,
        path=str(target),
        current_digest=_digest(current),
        proposed_content=proposed,
        diff=diff,
        changed=current != proposed,
    )


def publish_agents_rule(
    rule_id: str,
    path: str | Path,
    *,
    expected_current_digest: str,
) -> PublicationResult:
    """Atomically publish the exact reviewed rule if the target has not changed."""
    target = Path(path)
    preview = preview_agents_rule(rule_id, target)
    if preview.current_digest != expected_current_digest:
        raise RuntimeError("AGENTS.md changed after review; generate a new preview")
    if not preview.changed:
        return PublicationResult(
            rule_id=rule_id,
            path=str(target),
            changed=False,
            backup_path=None,
            published_digest=_digest(preview.proposed_content),
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    current = target.read_text(encoding="utf-8") if target.exists() else ""
    backup_dir = target.parent / ".second-brain-backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"AGENTS.md.{stamp}.{preview.current_digest[:12]}.bak"
    backup.write_text(current, encoding="utf-8")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".AGENTS.md.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(preview.proposed_content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, target)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return PublicationResult(
        rule_id=rule_id,
        path=str(target),
        changed=True,
        backup_path=str(backup),
        published_digest=_digest(preview.proposed_content),
    )
