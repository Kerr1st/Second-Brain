"""Read Codex Desktop tasks from its local SQLite index and rollout JSONL.

This adapter is deliberately read-only. It normalizes only user-authored prompt
events and visible final assistant outcomes; developer context, reasoning,
tools, and commentary remain outside the capture boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import sqlite3
from dataclasses import dataclass
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
logger = logging.getLogger(__name__)

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


class CodexSourceMissing(FileNotFoundError):
    """The indexed rollout exists in no known Codex layout (truly gone).

    Raised only after every known location was checked: the indexed path, its
    zstd-compressed sibling, and the same file name under ``sessions/`` or
    ``archived_sessions/``. Any other ``FileNotFoundError`` stays a plain read
    failure.
    """


#: Codex Desktop compresses older rollouts in place (``x.jsonl`` ->
#: ``x.jsonl.zst``) without rewriting ``threads.rollout_path`` (seen 2026-10-14).
_ZSTD_SUFFIX = ".zst"
_ROLLOUT_ROOTS = ("sessions", "archived_sessions")


def _resolve_rollout(source_locator, codex_home: Path | None = None) -> Path:
    """Return the on-disk rollout for an indexed locator, or raise CodexSourceMissing."""
    path = Path(source_locator)
    if path.is_file():
        return path
    compressed = path.with_name(path.name + _ZSTD_SUFFIX)
    if compressed.is_file():
        return compressed
    if codex_home is not None and path.name:
        # Moved between Codex layouts (e.g. sessions/ <-> archived_sessions/).
        for name in (path.name, path.name + _ZSTD_SUFFIX):
            for root in _ROLLOUT_ROOTS:
                base = codex_home / root
                if not base.is_dir():
                    continue
                matches = sorted(match for match in base.rglob(name) if match.is_file())
                if matches:
                    return matches[0]
    raise CodexSourceMissing(f"Codex rollout not found: {path.name}")


def _open_rollout_text(path: Path):
    if path.name.endswith(_ZSTD_SUFFIX):
        try:
            from compression import zstd
        except ImportError as exc:  # Python < 3.14: a broken reader, not a gone source
            raise CodexSourceCompatibilityError(
                "Codex rollout is zstd-compressed but compression.zstd is unavailable"
            ) from exc
        return zstd.open(path, "rt", encoding="utf-8")
    return path.open(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class _TitleObservation:
    title: str
    updated_at: datetime
    line_number: int


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
    # Non-interactive exec sets thread_source=user even for internal model
    # calls. It does not establish human task ownership. Fail closed.
    if source == "exec":
        return _TaskOwnership.UNKNOWN
    if thread_source == "user":
        return _TaskOwnership.USER_OWNED
    return _TaskOwnership.UNKNOWN


from src.capture.sources.codex_turns import (
    RolloutTurnAssembler, _fallback_turn_key, _authored_text_message, _record_timestamp,
)


def _load_sidebar_titles(path: Path) -> dict[str, _TitleObservation]:
    """Replay valid native sidebar-title observations from Codex's index."""
    try:
        with path.open(encoding="utf-8") as index_file:
            lines = index_file.readlines()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning("cannot read Codex session index %s: %s", path, exc)
        return {}

    observations: dict[str, _TitleObservation] = {}
    for line_number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            is_partial_tail = (
                line_number == len(lines) and not line.endswith(("\n", "\r"))
            )
            if not is_partial_tail:
                logger.warning(
                    "invalid Codex session-index record at line %s", line_number
                )
            continue
        if not isinstance(record, dict):
            continue
        task_id = record.get("id")
        title = record.get("thread_name")
        updated_at = _record_timestamp(record.get("updated_at"))
        if not (
            isinstance(task_id, str)
            and task_id
            and isinstance(title, str)
            and title.strip()
            and updated_at is not None
        ):
            continue
        observation = _TitleObservation(
            title=title.strip(),
            updated_at=updated_at,
            line_number=line_number,
        )
        previous = observations.get(task_id)
        if previous is None or (
            observation.updated_at,
            observation.line_number,
        ) > (previous.updated_at, previous.line_number):
            observations[task_id] = observation
    return observations


