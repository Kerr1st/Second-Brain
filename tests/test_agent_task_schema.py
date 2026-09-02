"""Minimal database invariants for Codex Task capture."""

from __future__ import annotations

import json

import psycopg2
import pytest

import src.db as db


def _insert_task(cursor, source_url="codex://real-task"):
    cursor.execute(
        """
        INSERT INTO memories (
            type, title, content, source_url, source_type, metadata, mem_class
        ) VALUES ('source', 'Task', 'turns', %s, 'codex_task', %s, 'source')
        RETURNING id
        """,
        (source_url, json.dumps({"record_kind": "captured_task"})),
    )
    return cursor.fetchone()[0]


def test_capture_migration_adds_no_revision_or_telemetry_columns(
    test_db, clean_tables
):
    with db.get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'memories'
            """
        )
        columns = {row[0] for row in cursor.fetchall()}

    assert {
        "source_created_at",
        "source_updated_at",
        "captured_at",
        "source_revision",
        "content_hash",
    }.isdisjoint(columns)


def test_native_codex_task_identity_is_unique(test_db, clean_tables):
    with db.get_connection() as connection, connection.cursor() as cursor:
        _insert_task(cursor)
        connection.commit()
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _insert_task(cursor)
        connection.rollback()


def test_topic_segment_index_is_unique_within_a_task(test_db, clean_tables):
    with db.get_connection() as connection, connection.cursor() as cursor:
        task_id = _insert_task(cursor)
        statement = """
            INSERT INTO memories (
                type, title, content, source_type, metadata, parent_id, mem_class
            ) VALUES ('source', %s, 'turns', 'codex_task', %s, %s, 'source')
        """
        metadata = json.dumps(
            {"record_kind": "topic_segment", "segment_index": 0}
        )
        cursor.execute(statement, ("First", metadata, task_id))
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cursor.execute(statement, ("Duplicate", metadata, task_id))
        connection.rollback()


def test_derived_from_provenance_cannot_expire(test_db, clean_tables):
    with db.get_connection() as connection, connection.cursor() as cursor:
        task_id = _insert_task(cursor)
        cursor.execute(
            """
            INSERT INTO memories (type, title, content, mem_class)
            VALUES ('decision', 'Memory', 'content', 'semantic')
            RETURNING id
            """
        )
        memory_id = cursor.fetchone()[0]
        with pytest.raises(psycopg2.errors.CheckViolation):
            cursor.execute(
                """
                INSERT INTO memory_relationships (
                    source_id, target_id, relation_type, expired_at
                ) VALUES (%s, %s, 'derived_from', now())
                """,
                (memory_id, task_id),
            )
        connection.rollback()
