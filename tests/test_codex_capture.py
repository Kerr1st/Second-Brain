"""Public-interface proof for the Codex Desktop capture path.

The source records are a capture-relevant excerpt from a real Codex Task. Tests
exercise the same public function and PostgreSQL schema used by the command.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import src.db as db
from src.capture import codex as codex_capture
from src.capture.agent_tasks import (
    DerivedMemory,
    TaskSemanticResult,
    TopicSegment,
)
from src.capture.sources.codex import CodexDesktopSource
from src.dream_cycle.storage import check_duplicate


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "codex"
MANIFEST = json.loads((FIXTURE_DIR / "real_task.json").read_text())
RECORDS = tuple(
    json.loads(line)
    for line in (FIXTURE_DIR / "real_task.jsonl").read_text().splitlines()
)
CORRECTION_MANIFEST = json.loads(
    (FIXTURE_DIR / "real_correction_task.json").read_text()
)
CORRECTION_RECORDS = tuple(
    json.loads(line)
    for line in (FIXTURE_DIR / "real_correction_task.jsonl").read_text().splitlines()
)
LEGACY_DELEGATED_MANIFEST = json.loads(
    (FIXTURE_DIR / "real_legacy_delegated_task.json").read_text()
)
TASK_ID = MANIFEST["id"]
CORRECTION_TASK_ID = CORRECTION_MANIFEST["id"]
NOW = datetime(2026, 7, 18, 18, 0, tzinfo=UTC)
VECTOR = [1.0, *([0.0] * 1023)]


def _turn_pairs(records=RECORDS):
    pairs = []
    prompt = None
    for record in records:
        payload = record.get("payload", {})
        if record["type"] == "event_msg" and payload.get("type") == "user_message":
            prompt = record
        elif (
            record["type"] == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "assistant"
            and payload.get("phase") == "final_answer"
        ):
            assert prompt is not None
            pairs.append((prompt, record))
            prompt = None
    return tuple(pairs)


def _write_excerpt(codex_home: Path, manifest, records) -> Path:
    rollout = codex_home / "sessions" / f"rollout-{manifest['id']}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return rollout


def _write_rollout(codex_home: Path, turn_indexes=(0, 1, 2)) -> Path:
    rollout = codex_home / "sessions" / f"rollout-{TASK_ID}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    records = [RECORDS[0]]
    if 0 in turn_indexes:
        # These real commentary and tool records prove that the capture boundary
        # ignores progress and tool activity around the first turn.
        records.extend(RECORDS[1:6])
        records.append(_turn_pairs()[0][1])
    for index in turn_indexes:
        if index == 0:
            continue
        records.extend(_turn_pairs()[index])
    rollout.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return rollout


def _write_state_database(codex_home: Path, manifest, rollout: Path) -> None:
    with sqlite3.connect(codex_home / "state_5.sqlite") as connection:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                model_provider TEXT NOT NULL,
                cwd TEXT NOT NULL,
                title TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                agent_path TEXT,
                created_at_ms INTEGER,
                updated_at_ms INTEGER,
                thread_source TEXT,
                git_origin_url TEXT,
                git_branch TEXT,
                git_sha TEXT
            );
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL,
                child_thread_id TEXT NOT NULL PRIMARY KEY,
                status TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO threads (
                id, rollout_path, created_at, updated_at, source, model_provider,
                cwd, title, archived, agent_path, created_at_ms, updated_at_ms,
                thread_source, git_origin_url, git_branch, git_sha
            ) VALUES (?, ?, ?, ?, ?, 'openai', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                manifest["id"],
                str(rollout),
                manifest["created_at"],
                manifest["updated_at"],
                manifest["source"],
                manifest["cwd"],
                manifest["title"],
                manifest["archived"],
                manifest["agent_path"],
                manifest["created_at_ms"],
                manifest["updated_at_ms"],
                manifest["thread_source"],
                manifest["git_origin_url"],
                manifest["git_branch"],
                manifest["git_sha"],
            ),
        )


def _real_codex_home(tmp_path: Path, turn_indexes=(0, 1, 2)) -> Path:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    rollout = _write_rollout(codex_home, turn_indexes)
    _write_state_database(codex_home, MANIFEST, rollout)
    return codex_home


def _insert_ownership_task(
    codex_home: Path,
    *,
    task_id: str,
    source: str,
    thread_source: str | None,
    agent_path: str | None = None,
    spawned_child: bool = False,
) -> None:
    with sqlite3.connect(codex_home / "state_5.sqlite") as connection:
        rollout_path = connection.execute(
            "SELECT rollout_path FROM threads LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO threads (
                id, rollout_path, created_at, updated_at, source, model_provider,
                cwd, title, archived, agent_path, created_at_ms, updated_at_ms,
                thread_source, git_origin_url, git_branch, git_sha
            ) VALUES (?, ?, ?, ?, ?, 'openai', ?, ?, 0, ?, ?, ?, ?, NULL, NULL, NULL)
            """,
            (
                task_id,
                rollout_path,
                MANIFEST["created_at"],
                MANIFEST["updated_at"],
                source,
                MANIFEST["cwd"],
                f"Ownership fixture {task_id}",
                agent_path,
                MANIFEST["created_at_ms"],
                MANIFEST["updated_at_ms"],
                thread_source,
            ),
        )
        if spawned_child:
            connection.execute(
                """
                INSERT INTO thread_spawn_edges (
                    parent_thread_id, child_thread_id, status
                ) VALUES (?, ?, 'completed')
                """,
                (TASK_ID, task_id),
            )


