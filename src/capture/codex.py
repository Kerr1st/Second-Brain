"""The single Codex Desktop Task capture path.

``run_codex_capture`` hides source discovery, six-hour eligibility, monotonic
Agent Turn capture, one combined semantic pass, and atomic semantic persistence.
The Captured Task commits before semantic processing so a transient model or
embedding failure never loses source evidence; the unchanged cursor causes the
whole unprocessed tail to retry on the next run.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable

import src.db as db
from src.backends.resolver import default_resolver
from src.capture.agent_tasks import (
    AgentTaskSnapshot,
    AgentTurn,
    AttachmentDescriptor,
    DerivedMemory,
    TaskSemanticResult,
    TopicSegment,
)
from src.capture.sources.codex import CodexDesktopSource
from src.embeddings import generate_embedding


IDLE_THRESHOLD = timedelta(hours=6)
TASK_SOURCE_TYPE = "codex_task"
MEMORY_SOURCE_TYPE = "distilled_agent_task"


_SEMANTIC_SYSTEM_PROMPT = """You perform one semantic pass over a Codex Task.

Return a JSON object with exactly two arrays: `segments` and `memories`.

Every new Agent Turn must appear exactly once, in order, in a Topic Segment.
Each segment has `title` and `turn_ids`. The first segment may continue the
provided previous segment only by repeating all of its turn IDs as an unchanged
prefix before new turn IDs. Do not summarize segments; the original turns are
stored as their content.

Create a memory only for a decision, insight, or correction episode that is
independently useful for later retrieval. It is valid and often correct to
return no memories. Each memory has `segment` (zero-based result index), `kind`
(`decision`, `insight`, or `correction_episode`), `title`, `content`, and
`supporting_turn_ids`. Every memory must cite
supporting turns from its segment and at least one newly provided turn.

A correction episode exists only when the user rejects, replaces, or materially
narrows a prior visible agent outcome and states a specific improved
expectation. Do not infer one from a new requirement, follow-up question,
changed circumstances alone, ambiguous dissatisfaction, sentiment, or quoted
third-party material. When either the prior misalignment or the corrected
expectation is unclear, abstain.

A conditional clarification is not a correction. For example, when the user
says to preserve X if it is already the standard but otherwise asks for an
explanation, abstain unless the user also unambiguously states that the prior
outcome was wrong and supplies its replacement.

For a correction episode:
- cite both the Agent Turn containing the prior visible agent outcome and the
  Agent Turn containing the user's correction;
- use `kind` `correction_episode`;
- write neutral, user-attributed content with exactly these two paragraphs:
  `Misalignment: ...` and `Corrected expectation: The user indicated ...`;
- preserve the substance and terminology of the correcting prompt. Do not add
  downstream implications, implementation consequences, generalized rules, or
  claims supplied only by the agent's acknowledgement. Prefer the narrowest
  faithful statement, even when a broader inference seems reasonable;
- do not formulate a rule or add categories, scope, applicability, promotion
  status, recurrence, keywords, or contradiction analysis.

