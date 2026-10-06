"""Pure Codex rollout turn assembly, independent of native files and databases."""
from __future__ import annotations
import hashlib
from collections import Counter
from datetime import UTC, datetime
from src.capture.agent_tasks import AgentTurn, AttachmentDescriptor

def _fallback_turn_key(task_id: str, timestamp: str) -> str:
    """Derive a content-independent ID from a native event timestamp."""
    evidence = f"{task_id}\n{timestamp}".encode("utf-8")
    return "event-" + hashlib.sha256(evidence).hexdigest()


def _authored_text_message(payload: dict) -> tuple[str, str, str] | None:
    """Read the verified Desktop text shape, not arbitrary user-role context.

    The native metadata distinguishes authored text from injected context. Keep
    unknown/multimodal shapes out until their authorship and descriptors have a
    reference integration; never copy attachment bytes into prompt text.
    """
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if not isinstance(metadata, dict) or metadata.get("content_item_kinds") != ["user.text"]:
        return None
    turn_id = metadata.get("turn_id")
    message_id = payload.get("id")
    if not all(isinstance(value, str) and value for value in (turn_id, message_id)):
        return None
    content = payload.get("content")
    if not isinstance(content, list) or not content or not all(
        isinstance(item, dict)
        and item.get("type") == "input_text"
        and isinstance(item.get("text"), str)
        for item in content
    ):
        return None
    prompt = "\n".join(item["text"] for item in content)
    return (prompt, message_id, turn_id) if prompt.strip() else None


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



class RolloutTurnAssembler:
    """Named event handlers preserve source identity and explain skipped input.

    Legacy prompt events remain opt-in at construction for historical capture
    identity parity. Native-source code supplies attachment descriptors; the
    assembler never reads images, metadata stores, or filesystem state.
    """
    def __init__(self, task_id, *, legacy=False):
        self.task_id = task_id
        self.legacy = legacy
        self.workspaces = []
        self.git = {}
        self.pending_prompts = []
        self.pending_messages = []
        self.message_turn_id = None
        self.active_turn_id = None
        self.unsupported_message_turns = set()
        self.turns = []
        self.last_ts = None
        self.drops = Counter()

    def feed(self, record, *, attachments=()):
        if not isinstance(record, dict):
            self.drops["non_object_record"] += 1
            return
        timestamp = _record_timestamp(record.get("timestamp"))
        if timestamp is not None and (self.last_ts is None or timestamp > self.last_ts):
            self.last_ts = timestamp
        payload = record.get("payload")
        if not isinstance(payload, dict):
            self.drops["non_object_payload"] += 1
            return
        kind = record.get("type")
        if kind == "session_meta":
            self._session(payload)
        elif kind == "event_msg" and payload.get("type") in {"task_started", "task_complete", "turn_aborted"}:
            self._turn_event(payload)
        elif kind == "event_msg" and payload.get("type") == "user_message":
            self._legacy_user(payload, record.get("timestamp"), attachments)
        elif kind == "response_item" and payload.get("type") == "message":
            if payload.get("role") == "user":
                self._user(payload)
            elif payload.get("role") == "assistant" and payload.get("phase") == "final_answer":
                self._final(payload)
            else:
                self.drops["non_conversation_message"] += 1
        else:
            self.drops["non_conversation_record"] += 1

    def _session(self, payload):
        workspace = payload.get("cwd")
        if isinstance(workspace, str) and workspace and workspace not in self.workspaces:
            self.workspaces.append(workspace)
        git = payload.get("git")
        if isinstance(git, dict):
            for key in ("repository_url", "branch", "commit_hash"):
                if isinstance(git.get(key), str) and git[key]:
                    self.git[key] = git[key]

    def _turn_event(self, payload):
        native_id = payload.get("turn_id")
        if payload["type"] == "turn_aborted":
            self.drops["aborted_legacy_prompts"] += len(self.pending_prompts)
            self.pending_prompts.clear()
        if payload["type"] == "task_started":
            if native_id != self.message_turn_id:
                self._drop_pending_messages("superseded_by_new_turn")
            self.active_turn_id = native_id
        elif native_id == self.message_turn_id or payload["type"] == "turn_aborted":
            # F-085: a turn that completes (or aborts) without a final_answer
            # loses its authored prompt. That is still dropped (there is no
            # outcome to pair it with) but no longer silently: the count is
            # part of the task's ``capture_drop_counts`` provenance.
            self._drop_pending_messages("task_complete_without_final" if payload["type"] == "task_complete"
                                        else "aborted_messages")

    def _drop_pending_messages(self, reason):
        if self.pending_messages:
            self.drops[reason] += len(self.pending_messages)
        self.pending_messages.clear()
        self.message_turn_id = None

    def _user(self, payload):
        authored = _authored_text_message(payload)
        if authored is None:
            metadata = payload.get("internal_chat_message_metadata_passthrough")
            if isinstance(metadata, dict):
                native_id = metadata.get("turn_id")
                kinds = metadata.get("content_item_kinds")
                if isinstance(kinds, list) and any(isinstance(kind, str) and kind.startswith("user.") for kind in kinds):
                    if isinstance(native_id, str) and native_id:
                        self.unsupported_message_turns.add(native_id)
            self.drops["unsupported_or_unauthored_user"] += 1
            return
        prompt, message_id, native_id = authored
        if self.active_turn_id is not None and native_id != self.active_turn_id:
            self.drops["user_turn_mismatch"] += 1
            return
        if native_id != self.message_turn_id:
            self.pending_messages.clear()
            self.message_turn_id = native_id
        if not any(item[1] == message_id for item in self.pending_messages):
            self.pending_messages.append((prompt, message_id, ()))
        else:
            self.drops["duplicate_user_message"] += 1

    def _legacy_user(self, payload, timestamp, attachments):
        if not self.legacy:
            self.drops["legacy_disabled"] += 1
            return
        raw = payload.get("message")
        prompt = raw if isinstance(raw, str) and raw.strip() else None
        client_id = payload.get("client_id")
        if isinstance(client_id, str) and client_id:
            event_key = client_id
        elif timestamp:
            event_key = _fallback_turn_key(self.task_id, str(timestamp))
        else:
            self.drops["legacy_missing_identity"] += 1
            return
        if prompt is not None or attachments:
            self.pending_prompts.append((prompt, event_key, attachments))
        else:
            self.drops["legacy_empty_prompt"] += 1

    def _final(self, payload):
        prompts = self.pending_prompts
        if not prompts:
            metadata = payload.get("internal_chat_message_metadata_passthrough")
            if not isinstance(metadata, dict) or metadata.get("turn_id") != self.message_turn_id:
                self.drops["final_turn_mismatch"] += 1
                return
            if self.message_turn_id in self.unsupported_message_turns:
                self.drops["final_depends_on_unsupported_input"] += 1
                return
            prompts = self.pending_messages
        outcome = final_answer_text(payload)
        if not prompts or outcome is None:
            self.drops["incomplete_turn"] += 1
            return
        self.turns.append(AgentTurn(turn_id=prompts[0][1],
            prompt_parts=tuple(item[0] for item in prompts if item[0] is not None),
            visible_outcome=outcome,
            attachments=tuple(attachment for item in prompts for attachment in item[2])))
        self.pending_prompts.clear()
        self.pending_messages.clear()
        self.message_turn_id = None

    @property
    def incomplete(self):
        return bool(self.pending_prompts or self.pending_messages)


def final_answer_text(payload):
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    text = "\n".join(item["text"] for item in content if isinstance(item, dict)
                     and item.get("type") == "output_text" and isinstance(item.get("text"), str))
    return text if text.strip() else None