def _write_correction_rollout(
    codex_home: Path, turn_indexes=(0, 1)
) -> Path:
    records = [CORRECTION_RECORDS[0]]
    pairs = _turn_pairs(CORRECTION_RECORDS)
    for index in turn_indexes:
        records.extend(pairs[index])
    return _write_excerpt(codex_home, CORRECTION_MANIFEST, records)


def _real_correction_codex_home(
    tmp_path: Path, turn_indexes=(0, 1)
) -> Path:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    rollout = _write_correction_rollout(codex_home, turn_indexes)
    _write_state_database(codex_home, CORRECTION_MANIFEST, rollout)
    return codex_home


class _SemanticScript:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def run(self, *, previous_segment, turns):
        self.calls.append((previous_segment, turns))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(previous_segment, turns)
        return result


def _one_segment(previous_segment, turns, *, memory=True):
    prior = previous_segment.turn_ids if previous_segment is not None else ()
    turn_ids = prior + tuple(turn.turn_id for turn in turns)
    memories = ()
    if memory:
        memories = (
            DerivedMemory(
                segment=0,
                kind="decision",
                title="Preserve workspace as provenance",
                content=(
                    "Keep the Codex workspace as source provenance rather than "
                    "automatically assigning it as the memory's semantic project."
                ),
                supporting_turn_ids=(turns[-1].turn_id,),
            ),
        )
    return TaskSemanticResult(
        segments=(
            TopicSegment(
                title="Codex task capture and project attribution",
                turn_ids=turn_ids,
            ),
        ),
        memories=memories,
    )


def _services(codex_home, semantic, lock_path, *, embed=lambda text: VECTOR):
    return SimpleNamespace(
        source=CodexDesktopSource(codex_home),
        connect=db.get_connection,
        semantic=semantic,
        embed=embed,
        lock_path=lock_path,
    )


def _install_services(monkeypatch, services):
    monkeypatch.setattr(codex_capture, "_default_services", lambda: services)