Do not emit processing telemetry, hashes, or versions."""


@dataclass(frozen=True, slots=True)
class TaskFailure:
    task_id: str
    stage: str
    error_type: str


@dataclass(frozen=True, slots=True)
class CaptureReport:
    enumerated: int = 0
    skipped_delegated: int = 0
    skipped_unknown_ownership: int = 0
    eligible: int = 0
    skipped_recent: int = 0
    skipped_archived: int = 0
    incomplete: int = 0
    captured: int = 0
    refreshed: int = 0
    unchanged: int = 0
    semantic_processed: int = 0
    semantic_retried: int = 0
    failed: int = 0
    dry_run: bool = False
    failures: tuple[TaskFailure, ...] = ()


@dataclass(slots=True)
class _Services:
    source: CodexDesktopSource
    connect: Callable
    semantic: Any
    embed: Callable[[str], list[float]]
    lock_path: Path


@dataclass(frozen=True, slots=True)
class _CapturedTask:
    memory_id: Any
    title: str
    source_url: str
    metadata: dict[str, Any]
    turns: tuple[AgentTurn, ...]
    disposition: str


@dataclass(frozen=True, slots=True)
class _StoredSegment:
    memory_id: Any
    index: int
    segment: TopicSegment


class CodexCaptureAlreadyRunning(RuntimeError):
    """Another local capture process currently owns the singleton lock."""


class SemanticResultError(ValueError):
    """The combined semantic result does not preserve the evidence contract."""


class _BackendSemanticPass:
    """Lazily resolve the configured Thinker only when semantic work exists."""

    def run(self, *, previous_segment, turns):
        resolver = default_resolver()
        spec = resolver.spec_for("thinker")
        invoker = resolver.invoker_for("thinker")
        payload = {
            "previous_segment": (
                {
                    "title": previous_segment.title,
                    "turn_ids": list(previous_segment.turn_ids),
                    "content": previous_segment.content,
                }
                if previous_segment is not None
                else None
            ),
            "new_turns": [_turn_dict(turn) for turn in turns],
        }
        result = invoker.invoke(
            _SEMANTIC_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            tools=False,
            effort=spec.effort,
        )
        return _parse_semantic_output(result["output"])


def _default_services() -> _Services:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return _Services(
        source=CodexDesktopSource(codex_home),
        connect=db.get_connection,
        semantic=_BackendSemanticPass(),
        embed=generate_embedding,
        lock_path=Path(tempfile.gettempdir())
        / f"second-brain-codex-capture-{os.getuid()}.lock",
    )


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CodexCaptureAlreadyRunning(
                "another Codex capture run is already active"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def run_codex_capture(
    now: datetime,
    *,
    task_id: str | None = None,
    backfill: bool = False,
    dry_run: bool = False,
) -> CaptureReport:
    """Capture eligible Codex Tasks through the one v1 processing path."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    now = now.astimezone(UTC)
    services = _default_services()
    with _exclusive_lock(services.lock_path):
        return _run(
            now=now,
            task_id=task_id,
            backfill=backfill,
            dry_run=dry_run,
            services=services,
        )


def _run(*, now, task_id, backfill, dry_run, services) -> CaptureReport:
    counts = {
        "enumerated": 0,
        "skipped_delegated": 0,
        "skipped_unknown_ownership": 0,
        "eligible": 0,
        "skipped_recent": 0,
        "skipped_archived": 0,
        "incomplete": 0,
        "captured": 0,
        "refreshed": 0,
        "unchanged": 0,
        "semantic_processed": 0,
        "semantic_retried": 0,
        "failed": 0,
    }
    failures: list[TaskFailure] = []
    refs = tuple(services.source.enumerate_tasks(now))
    counts["enumerated"] = len(refs)
    counts["skipped_delegated"] = services.source.skipped_delegated
    counts["skipped_unknown_ownership"] = (
        services.source.skipped_unknown_ownership
    )

    for ref in refs:
        if task_id is not None and ref.native_task_id != task_id:
            continue
        include_archived = backfill or task_id is not None
        if ref.archived and not include_archived:
            counts["skipped_archived"] += 1
            continue
        if now - ref.last_activity_at < IDLE_THRESHOLD:
            counts["skipped_recent"] += 1
            continue

        try:
            snapshot = services.source.fetch_task(ref)
        except Exception as exc:
            _record_failure(counts, failures, ref.native_task_id, "source_read", exc)
            continue
        if now - snapshot.source_updated_at < IDLE_THRESHOLD:
            counts["skipped_recent"] += 1
            continue
        if not snapshot.turns:
            counts["incomplete"] += 1
            continue

        counts["eligible"] += 1
        if dry_run:
            continue

        try:
            captured = _capture_snapshot(snapshot, now, services.connect)
        except Exception as exc:
            _record_failure(counts, failures, ref.native_task_id, "capture", exc)
            continue
        counts[captured.disposition] += 1

        tail = _unprocessed_tail(captured.turns, captured.metadata.get("semantic_cursor"))
        if not tail:
            continue
        if captured.disposition == "unchanged":
            counts["semantic_retried"] += 1

        try:
            _process_semantics(captured, tail, services)
        except Exception as exc:
            _record_failure(counts, failures, ref.native_task_id, "semantic", exc)
            continue
        counts["semantic_processed"] += 1

    return CaptureReport(
        **counts,
        dry_run=dry_run,
        failures=tuple(failures),
    )


def _record_failure(counts, failures, task_id, stage, error) -> None:
    counts["failed"] += 1
    failures.append(
        TaskFailure(
            task_id=task_id,
            stage=stage,
            error_type=type(error).__name__,
        )
    )


