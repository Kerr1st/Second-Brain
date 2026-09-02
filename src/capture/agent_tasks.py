"""Small value objects used by Codex Task capture.

This is intentionally not a source-neutral framework. Codex is the sole v1
integration; a shared adapter contract can be extracted when a second source is
implemented and provides evidence for the right abstraction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def _aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")


@dataclass(frozen=True, slots=True)
class AttachmentDescriptor:
    attachment_key: str
    media_kind: str
    filename: str | None = None
    content_type: str | None = None
    byte_size: int | None = None
    source_reference: str | None = None
    reference_is_durable: bool = False

    def __post_init__(self) -> None:
        if not self.attachment_key or not self.media_kind:
            raise ValueError("an attachment needs a key and media kind")
        if self.byte_size is not None and self.byte_size < 0:
            raise ValueError("attachment byte_size cannot be negative")


@dataclass(frozen=True, slots=True)
class AgentTurn:
    turn_id: str
    prompt_parts: tuple[str, ...]
    visible_outcome: str
    attachments: tuple[AttachmentDescriptor, ...] = ()

    def __post_init__(self) -> None:
        if not self.turn_id:
            raise ValueError("an Agent Turn needs a stable ID")
        if not self.prompt_parts and not self.attachments:
            raise ValueError("an Agent Turn needs a prompt or attachment")
        if not self.visible_outcome.strip():
            raise ValueError("an Agent Turn needs a visible final outcome")

    @property
    def turn_key(self) -> str:
        """Compatibility alias local to the retained source parser."""
        return self.turn_id


@dataclass(frozen=True, slots=True)
class AgentTaskRef:
    source_type: str
    native_task_id: str
    source_identity: str
    source_locator: str
    title: str
    source_created_at: datetime
    last_activity_at: datetime
    archived: bool = False
    current_workspace: str | None = None

    def __post_init__(self) -> None:
        _aware(self.source_created_at, "source_created_at")
        _aware(self.last_activity_at, "last_activity_at")
        if not self.native_task_id or not self.source_identity:
            raise ValueError("a Task reference needs native and source identities")


@dataclass(frozen=True, slots=True)
class ProvenanceField:
    key: str
    value: str


@dataclass(frozen=True, slots=True)
class AgentTaskSnapshot:
    source_type: str
    native_task_id: str
    source_identity: str
    title: str
    turns: tuple[AgentTurn, ...]
    source_created_at: datetime
    source_updated_at: datetime
    observed_at: datetime
    archived: bool = False
    workspace_history: tuple[str, ...] = ()
    codex_project_id: str | None = None
    codex_project_name: str | None = None
    codex_project_root: str | None = None
    git_repository: str | None = None
    git_branch: str | None = None
    git_commit: str | None = None
    provenance: tuple[ProvenanceField, ...] = ()

    def __post_init__(self) -> None:
        _aware(self.source_created_at, "source_created_at")
        _aware(self.source_updated_at, "source_updated_at")
        _aware(self.observed_at, "observed_at")
        turn_ids = tuple(turn.turn_id for turn in self.turns)
        if len(turn_ids) != len(set(turn_ids)):
            raise ValueError("Agent Turn IDs must be unique within a Task")

    @property
    def incomplete_turn_count(self) -> int:
        for item in self.provenance:
            if item.key == "incomplete_turn_count":
                try:
                    return max(0, int(item.value))
                except (TypeError, ValueError):
                    return 0
        return 0


@dataclass(frozen=True, slots=True)
class TopicSegment:
    title: str
    turn_ids: tuple[str, ...]
    content: str | None = None

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("a Topic Segment needs a title")
        if not self.turn_ids or len(self.turn_ids) != len(set(self.turn_ids)):
            raise ValueError("a Topic Segment needs unique ordered Agent Turn IDs")


@dataclass(frozen=True, slots=True)
class DerivedMemory:
    segment: int
    kind: str
    title: str
    content: str
    supporting_turn_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.segment < 0:
            raise ValueError("memory segment index cannot be negative")
        if self.kind not in {"decision", "insight", "correction_episode"}:
            raise ValueError(
                "memory kind must be decision, insight, or correction_episode"
            )
        if not self.title.strip() or not self.content.strip():
            raise ValueError("a derived memory needs a title and content")
        if (
            not self.supporting_turn_ids
            or len(self.supporting_turn_ids) != len(set(self.supporting_turn_ids))
        ):
            raise ValueError("a derived memory needs unique supporting turn IDs")
        if self.kind == "correction_episode":
            if len(self.supporting_turn_ids) < 2:
                raise ValueError(
                    "a correction episode must cite the prior outcome and correction"
                )
            paragraphs = self.content.split("\n\n")
            if (
                len(paragraphs) != 2
                or not paragraphs[0].startswith("Misalignment: ")
                or not paragraphs[0].removeprefix("Misalignment: ").strip()
                or not paragraphs[1].startswith(
                    "Corrected expectation: The user indicated "
                )
                or not paragraphs[1]
                .removeprefix("Corrected expectation: The user indicated ")
                .strip()
            ):
                raise ValueError(
                    "a correction episode must use the neutral attributed format"
                )


@dataclass(frozen=True, slots=True)
class TaskSemanticResult:
    segments: tuple[TopicSegment, ...]
    memories: tuple[DerivedMemory, ...] = ()

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError("a semantic pass must return at least one Topic Segment")
