"""Read Codex Desktop tasks from its local SQLite index and rollout JSONL.

This adapter is deliberately read-only. It normalizes only user-authored prompt
events and visible final assistant outcomes; developer context, reasoning,
tools, and commentary remain outside the capture boundary.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Iterable

from src.capture.agent_tasks import (
    AttachmentDescriptor,
    AgentTaskRef,
    AgentTaskSnapshot,
    AgentTurn,
    ProvenanceField,
)


SOURCE_TYPE = "codex_desktop"

_REQUIRED_THREAD_COLUMNS = frozenset(
    {
        "id",
        "rollout_path",
        "created_at",
        "updated_at",
        "source",
        "cwd",
        "title",
        "archived",
        "agent_path",
        "created_at_ms",
        "updated_at_ms",
        "thread_source",
    }
)
_REQUIRED_SPAWN_EDGE_COLUMNS = frozenset({"child_thread_id"})


class CodexSourceCompatibilityError(RuntimeError):
    """The local Codex state schema cannot be read safely by this adapter."""


class CodexSourceParseError(RuntimeError):
    """A Codex rollout is malformed and cannot be normalized safely."""


class CodexSourceChangedDuringRead(RuntimeError):
    """A Codex rollout changed while it was being read and must be retried."""


class _TaskOwnership(str, Enum):
    USER_OWNED = "user-owned"
    DELEGATED = "delegated"
    UNKNOWN = "unknown"


def _require_columns(
    conn: sqlite3.Connection,
    table: str,
    required: frozenset[str],
) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if not columns:
        raise CodexSourceCompatibilityError(
            f"incompatible Codex state database: missing required table: {table}"
        )
    missing = sorted(required - columns)
    if missing:
        raise CodexSourceCompatibilityError(
            f"incompatible Codex state database: {table} is missing required "
            f"column(s): {', '.join(missing)}"
        )


def _utc_timestamp(milliseconds: int | None, seconds: int) -> datetime:
    value = milliseconds / 1000 if milliseconds is not None else seconds
    return datetime.fromtimestamp(value, tz=UTC)


def _task_ownership(
    source: str,
    thread_source: str | None,
    agent_path: str | None,
    spawned_child: bool,
) -> _TaskOwnership:
    """Classify one native Codex Task from structured ownership evidence."""
    if spawned_child or agent_path or thread_source == "subagent":
        return _TaskOwnership.DELEGATED
    try:
        parsed = json.loads(source)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict) and "subagent" in parsed:
        return _TaskOwnership.DELEGATED
    if thread_source == "user":
        return _TaskOwnership.USER_OWNED
    return _TaskOwnership.UNKNOWN


def _fallback_turn_key(task_id: str, timestamp: str) -> str:
    """Derive a content-independent ID from a native event timestamp."""
    evidence = f"{task_id}\n{timestamp}".encode("utf-8")
    return "event-" + hashlib.sha256(evidence).hexdigest()


def _record_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _rollout_records(source_locator: str):
    """Yield complete JSONL records, ignoring only an in-progress final append."""
    path = Path(source_locator)
    before = path.stat()
    before_fingerprint = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    with path.open(encoding="utf-8") as rollout:
        lines = rollout.readlines()
    records = []
    for line_number, line in enumerate(lines, start=1):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            is_final_partial = (
                line_number == len(lines)
                and not line.endswith(("\n", "\r"))
            )
            if is_final_partial:
                break
            raise CodexSourceParseError(
                "invalid Codex rollout JSONL: malformed interior record "
                f"at line {line_number}"
            ) from exc
    after = path.stat()
    after_fingerprint = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if after_fingerprint != before_fingerprint:
        raise CodexSourceChangedDuringRead(
            "Codex rollout changed during read; retry the task"
        )
    yield from records


def _local_image_descriptors(
    payload: dict,
    event_key: str,
) -> tuple[AttachmentDescriptor, ...]:
    """Describe verified ``local_images`` string references without reading bytes."""
    local_images = payload.get("local_images")
    if not isinstance(local_images, list):
        return ()

    descriptors = []
    for index, reference in enumerate(local_images):
        if not isinstance(reference, str) or not reference:
            continue
        path = Path(reference)
        try:
            byte_size = path.stat().st_size
        except OSError:
            byte_size = None
        content_type, _ = mimetypes.guess_type(path.name)
        descriptors.append(
            AttachmentDescriptor(
                attachment_key=f"{event_key}:local-image:{index}",
                media_kind="image",
                filename=path.name or None,
                content_type=content_type,
                byte_size=byte_size,
                source_reference=reference,
                reference_is_durable=False,
            )
        )
    return tuple(descriptors)


def _explicit_codex_project(
    codex_home: Path,
    task_id: str,
) -> tuple[str | None, str | None, str | None]:
    """Resolve only an explicit, internally consistent Codex Project assignment."""
    state_path = codex_home / ".codex-global-state.json"
    try:
        with state_path.open(encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None, None, None
    if not isinstance(state, dict):
        return None, None, None

    assignments = state.get("thread-project-assignments")
    projects = state.get("local-projects")
    if not isinstance(assignments, dict) or not isinstance(projects, dict):
        return None, None, None
    assignment = assignments.get(task_id)
    if not isinstance(assignment, dict) or assignment.get("projectKind") != "local":
        return None, None, None
    project_id = assignment.get("projectId")
    if not isinstance(project_id, str) or not project_id:
        return None, None, None
    project = projects.get(project_id)
    if not isinstance(project, dict) or project.get("id") != project_id:
        return None, None, None

    name = project.get("name")
    roots = project.get("rootPaths")
    root = next(
        (
            candidate
            for candidate in roots
            if isinstance(candidate, str) and candidate
        ),
        None,
    ) if isinstance(roots, list) else None
    return (
        project_id,
        name if isinstance(name, str) and name else None,
        root,
    )


def _sqlite_git_provenance(
    state_path: Path,
    task_id: str,
) -> tuple[str | None, str | None, str | None]:
    """Read nullable git summary fields when this Codex schema provides them."""
    uri = f"file:{state_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(threads)")
        }
        if not {"git_origin_url", "git_branch", "git_sha"} <= columns:
            return None, None, None
        row = conn.execute(
            """
            SELECT git_origin_url, git_branch, git_sha
            FROM threads
            WHERE id = ?
            """,
            (task_id,),
        ).fetchone()
    if row is None:
        return None, None, None
    return tuple(
        value if isinstance(value, str) and value else None
        for value in row
    )


class CodexDesktopSource:
    """Read user-owned Codex Desktop Tasks without modifying native state."""

    source_type = SOURCE_TYPE

    def __init__(self, codex_home: str | Path):
        self.codex_home = Path(codex_home).expanduser()
        self._state_path = self.codex_home / "state_5.sqlite"
        self._observed_at: datetime | None = None
        self.skipped_delegated = 0
        self.skipped_unknown_ownership = 0

    def enumerate_tasks(self, now: datetime) -> Iterable[AgentTaskRef]:
        """Enumerate user-owned task references from Codex's local index."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include a timezone")
        self._observed_at = now.astimezone(UTC)
        self.skipped_delegated = 0
        self.skipped_unknown_ownership = 0

        uri = f"file:{self._state_path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            _require_columns(conn, "threads", _REQUIRED_THREAD_COLUMNS)
            _require_columns(
                conn,
                "thread_spawn_edges",
                _REQUIRED_SPAWN_EDGE_COLUMNS,
            )
            rows = conn.execute(
                """
                SELECT
                    t.id,
                    t.rollout_path,
                    t.created_at,
                    t.updated_at,
                    t.source,
                    t.cwd,
                    t.title,
                    t.archived,
                    t.agent_path,
                    t.created_at_ms,
                    t.updated_at_ms,
                    t.thread_source,
                    e.child_thread_id IS NOT NULL AS spawned_child
                FROM threads AS t
                LEFT JOIN thread_spawn_edges AS e
                    ON e.child_thread_id = t.id
                ORDER BY COALESCE(t.created_at_ms, t.created_at * 1000), t.id
                """
            ).fetchall()

        refs = []
        for row in rows:
            ownership = _task_ownership(
                row["source"],
                row["thread_source"],
                row["agent_path"],
                bool(row["spawned_child"]),
            )
            if ownership is _TaskOwnership.DELEGATED:
                self.skipped_delegated += 1
                continue
            if ownership is _TaskOwnership.UNKNOWN:
                self.skipped_unknown_ownership += 1
                continue
            task_id = row["id"]
            sqlite_activity = _utc_timestamp(
                row["updated_at_ms"], row["updated_at"]
            )
            refs.append(
                AgentTaskRef(
                    source_type=self.source_type,
                    native_task_id=task_id,
                    source_identity=f"codex://{task_id}",
                    source_locator=row["rollout_path"],
                    title=row["title"],
                    source_created_at=_utc_timestamp(
                        row["created_at_ms"], row["created_at"]
                    ),
                    last_activity_at=sqlite_activity,
                    archived=bool(row["archived"]),
                    current_workspace=row["cwd"] or None,
                )
            )
        return tuple(refs)

    def fetch_task(self, ref: AgentTaskRef) -> AgentTaskSnapshot:
        """Fetch one task as complete authored-prompt/final-outcome turns."""
        if ref.source_type != self.source_type:
            raise ValueError(
                f"cannot fetch {ref.source_type!r} with {self.source_type!r} source"
            )

        workspaces: list[str] = []
        pending_prompts: list[
            tuple[str | None, str, tuple[AttachmentDescriptor, ...]]
        ] = []
        turns: list[AgentTurn] = []
        rollout_activity: datetime | None = None
        git_repository, git_branch, git_commit = _sqlite_git_provenance(
            self._state_path,
            ref.native_task_id,
        )

        for record in _rollout_records(ref.source_locator):
            record_activity = _record_timestamp(record.get("timestamp"))
            if record_activity is not None and (
                rollout_activity is None or record_activity > rollout_activity
            ):
                rollout_activity = record_activity
            record_type = record.get("type")
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue

            if record_type == "session_meta":
                workspace = payload.get("cwd")
                if isinstance(workspace, str) and workspace and workspace not in workspaces:
                    workspaces.append(workspace)
                git = payload.get("git")
                if isinstance(git, dict):
                    repository = git.get("repository_url")
                    branch = git.get("branch")
                    commit = git.get("commit_hash")
                    if isinstance(repository, str) and repository:
                        git_repository = repository
                    if isinstance(branch, str) and branch:
                        git_branch = branch
                    if isinstance(commit, str) and commit:
                        git_commit = commit
                continue

            if record_type == "event_msg" and payload.get("type") == "user_message":
                raw_prompt = payload.get("message")
                prompt = (
                    raw_prompt
                    if isinstance(raw_prompt, str) and raw_prompt.strip()
                    else None
                )
                client_id = payload.get("client_id")
                prompt_timestamp = str(record.get("timestamp") or "")
                if isinstance(client_id, str) and client_id:
                    event_key = client_id
                elif prompt_timestamp:
                    event_key = _fallback_turn_key(
                        ref.native_task_id,
                        prompt_timestamp,
                    )
                else:
                    # Without either native identity signal, a later edit could
                    # masquerade as a new turn and violate monotonic capture.
                    continue
                attachments = _local_image_descriptors(payload, event_key)
                if prompt is not None or attachments:
                    pending_prompts.append(
                        (
                            prompt,
                            event_key,
                            attachments,
                        )
                    )
                continue

            if not (
                record_type == "response_item"
                and payload.get("type") == "message"
                and payload.get("role") == "assistant"
                and payload.get("phase") == "final_answer"
                and pending_prompts
            ):
                continue

            content = payload.get("content")
            if not isinstance(content, list):
                continue
            outcome_parts = [
                item["text"]
                for item in content
                if isinstance(item, dict)
                and item.get("type") == "output_text"
                and isinstance(item.get("text"), str)
            ]
            outcome = "\n".join(outcome_parts)
            if not outcome.strip():
                continue

            _, turn_key, _ = pending_prompts[0]
            turns.append(
                AgentTurn(
                    turn_id=turn_key,
                    prompt_parts=tuple(
                        item[0]
                        for item in pending_prompts
                        if item[0] is not None
                    ),
                    visible_outcome=outcome,
                    attachments=tuple(
                        attachment
                        for item in pending_prompts
                        for attachment in item[2]
                    ),
                )
            )
            pending_prompts.clear()

        observed_at = self._observed_at or datetime.now(tz=UTC)
        source_created_at = ref.source_created_at or ref.last_activity_at
        if ref.current_workspace and ref.current_workspace not in workspaces:
            workspaces.append(ref.current_workspace)
        project_id, project_name, project_root = _explicit_codex_project(
            self.codex_home,
            ref.native_task_id,
        )
        return AgentTaskSnapshot(
            source_type=self.source_type,
            native_task_id=ref.native_task_id,
            source_identity=ref.source_identity,
            title=ref.title,
            turns=tuple(turns),
            source_created_at=source_created_at,
            source_updated_at=max(
                timestamp
                for timestamp in (ref.last_activity_at, rollout_activity)
                if timestamp is not None
            ),
            observed_at=observed_at,
            archived=ref.archived,
            workspace_history=tuple(workspaces),
            codex_project_id=project_id,
            codex_project_name=project_name,
            codex_project_root=project_root,
            git_repository=git_repository,
            git_branch=git_branch,
            git_commit=git_commit,
            provenance=(
                ProvenanceField(
                    key="incomplete_turn_count",
                    value="1" if pending_prompts else "0",
                ),
            ),
        )