def _attachment_dict(attachment: AttachmentDescriptor) -> dict[str, Any]:
    return asdict(attachment)


def _turn_dict(turn: AgentTurn) -> dict[str, Any]:
    return {
        "turn_id": turn.turn_id,
        "prompt_parts": list(turn.prompt_parts),
        "visible_outcome": turn.visible_outcome,
        "attachments": [_attachment_dict(item) for item in turn.attachments],
    }


def _turn_from_dict(value: dict[str, Any]) -> AgentTurn:
    return AgentTurn(
        turn_id=value["turn_id"],
        prompt_parts=tuple(value.get("prompt_parts", ())),
        visible_outcome=value["visible_outcome"],
        attachments=tuple(
            AttachmentDescriptor(**attachment)
            for attachment in value.get("attachments", ())
        ),
    )


def _render_turn(turn: AgentTurn, number: int) -> str:
    prompt = "\n\n".join(turn.prompt_parts)
    parts = [
        f"## Agent Turn {number}",
        f"Turn ID: {turn.turn_id}",
        "User prompt:",
        prompt or "[attachment-only prompt]",
    ]
    if turn.attachments:
        parts.append("Attachments:")
        for item in turn.attachments:
            label = item.filename or item.attachment_key
            details = ", ".join(
                value
                for value in (item.media_kind, item.content_type)
                if value
            )
            parts.append(f"- {label}" + (f" ({details})" if details else ""))
    parts.extend(("Visible final answer:", turn.visible_outcome))
    return "\n".join(parts)


def _render_task(title: str, turns: tuple[AgentTurn, ...]) -> str:
    return "\n\n".join(
        [f"# {title}"]
        + [_render_turn(turn, index) for index, turn in enumerate(turns, start=1)]
    )