def _fingerprint(path):
    stat = Path(path).stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _read_rollout(source_locator, codex_home: Path | None = None):
    """Return a bounded streaming reader plus its verified source fingerprint.

    The caller must consume the iterator before constructing a snapshot. Any
    concurrent rewrite aborts the capture, so no cursor advances over stale data.
    The locator is resolved first (see ``_resolve_rollout``); a source found in
    no known location raises ``CodexSourceMissing``.
    """
    path = _resolve_rollout(source_locator, codex_home)
    fingerprint = _fingerprint(path)
    def records():
        try:
            handle = _open_rollout_text(path)
        except FileNotFoundError as exc:
            # Removed (e.g. compressed by Codex) between resolve and open.
            raise CodexSourceChangedDuringRead(
                "Codex rollout moved during read; retry the task"
            ) from exc
        with handle:
            number = 0
            while True:
                line = handle.readline(8 * 1024 * 1024 + 1)
                if not line:
                    break
                number += 1
                if len(line) > 8 * 1024 * 1024:
                    raise CodexSourceParseError("Codex rollout record exceeds bounded reader limit")
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    if not line.endswith(("\n", "\r")):
                        break  # an in-progress final append
                    raise CodexSourceParseError(f"invalid Codex rollout JSONL: malformed interior record at line {number}") from exc
                yield record
        try:
            final = _fingerprint(path)
        except FileNotFoundError:
            final = None
        if final != fingerprint:
            raise CodexSourceChangedDuringRead("Codex rollout changed during read; retry the task")
    return records(), fingerprint


def _rollout_records(source_locator):
    """Compatibility iterator; new captures also persist the returned fingerprint."""
    records, _ = _read_rollout(source_locator)
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