def _task_row():
    with db.get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, content, metadata, project, embedding
            FROM memories
            WHERE source_type = 'codex_task'
              AND parent_id IS NULL
              AND source_url = %s
            """,
            (f"codex://{TASK_ID}",),
        )
        return cursor.fetchone()


def test_archived_real_task_requires_backfill_and_dry_run_writes_nothing(
    test_db, clean_tables, tmp_path, monkeypatch
):
    home = _real_codex_home(tmp_path)
    semantic = _SemanticScript()
    _install_services(
        monkeypatch,
        _services(home, semantic, tmp_path / "capture.lock"),
    )

    normal = codex_capture.run_codex_capture(NOW)
    preview = codex_capture.run_codex_capture(NOW, backfill=True, dry_run=True)

    assert normal.enumerated == 1
    assert normal.skipped_archived == 1
    assert preview.eligible == 1
    assert preview.dry_run is True
    assert _task_row() is None
    assert semantic.calls == []


def test_task_is_eligible_at_exactly_six_hours_of_inactivity(
    test_db, clean_tables, tmp_path, monkeypatch
):
    home = _real_codex_home(tmp_path)
    semantic = _SemanticScript()
    _install_services(
        monkeypatch,
        _services(home, semantic, tmp_path / "capture.lock"),
    )
    exact_boundary_ms = int(NOW.timestamp() * 1000) - (6 * 60 * 60 * 1000)
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute(
            "UPDATE threads SET updated_at = ?, updated_at_ms = ? WHERE id = ?",
            (exact_boundary_ms // 1000, exact_boundary_ms, TASK_ID),
        )

    exact = codex_capture.run_codex_capture(NOW, backfill=True, dry_run=True)

    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute(
            "UPDATE threads SET updated_at_ms = ? WHERE id = ?",
            (exact_boundary_ms + 1, TASK_ID),
        )
    too_recent = codex_capture.run_codex_capture(NOW, backfill=True, dry_run=True)

    assert (exact.eligible, exact.skipped_recent) == (1, 0)
    assert (too_recent.eligible, too_recent.skipped_recent) == (0, 1)
    assert semantic.calls == []


def test_native_ownership_evidence_excludes_delegated_and_unknown_tasks(
    test_db, clean_tables, tmp_path, monkeypatch
):
    home = _real_codex_home(tmp_path)
    _insert_ownership_task(
        home,
        task_id=LEGACY_DELEGATED_MANIFEST["id"],
        source=LEGACY_DELEGATED_MANIFEST["source"],
        thread_source=LEGACY_DELEGATED_MANIFEST["thread_source"],
        agent_path=LEGACY_DELEGATED_MANIFEST["agent_path"],
    )
    _insert_ownership_task(
        home,
        task_id="unknown-ownership-task",
        source="vscode",
        thread_source=None,
    )
    _insert_ownership_task(
        home,
        task_id="agent-path-delegated-task",
        source="vscode",
        thread_source="user",
        agent_path="/root/research",
    )
    _insert_ownership_task(
        home,
        task_id="structured-source-delegated-task",
        source=json.dumps({"subagent": {"thread_spawn": {"depth": 1}}}),
        thread_source=None,
    )
    _insert_ownership_task(
        home,
        task_id="spawn-edge-delegated-task",
        source="vscode",
        thread_source="user",
        spawned_child=True,
    )
    semantic = _SemanticScript()
    _install_services(
        monkeypatch,
        _services(home, semantic, tmp_path / "capture.lock"),
    )

    report = codex_capture.run_codex_capture(NOW, backfill=True, dry_run=True)

    assert report.enumerated == 1
    assert report.skipped_delegated == 4
    assert report.skipped_unknown_ownership == 1
    assert report.eligible == 1
    assert semantic.calls == []


def test_real_task_captures_monotonically_refreshes_and_ignores_source_drift(
    test_db, clean_tables, tmp_path, monkeypatch
):
    home = _real_codex_home(tmp_path, turn_indexes=(0, 1))
    semantic = _SemanticScript(
        lambda previous, turns: _one_segment(previous, turns),
        lambda previous, turns: _one_segment(previous, turns),
    )
    _install_services(
        monkeypatch,
        _services(home, semantic, tmp_path / "capture.lock"),
    )

    first = codex_capture.run_codex_capture(NOW, backfill=True)
    unchanged = codex_capture.run_codex_capture(NOW, backfill=True)
    first_id, _, first_metadata, project, embedding = _task_row()

    _write_rollout(home, turn_indexes=(0, 1, 2))
    resumed = codex_capture.run_codex_capture(NOW, backfill=True)
    resumed_id, content, resumed_metadata, _, _ = _task_row()

    # The real source now omits and reorders known evidence. Stored turns remain
    # immutable and no historical repair path is invoked.
    _write_rollout(home, turn_indexes=(2, 0))
    drifted = codex_capture.run_codex_capture(NOW, backfill=True)
    _, _, drifted_metadata, _, _ = _task_row()

    assert (first.captured, first.semantic_processed) == (1, 1)
    assert (unchanged.unchanged, unchanged.semantic_processed) == (1, 0)
    assert (resumed.refreshed, resumed.semantic_processed) == (1, 1)
    assert (drifted.unchanged, drifted.semantic_processed) == (1, 0)
    assert resumed_id == first_id
    assert project is None
    assert embedding is None
    assert len(first_metadata["turns"]) == 2
    assert len(resumed_metadata["turns"]) == 3
    assert drifted_metadata["turns"] == resumed_metadata["turns"]
    assert "I’ll check this project’s Codex task list" not in content
    assert "custom_tool_call" not in content
    attachment = resumed_metadata["turns"][2]["attachments"][0]
    assert attachment["filename"].endswith(".png")
    assert attachment["media_kind"] == "image"
    assert attachment["source_reference"].startswith("/var/folders/")
    assert semantic.calls[1][0].turn_ids == tuple(
        turn["turn_id"] for turn in first_metadata["turns"]
    )


def test_source_provenance_refreshes_without_new_turns(
    test_db, clean_tables, tmp_path, monkeypatch
):
    home = _real_codex_home(tmp_path)
    semantic = _SemanticScript(
        lambda previous, turns: _one_segment(previous, turns),
    )
    _install_services(
        monkeypatch,
        _services(home, semantic, tmp_path / "capture.lock"),
    )

    first = codex_capture.run_codex_capture(NOW, backfill=True)
    _, _, initial_metadata, _, _ = _task_row()
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute(
            """
            UPDATE threads
            SET title = ?, cwd = ?, git_branch = ?,
                updated_at = ?, updated_at_ms = ?
            WHERE id = ?
            """,
            (
                "Renamed Codex task",
                "/Users/kerrclaw/repositories/Github Repos/Second-Brain-renamed",
                "codex/codex-task-capture",
                MANIFEST["updated_at"] + 60,
                MANIFEST["updated_at_ms"] + 60_000,
                TASK_ID,
            ),
        )

    refreshed = codex_capture.run_codex_capture(NOW, backfill=True)
    _, content, metadata, _, _ = _task_row()

    assert (first.captured, first.semantic_processed) == (1, 1)
    assert (refreshed.refreshed, refreshed.semantic_processed) == (1, 0)
    assert len(semantic.calls) == 1
    assert metadata["turns"] == initial_metadata["turns"]
    assert metadata["semantic_cursor"] == initial_metadata["semantic_cursor"]
    assert metadata["captured_at"] == initial_metadata["captured_at"]
    assert metadata["source_updated_at"] != initial_metadata["source_updated_at"]
    assert metadata["workspace_history"][-1].endswith("Second-Brain-renamed")
    assert metadata["git"]["branch"] == "codex/codex-task-capture"
    assert content.startswith("# Renamed Codex task")


def test_semantic_failure_is_atomic_and_retries_the_same_unprocessed_tail(
    test_db, clean_tables, tmp_path, monkeypatch
):
    home = _real_codex_home(tmp_path)
    semantic = _SemanticScript(
        lambda previous, turns: _one_segment(previous, turns),
        lambda previous, turns: _one_segment(previous, turns),
    )
    calls = 0

    def failing_embedding(text):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("embedding unavailable")
        return VECTOR

    services = _services(
        home,
        semantic,
        tmp_path / "capture.lock",
        embed=failing_embedding,
    )
    _install_services(monkeypatch, services)

    failed = codex_capture.run_codex_capture(NOW, backfill=True)
    task_id, _, metadata, _, _ = _task_row()
    with db.get_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM memories WHERE parent_id = %s", (task_id,))
        segment_count = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM memory_relationships")
        relationship_count = cursor.fetchone()[0]

    services.embed = lambda text: VECTOR
    retried = codex_capture.run_codex_capture(NOW, backfill=True)

    assert failed.captured == 1
    assert failed.failed == 1
    assert metadata["semantic_cursor"] is None
    assert segment_count == 0
    assert relationship_count == 0
    assert retried.unchanged == 1
    assert retried.semantic_processed == 1
    assert retried.failed == 0
    assert tuple(turn.turn_id for turn in semantic.calls[0][1]) == tuple(
        turn.turn_id for turn in semantic.calls[1][1]
    )


def test_combined_pass_persists_searchable_segments_memories_and_exact_provenance(
    test_db, clean_tables, tmp_path, monkeypatch
):
    home = _real_codex_home(tmp_path)
    semantic = _SemanticScript(
        lambda previous, turns: _one_segment(previous, turns),
    )
    _install_services(
        monkeypatch,
        _services(home, semantic, tmp_path / "capture.lock"),
    )

    report = codex_capture.run_codex_capture(NOW, task_id=TASK_ID)
    task_id, _, task_metadata, _, _ = _task_row()
    with db.get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, metadata, embedding IS NOT NULL
            FROM memories
            WHERE parent_id = %s
            """,
            (task_id,),
        )
        segment_id, segment_metadata, segment_embedded = cursor.fetchone()
        cursor.execute(
            """
            SELECT m.id, m.type, m.metadata, m.embedding IS NOT NULL,
                   r.target_id, r.expired_at
            FROM memories AS m
            JOIN memory_relationships AS r ON r.source_id = m.id
            WHERE m.source_type = 'distilled_agent_task'
              AND r.relation_type = 'derived_from'
            """
        )
        memory = cursor.fetchone()

    retrieved = db.search_similar(VECTOR, limit=10, status="active")
    retrieved_ids = {str(item["id"]) for item in retrieved}

    assert report.semantic_processed == 1
    assert task_metadata["semantic_cursor"] == task_metadata["turns"][-1]["turn_id"]
    assert segment_metadata["turn_ids"] == [
        turn["turn_id"] for turn in task_metadata["turns"]
    ]
    assert segment_embedded is True
    assert memory[1] == "decision"
    assert memory[2]["supporting_turn_ids"] == [task_metadata["turns"][-1]["turn_id"]]
    assert memory[3] is True
    assert memory[4] == segment_id
    assert memory[5] is None
    assert str(segment_id) in retrieved_ids
    assert str(memory[0]) in retrieved_ids