def _metadata_for_snapshot(
    snapshot: AgentTaskSnapshot,
    turns: tuple[AgentTurn, ...],
    *,
    semantic_cursor: str | None,
    captured_at: datetime,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous = previous or {}
    workspaces = list(previous.get("workspace_history", ()))
    for workspace in snapshot.workspace_history:
        if workspace not in workspaces:
            workspaces.append(workspace)
    codex_project = {
        key: value
        for key, value in {
            "id": snapshot.codex_project_id,
            "name": snapshot.codex_project_name,
            "root": snapshot.codex_project_root,
        }.items()
        if value is not None
    }
    git = {
        key: value
        for key, value in {
            "repository": snapshot.git_repository,
            "branch": snapshot.git_branch,
            "commit": snapshot.git_commit,
        }.items()
        if value is not None
    }
    provenance = {item.key: item.value for item in snapshot.provenance}
    native_title = {
        key: value
        for key, value in {
            "value": provenance.get("native_title_value"),
            "source": provenance.get("native_title_source"),
            "source_updated_at": provenance.get(
                "native_title_source_updated_at"
            ),
            "sqlite_title": provenance.get("sqlite_title"),
        }.items()
        if value is not None
    }
    metadata = {
        "record_kind": "captured_task",
        "native_task_id": snapshot.native_task_id,
        "native_source_type": snapshot.source_type,
        "source_created_at": snapshot.source_created_at.isoformat(),
        "source_updated_at": snapshot.source_updated_at.isoformat(),
        "captured_at": captured_at.isoformat(),
        "archived": snapshot.archived,
        "workspace_history": workspaces,
        "codex_project": codex_project or None,
        "git": git or None,
        "incomplete_turn_count": snapshot.incomplete_turn_count,
        "turns": [_turn_dict(turn) for turn in turns],
        "semantic_cursor": semantic_cursor,
    }
    if native_title:
        metadata["native_title"] = native_title
    return metadata


def _capture_snapshot(snapshot, now, connect) -> _CapturedTask:
    with connect() as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, title, metadata
                    FROM memories
                    WHERE source_type = %s
                      AND source_url = %s
                      AND parent_id IS NULL
                      AND metadata @> '{"record_kind": "captured_task"}'::jsonb
                    FOR UPDATE
                    """,
                    (TASK_SOURCE_TYPE, snapshot.source_identity),
                )
                row = cursor.fetchone()
                if row is None:
                    turns = snapshot.turns
                    metadata = _metadata_for_snapshot(
                        snapshot,
                        turns,
                        semantic_cursor=None,
                        captured_at=now,
                    )
                    cursor.execute(
                        """
                        INSERT INTO memories (
                            type, title, content, embedding, tags, source_url,
                            source_type, metadata, status, confidence, parent_id,
                            mem_class, project, encoding_context
                        ) VALUES (
                            'source', %s, %s, NULL, ARRAY['codex', 'agent-task'],
                            %s, %s, %s, 'active', 1.0, NULL, 'source', NULL, %s
                        )
                        RETURNING id
                        """,
                        (
                            snapshot.title,
                            _render_task(snapshot.title, turns),
                            snapshot.source_identity,
                            TASK_SOURCE_TYPE,
                            json.dumps(metadata),
                            "User prompts and visible final answers from a Codex Task.",
                        ),
                    )
                    memory_id = cursor.fetchone()[0]
                    disposition = "captured"
                    title = snapshot.title
                else:
                    memory_id, title, previous = row
                    stored_turns = tuple(
                        _turn_from_dict(value) for value in previous.get("turns", ())
                    )
                    known = {turn.turn_id for turn in stored_turns}
                    unseen = tuple(
                        turn for turn in snapshot.turns if turn.turn_id not in known
                    )
                    if not unseen:
                        stored_title = title
                        title = snapshot.title or stored_title
                        metadata = _metadata_for_snapshot(
                            snapshot,
                            stored_turns,
                            semantic_cursor=previous.get("semantic_cursor"),
                            captured_at=now,
                            previous=previous,
                        )
                        metadata["captured_at"] = previous.get(
                            "captured_at", metadata["captured_at"]
                        )
                        if title == stored_title and metadata == previous:
                            connection.rollback()
                            return _CapturedTask(
                                memory_id=memory_id,
                                title=title,
                                source_url=snapshot.source_identity,
                                metadata=previous,
                                turns=stored_turns,
                                disposition="unchanged",
                            )
                        cursor.execute(
                            """
                            UPDATE memories
                            SET title = %s, content = %s, metadata = %s,
                                updated_at = now()
                            WHERE id = %s
                            """,
                            (
                                title,
                                _render_task(title, stored_turns),
                                json.dumps(metadata),
                                memory_id,
                            ),
                        )
                        connection.commit()
                        return _CapturedTask(
                            memory_id=memory_id,
                            title=title,
                            source_url=snapshot.source_identity,
                            metadata=metadata,
                            turns=stored_turns,
                            disposition="refreshed",
                        )
                    turns = stored_turns + unseen
                    title = snapshot.title or title
                    metadata = _metadata_for_snapshot(
                        snapshot,
                        turns,
                        semantic_cursor=previous.get("semantic_cursor"),
                        captured_at=now,
                        previous=previous,
                    )
                    cursor.execute(
                        """
                        UPDATE memories
                        SET title = %s, content = %s, metadata = %s,
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (
                            title,
                            _render_task(title, turns),
                            json.dumps(metadata),
                            memory_id,
                        ),
                    )
                    disposition = "refreshed"
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return _CapturedTask(
        memory_id=memory_id,
        title=title,
        source_url=snapshot.source_identity,
        metadata=metadata,
        turns=turns,
        disposition=disposition,
    )


def _unprocessed_tail(
    turns: tuple[AgentTurn, ...], cursor: str | None
) -> tuple[AgentTurn, ...]:
    if cursor is None:
        return turns
    for index, turn in enumerate(turns):
        if turn.turn_id == cursor:
            return turns[index + 1 :]
    raise RuntimeError("semantic cursor does not identify a stored Agent Turn")