def _project_state(codex_home):
    try:
        with (codex_home / ".codex-global-state.json").open(encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _explicit_codex_project(
    codex_home: Path,
    task_id: str,
    state: dict | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve only an explicit, internally consistent Codex Project assignment."""
    if state is None:
        state = _project_state(codex_home)
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
        self._project_state = None
        self.skipped_delegated = 0
        self.skipped_unknown_ownership = 0

    def enumerate_tasks(self, now: datetime) -> Iterable[AgentTaskRef]:
        """Enumerate user-owned task references from Codex's local index."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include a timezone")
        self.skipped_delegated = 0
        self.skipped_unknown_ownership = 0
        self._project_state = _project_state(self.codex_home)
        sidebar_titles = _load_sidebar_titles(
            self.codex_home / "session_index.jsonl"
        )

        uri = f"file:{self._state_path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            _require_columns(conn, "threads", _REQUIRED_THREAD_COLUMNS)
            _require_columns(
                conn,
                "thread_spawn_edges",
                _REQUIRED_SPAWN_EDGE_COLUMNS,
            )
            thread_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(threads)")
            }
            name_selection = "t.name" if "name" in thread_columns else "NULL"
            git_selection = ", ".join(f"t.{key}" if key in thread_columns else f"NULL AS {key}"
                                      for key in ("git_origin_url", "git_branch", "git_sha"))
            rows = conn.execute(
                f"""
                SELECT
                    t.id,
                    t.rollout_path,
                    t.created_at,
                    t.updated_at,
                    t.source,
                    t.cwd,
                    t.title,
                    {name_selection} AS name,
                    {git_selection},
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
            sqlite_title = row["title"]
            sqlite_name = row["name"]
            title_observation = sidebar_titles.get(task_id)
            if isinstance(sqlite_name, str) and sqlite_name.strip():
                effective_title = sqlite_name.strip()
                title_source = "sqlite_name"
                title_updated_at = None
            elif title_observation is not None:
                effective_title = title_observation.title
                title_source = "session_index"
                title_updated_at = title_observation.updated_at
            else:
                effective_title = sqlite_title
                title_source = "sqlite_title"
                title_updated_at = None
            title_provenance = [
                ProvenanceField("native_title_source", title_source),
                ProvenanceField("native_title_value", effective_title),
                ProvenanceField("sqlite_title", sqlite_title),
            ]
            if title_updated_at is not None:
                title_provenance.append(
                    ProvenanceField(
                        "native_title_source_updated_at",
                        title_updated_at.isoformat(),
                    )
                )
            sqlite_activity = _utc_timestamp(
                row["updated_at_ms"], row["updated_at"]
            )
            refs.append(
                AgentTaskRef(
                    source_type=self.source_type,
                    native_task_id=task_id,
                    source_identity=f"codex://{task_id}",
                    source_locator=row["rollout_path"],
                    title=effective_title,
                    source_created_at=_utc_timestamp(
                        row["created_at_ms"], row["created_at"]
                    ),
                    last_activity_at=sqlite_activity,
                    archived=bool(row["archived"]),
                    current_workspace=row["cwd"] or None,
                    observed_at=now.astimezone(UTC),
                    provenance=tuple(title_provenance),
                    native_git=tuple(row[key] if isinstance(row[key], str) and row[key] else None
                                     for key in ("git_origin_url", "git_branch", "git_sha")),
                )
            )
        return tuple(refs)

    def fetch_task(self, ref: AgentTaskRef) -> AgentTaskSnapshot:
        """Fetch one task as complete authored-prompt/final-outcome turns."""
        if ref.source_type != self.source_type:
            raise ValueError(
                f"cannot fetch {ref.source_type!r} with {self.source_type!r} source"
            )

        assembler = RolloutTurnAssembler(ref.native_task_id, legacy=True)
        git_repository, git_branch, git_commit = (ref.native_git if ref.native_git is not None
            else _sqlite_git_provenance(self._state_path, ref.native_task_id))
        records, fingerprint = _read_rollout(ref.source_locator, self.codex_home)
        for record in records:
            attachments = ()
            payload = record.get("payload") if isinstance(record, dict) else None
            if isinstance(payload, dict) and record.get("type") == "event_msg" and payload.get("type") == "user_message":
                event_key = payload.get("client_id") or _fallback_turn_key(ref.native_task_id, str(record.get("timestamp") or ""))
                attachments = _local_image_descriptors(payload, event_key)
            assembler.feed(record, attachments=attachments)
        workspaces = assembler.workspaces
        git_repository = assembler.git.get("repository_url", git_repository)
        git_branch = assembler.git.get("branch", git_branch)
        git_commit = assembler.git.get("commit_hash", git_commit)
        observed_at = ref.observed_at or datetime.now(tz=UTC)
        source_created_at = ref.source_created_at or ref.last_activity_at
        if ref.current_workspace and ref.current_workspace not in workspaces:
            workspaces.append(ref.current_workspace)
        if self._project_state is None:
            self._project_state = _project_state(self.codex_home)
        project_id, project_name, project_root = _explicit_codex_project(
            self.codex_home, ref.native_task_id, state=self._project_state,
        )
        return AgentTaskSnapshot(
            source_type=self.source_type,
            native_task_id=ref.native_task_id,
            source_identity=ref.source_identity,
            title=ref.title,
            turns=tuple(assembler.turns),
            source_created_at=source_created_at,
            source_updated_at=max(
                timestamp
                for timestamp in (ref.last_activity_at, assembler.last_ts)
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
                    value="1" if assembler.incomplete else "0",
                ),
                ProvenanceField("capture_drop_counts", json.dumps(assembler.drops, sort_keys=True)),
                ProvenanceField("rollout_fingerprint", json.dumps(fingerprint)),
                *ref.provenance,
            ),
        )