def test_real_user_correction_persists_neutral_episode_with_exact_provenance(
    test_db, clean_tables, tmp_path, monkeypatch
):
    home = _real_correction_codex_home(tmp_path)

    def correction_result(previous, turns):
        assert previous is None
        assert len(turns) == 2
        return TaskSemanticResult(
            segments=(
                TopicSegment(
                    title="Separate agent integration identities",
                    turn_ids=tuple(turn.turn_id for turn in turns),
                ),
            ),
            memories=(
                DerivedMemory(
                    segment=0,
                    kind="correction_episode",
                    title="Distinguish Amazon agent integrations",
                    content=(
                        "Misalignment: The agent documented “Amazon Quick” as "
                        "“Kiro CLI/Amazon Q,” conflating Amazon Quick, Kiro, and "
                        "Amazon Q Developer.\n\nCorrected expectation: The user "
                        "indicated that Amazon Quick, Kiro, and Amazon Q Developer "
                        "are distinct and should not be conflated."
                    ),
                    supporting_turn_ids=tuple(turn.turn_id for turn in turns),
                ),
            ),
        )

    semantic = _SemanticScript(correction_result)
    _install_services(
        monkeypatch,
        _services(home, semantic, tmp_path / "capture.lock"),
    )

    report = codex_capture.run_codex_capture(NOW, task_id=CORRECTION_TASK_ID)
    unchanged = codex_capture.run_codex_capture(NOW, task_id=CORRECTION_TASK_ID)
    with db.get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT m.id, m.type, m.content, m.mem_class, m.metadata,
                   r.target_id, r.expired_at
            FROM memories AS m
            JOIN memory_relationships AS r ON r.source_id = m.id
            WHERE m.source_type = 'distilled_agent_task'
              AND m.type = 'correction_episode'
              AND r.relation_type = 'derived_from'
            """
        )
        episode = cursor.fetchone()

    retrieved = db.search_similar(VECTOR, limit=10, status="active")
    with patch("src.mcp_server.generate_embedding", return_value=VECTOR):
        from src.mcp_server import memory_search

        dream_cycle_search = memory_search(
            query="Amazon Quick separate from Kiro and Amazon Q Developer",
            limit=10,
            status="active",
        )

    assert report.semantic_processed == 1
    assert (unchanged.unchanged, unchanged.semantic_processed) == (1, 0)
    assert len(semantic.calls) == 1
    assert episode[1] == "correction_episode"
    assert episode[2] == (
        "Misalignment: The agent documented “Amazon Quick” as “Kiro CLI/Amazon "
        "Q,” conflating Amazon Quick, Kiro, and Amazon Q Developer.\n\n"
        "Corrected expectation: The user indicated that Amazon Quick, Kiro, and "
        "Amazon Q Developer are distinct and should not be conflated."
    )
    assert episode[3] == "episodic"
    assert episode[4]["supporting_turn_ids"] == [
        "ee442a21-8919-4145-b3df-b454fd987cd7",
        "b0a21672-118f-433e-a1e7-3b206baed324",
    ]
    assert episode[6] is None
    assert str(episode[0]) in {str(item["id"]) for item in retrieved}
    assert str(episode[0]) in {
        item["id"] for item in dream_cycle_search["results"]
    }


def test_resumed_task_extends_adjacent_segment_to_capture_correction(
    test_db, clean_tables, tmp_path, monkeypatch
):
    home = _real_correction_codex_home(tmp_path, turn_indexes=(0,))

    def initial_result(previous, turns):
        assert previous is None
        assert len(turns) == 1
        return TaskSemanticResult(
            segments=(
                TopicSegment(
                    title="Agent integration capture standard",
                    turn_ids=(turns[0].turn_id,),
                ),
            ),
            memories=(),
        )

    def resumed_result(previous, turns):
        assert previous is not None
        assert len(turns) == 1
        turn_ids = previous.turn_ids + (turns[0].turn_id,)
        return TaskSemanticResult(
            segments=(
                TopicSegment(
                    title="Separate agent integration identities",
                    turn_ids=turn_ids,
                ),
            ),
            memories=(
                DerivedMemory(
                    segment=0,
                    kind="correction_episode",
                    title="Distinguish Amazon agent integrations",
                    content=(
                        "Misalignment: The agent documented “Amazon Quick” as "
                        "“Kiro CLI/Amazon Q,” conflating Amazon Quick, Kiro, and "
                        "Amazon Q Developer.\n\nCorrected expectation: The user "
                        "indicated that Amazon Quick, Kiro, and Amazon Q Developer "
                        "are distinct and should not be conflated."
                    ),
                    supporting_turn_ids=turn_ids,
                ),
            ),
        )

    semantic = _SemanticScript(initial_result, resumed_result)
    _install_services(
        monkeypatch,
        _services(home, semantic, tmp_path / "capture.lock"),
    )

    first = codex_capture.run_codex_capture(NOW, task_id=CORRECTION_TASK_ID)
    with db.get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id
            FROM memories
            WHERE source_type = 'codex_task'
              AND metadata->>'record_kind' = 'topic_segment'
            """
        )
        original_segment_id = cursor.fetchone()[0]

    _write_correction_rollout(home)
    resumed = codex_capture.run_codex_capture(NOW, task_id=CORRECTION_TASK_ID)

    with db.get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, metadata->'turn_ids'
            FROM memories
            WHERE source_type = 'codex_task'
              AND metadata->>'record_kind' = 'topic_segment'
            """
        )
        segment_id, segment_turn_ids = cursor.fetchone()
        cursor.execute(
            """
            SELECT count(*), min(r.target_id::text)
            FROM memories AS m
            JOIN memory_relationships AS r ON r.source_id = m.id
            WHERE m.type = 'correction_episode'
              AND r.relation_type = 'derived_from'
              AND r.expired_at IS NULL
            """
        )
        episode_count, target_id = cursor.fetchone()

    assert (first.captured, first.semantic_processed) == (1, 1)
    assert (resumed.refreshed, resumed.semantic_processed) == (1, 1)
    assert segment_id == original_segment_id
    assert segment_turn_ids == [
        "ee442a21-8919-4145-b3df-b454fd987cd7",
        "b0a21672-118f-433e-a1e7-3b206baed324",
    ]
    assert episode_count == 1
    assert target_id == str(original_segment_id)


def test_zero_memory_segment_is_searchable_but_dream_cycle_ignores_child_source(
    test_db, clean_tables, tmp_path, monkeypatch
):
    home = _real_codex_home(tmp_path)
    semantic = _SemanticScript(
        lambda previous, turns: _one_segment(previous, turns, memory=False),
    )
    _install_services(
        monkeypatch,
        _services(home, semantic, tmp_path / "capture.lock"),
    )

    assert codex_capture.run_codex_capture(NOW, task_id=TASK_ID).failed == 0
    with db.get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM memories WHERE parent_id IS NOT NULL AND source_type = 'codex_task'"
        )
        segment_id = str(cursor.fetchone()[0])
        cursor.execute(
            "SELECT count(*) FROM memories WHERE source_type = 'distilled_agent_task'"
        )
        assert cursor.fetchone()[0] == 0

    retrieved = db.search_similar(VECTOR, limit=10, status="active")
    assert segment_id in {str(item["id"]) for item in retrieved}
    with patch("src.dream_cycle.storage.generate_embedding", return_value=VECTOR):
        assert check_duplicate("Codex task capture project attribution") is None