def _last_segment(captured: _CapturedTask, connect) -> _StoredSegment | None:
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, title, content, metadata
            FROM memories
            WHERE parent_id = %s
              AND source_type = %s
              AND metadata @> '{"record_kind": "topic_segment"}'::jsonb
            ORDER BY ((metadata->>'segment_index')::integer) DESC
            LIMIT 1
            """,
            (captured.memory_id, TASK_SOURCE_TYPE),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    memory_id, title, content, metadata = row
    return _StoredSegment(
        memory_id=memory_id,
        index=int(metadata["segment_index"]),
        segment=TopicSegment(
            title=title,
            turn_ids=tuple(metadata["turn_ids"]),
            content=content,
        ),
    )


def _process_semantics(captured, tail, services) -> None:
    previous = _last_segment(captured, services.connect)
    result = services.semantic.run(
        previous_segment=previous.segment if previous is not None else None,
        turns=tail,
    )
    _validate_semantic_result(result, previous, tail)

    turn_lookup = {turn.turn_id: turn for turn in captured.turns}
    segment_contents = tuple(
        _render_segment(segment, turn_lookup) for segment in result.segments
    )
    segment_embeddings = tuple(services.embed(text) for text in segment_contents)
    memory_embeddings = tuple(
        services.embed(memory.content) for memory in result.memories
    )
    _store_semantic_result(
        captured=captured,
        tail=tail,
        previous=previous,
        result=result,
        segment_contents=segment_contents,
        segment_embeddings=segment_embeddings,
        memory_embeddings=memory_embeddings,
        connect=services.connect,
    )


def _validate_semantic_result(result, previous, tail) -> None:
    if not isinstance(result, TaskSemanticResult):
        raise SemanticResultError("semantic pass returned the wrong result type")
    new_ids = tuple(turn.turn_id for turn in tail)
    new_set = set(new_ids)
    prior_ids = previous.segment.turn_ids if previous is not None else ()
    extended = bool(
        prior_ids
        and result.segments[0].turn_ids[: len(prior_ids)] == prior_ids
    )

    observed_new: list[str] = []
    for index, segment in enumerate(result.segments):
        ids = segment.turn_ids
        if index == 0 and extended:
            if len(ids) == len(prior_ids):
                raise SemanticResultError("an extended segment must include a new turn")
            ids = ids[len(prior_ids) :]
        elif set(ids) & set(prior_ids):
            raise SemanticResultError("only the first segment may extend the last segment")
        unknown = set(ids) - new_set
        if unknown:
            raise SemanticResultError("a segment referenced an unknown Agent Turn")
        observed_new.extend(ids)
    if tuple(observed_new) != new_ids:
        raise SemanticResultError(
            "Topic Segments must cover every new Agent Turn exactly once and in order"
        )

    for memory in result.memories:
        if memory.segment >= len(result.segments):
            raise SemanticResultError("a memory referenced an unknown Topic Segment")
        segment_ids = set(result.segments[memory.segment].turn_ids)
        support = set(memory.supporting_turn_ids)
        if not support <= segment_ids:
            raise SemanticResultError("memory support must belong to its Topic Segment")
        if not support & new_set:
            raise SemanticResultError(
                "a newly derived memory must cite at least one newly processed turn"
            )


def _render_segment(
    segment: TopicSegment, turn_lookup: dict[str, AgentTurn]
) -> str:
    return "\n\n".join(
        [f"# {segment.title}"]
        + [
            _render_turn(turn_lookup[turn_id], number)
            for number, turn_id in enumerate(segment.turn_ids, start=1)
        ]
    )


def _store_semantic_result(
    *,
    captured,
    tail,
    previous,
    result,
    segment_contents,
    segment_embeddings,
    memory_embeddings,
    connect,
) -> None:
    prior_ids = previous.segment.turn_ids if previous is not None else ()
    extends_previous = bool(
        prior_ids
        and result.segments[0].turn_ids[: len(prior_ids)] == prior_ids
    )
    expected_cursor = captured.metadata.get("semantic_cursor")
    segment_ids: list[Any] = []

    with connect() as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT metadata FROM memories WHERE id = %s FOR UPDATE",
                    (captured.memory_id,),
                )
                current_metadata = cursor.fetchone()[0]
                if current_metadata.get("semantic_cursor") != expected_cursor:
                    raise RuntimeError("semantic cursor changed during processing")

                next_index = previous.index + 1 if previous is not None else 0
                for result_index, segment in enumerate(result.segments):
                    content = segment_contents[result_index]
                    embedding = segment_embeddings[result_index]
                    if result_index == 0 and extends_previous:
                        segment_index = previous.index
                        segment_id = previous.memory_id
                        metadata = {
                            "record_kind": "topic_segment",
                            "segment_index": segment_index,
                            "turn_ids": list(segment.turn_ids),
                            "task_source_url": captured.source_url,
                        }
                        cursor.execute(
                            """
                            UPDATE memories
                            SET title = %s, content = %s, embedding = %s,
                                metadata = %s, updated_at = now()
                            WHERE id = %s AND parent_id = %s
                            """,
                            (
                                segment.title,
                                content,
                                str(embedding),
                                json.dumps(metadata),
                                segment_id,
                                captured.memory_id,
                            ),
                        )
                    else:
                        segment_index = next_index
                        next_index += 1
                        metadata = {
                            "record_kind": "topic_segment",
                            "segment_index": segment_index,
                            "turn_ids": list(segment.turn_ids),
                            "task_source_url": captured.source_url,
                        }
                        cursor.execute(
                            """
                            INSERT INTO memories (
                                type, title, content, embedding, tags, source_url,
                                source_type, metadata, status, confidence, parent_id,
                                mem_class, project, encoding_context
                            ) VALUES (
                                'source', %s, %s, %s,
                                ARRAY['codex', 'topic-segment'], %s, %s, %s,
                                'active', 1.0, %s, 'source', NULL, %s
                            )
                            RETURNING id
                            """,
                            (
                                segment.title,
                                content,
                                str(embedding),
                                f"{captured.source_url}#segment-{segment_index}",
                                TASK_SOURCE_TYPE,
                                json.dumps(metadata),
                                captured.memory_id,
                                "Original Agent Turns grouped as one Topic Segment.",
                            ),
                        )
                        segment_id = cursor.fetchone()[0]
                    segment_ids.append(segment_id)

                for memory, embedding in zip(
                    result.memories, memory_embeddings, strict=True
                ):
                    mem_class = (
                        "episodic"
                        if memory.kind == "correction_episode"
                        else "semantic"
                    )
                    encoding_context = (
                        "User-attributed correction evidence derived from a "
                        "Codex Topic Segment."
                        if memory.kind == "correction_episode"
                        else "Decision or insight derived from a Codex Topic Segment."
                    )
                    metadata = {
                        "record_kind": "task_memory",
                        "kind": memory.kind,
                        "supporting_turn_ids": list(memory.supporting_turn_ids),
                        "task_source_url": captured.source_url,
                    }
                    cursor.execute(
                        """
                        INSERT INTO memories (
                            type, title, content, embedding, tags, source_url,
                            source_type, metadata, status, confidence, parent_id,
                            mem_class, project, encoding_context
                        ) VALUES (
                            %s, %s, %s, %s, ARRAY['codex', 'task-memory'],
                            %s, %s, %s, 'active', 1.0, NULL, %s, NULL, %s
                        )
                        RETURNING id
                        """,
                        (
                            memory.kind,
                            memory.title,
                            memory.content,
                            str(embedding),
                            captured.source_url,
                            MEMORY_SOURCE_TYPE,
                            json.dumps(metadata),
                            mem_class,
                            encoding_context,
                        ),
                    )
                    memory_id = cursor.fetchone()[0]
                    cursor.execute(
                        """
                        INSERT INTO memory_relationships (
                            source_id, target_id, relation_type, note
                        ) VALUES (%s, %s, 'derived_from', %s)
                        """,
                        (
                            memory_id,
                            segment_ids[memory.segment],
                            "Supporting Agent Turn IDs: "
                            + ", ".join(memory.supporting_turn_ids),
                        ),
                    )

                current_metadata["semantic_cursor"] = tail[-1].turn_id
                cursor.execute(
                    """
                    UPDATE memories
                    SET metadata = %s, updated_at = now()
                    WHERE id = %s
                    """,
                    (json.dumps(current_metadata), captured.memory_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _parse_semantic_output(output: Any) -> TaskSemanticResult:
    if not isinstance(output, dict):
        raise SemanticResultError("semantic output must be a JSON object")
    segments_value = output.get("segments")
    memories_value = output.get("memories")
    if not isinstance(segments_value, list) or not isinstance(memories_value, list):
        raise SemanticResultError("semantic output needs segments and memories arrays")
    try:
        segments = tuple(
            TopicSegment(
                title=value["title"],
                turn_ids=tuple(value["turn_ids"]),
            )
            for value in segments_value
        )
        memories = tuple(
            DerivedMemory(
                segment=value["segment"],
                kind=value["kind"],
                title=value["title"],
                content=value["content"],
                supporting_turn_ids=tuple(value["supporting_turn_ids"]),
            )
            for value in memories_value
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SemanticResultError("semantic output has an invalid shape") from exc
    return TaskSemanticResult(segments=segments, memories=memories)
