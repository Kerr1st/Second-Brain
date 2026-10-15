"""Codex rollouts that moved or vanished from the indexed ``rollout_path``.

Codex Desktop compresses older rollouts in place (``x.jsonl`` ->
``x.jsonl.zst``) without updating ``threads.rollout_path``; on 2026-10-15 that
made 37 ``source_read`` FileNotFoundErrors fail the codex-capture job. Content
that still exists must be captured; truly-gone sources are skipped
(``skipped_missing_source``) without failing the job, unless no source could be
read at all.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.capture import codex as codex_capture
from src.capture.sources.codex import (
    CodexDesktopSource,
    CodexSourceMissing,
    _resolve_rollout,
)
from tests.test_codex_capture import (
    MANIFEST,
    NOW,
    TASK_ID,
    _install_services,
    _one_segment,
    _real_codex_home,
    _SemanticScript,
    _services,
    _task_row,
    _write_rollout,
    _write_state_database,
)

zstd = pytest.importorskip("compression.zstd")


def _compress(path: Path) -> Path:
    """Mimic Codex: replace ``x.jsonl`` with ``x.jsonl.zst``, index untouched."""
    target = path.with_name(path.name + ".zst")
    target.write_bytes(zstd.compress(path.read_bytes()))
    path.unlink()
    return target


def _add_task(home: Path, task_id: str, rollout: Path) -> None:
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute(
            """
            INSERT INTO threads (
                id, rollout_path, created_at, updated_at, source, model_provider,
                cwd, title, archived, agent_path, created_at_ms, updated_at_ms,
                thread_source, git_origin_url, git_branch, git_sha
            ) VALUES (?, ?, ?, ?, 'vscode', 'openai', ?, ?, 0, NULL, ?, ?, 'user',
                      NULL, NULL, NULL)
            """,
            (
                task_id, str(rollout), MANIFEST["created_at"], MANIFEST["updated_at"],
                MANIFEST["cwd"], f"Task {task_id}", MANIFEST["created_at_ms"],
                MANIFEST["updated_at_ms"],
            ),
        )


def _dry_services(home, tmp_path, source=None):
    return SimpleNamespace(
        source=source or CodexDesktopSource(home),
        connect=lambda: pytest.fail("dry run must not touch the database"),
        semantic=_SemanticScript(),
        embed=lambda text: pytest.fail("dry run must not embed"),
        lock_path=tmp_path / "capture.lock",
    )


def _snapshot(home):
    source = CodexDesktopSource(home)
    (ref,) = source.enumerate_tasks(NOW)
    return source.fetch_task(ref)


# --- source resolution -----------------------------------------------------


def test_compressed_rollout_reads_identically_to_the_plain_one(tmp_path):
    home = _real_codex_home(tmp_path)
    plain = _snapshot(home)
    rollout = home / "sessions" / f"rollout-{TASK_ID}.jsonl"
    _compress(rollout)

    compressed = _snapshot(home)

    assert len(plain.turns) == 3
    assert compressed.turns == plain.turns
    assert compressed.source_updated_at == plain.source_updated_at


def test_rollout_moved_to_archived_sessions_is_found(tmp_path):
    home = _real_codex_home(tmp_path)
    rollout = home / "sessions" / f"rollout-{TASK_ID}.jsonl"
    moved = home / "archived_sessions" / "2026" / rollout.name
    moved.parent.mkdir(parents=True)
    rollout.rename(moved)

    assert _resolve_rollout(rollout, home) == moved
    assert len(_snapshot(home).turns) == 3


def test_plain_rollout_is_preferred_over_a_compressed_sibling(tmp_path):
    rollout = tmp_path / "rollout-x.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    rollout.with_name(rollout.name + ".zst").write_bytes(zstd.compress(b"{}\n"))

    assert _resolve_rollout(rollout, tmp_path) == rollout


def test_truly_gone_rollout_raises_codex_source_missing(tmp_path):
    home = _real_codex_home(tmp_path)
    (home / "sessions" / f"rollout-{TASK_ID}.jsonl").unlink()
    source = CodexDesktopSource(home)
    (ref,) = source.enumerate_tasks(NOW)

    with pytest.raises(CodexSourceMissing) as caught:
        source.fetch_task(ref)
    assert isinstance(caught.value, FileNotFoundError)


# --- job classification (dry run: no DB, model, or embedding) -------------


def test_missing_source_is_skipped_not_failed_and_warned_once(
    tmp_path, monkeypatch, caplog
):
    home = _real_codex_home(tmp_path)
    _compress(home / "sessions" / f"rollout-{TASK_ID}.jsonl")
    for task_id in ("gone-1", "gone-2"):
        _add_task(home, task_id, home / "sessions" / f"rollout-{task_id}.jsonl")
    _install_services(monkeypatch, _dry_services(home, tmp_path))

    with caplog.at_level(logging.WARNING, logger=codex_capture.logger.name):
        report = codex_capture.run_codex_capture(NOW, backfill=True, dry_run=True)

    assert report.enumerated == 3
    assert report.eligible == 1  # the compressed task is still read
    assert report.skipped_missing_source == 2
    assert report.failed == 0
    assert report.infrastructure_failed == 0
    assert report.failures == ()
    warnings = [r for r in caplog.records if "skipped_missing_source" in r.getMessage()]
    assert len(warnings) == 1
    assert "2 Task(s)" in warnings[0].getMessage()


def test_all_sources_missing_still_fails_the_job(tmp_path, monkeypatch):
    home = _real_codex_home(tmp_path)
    (home / "sessions" / f"rollout-{TASK_ID}.jsonl").unlink()
    _add_task(home, "gone-1", home / "sessions" / "rollout-gone-1.jsonl")
    _install_services(monkeypatch, _dry_services(home, tmp_path))

    report = codex_capture.run_codex_capture(NOW, backfill=True, dry_run=True)

    assert report.skipped_missing_source == 0
    assert report.failed == 2
    assert report.infrastructure_failed == 2
    assert {f.stage for f in report.failures} == {"source_read"}
    assert {f.error_type for f in report.failures} == {"CodexSourceMissing"}


def test_corrupt_compressed_rollout_is_a_failure_not_a_skip(tmp_path, monkeypatch):
    home = _real_codex_home(tmp_path)
    _add_task(home, "corrupt", home / "sessions" / "rollout-corrupt.jsonl")
    (home / "sessions" / "rollout-corrupt.jsonl.zst").write_bytes(b"not zstd at all")
    _install_services(monkeypatch, _dry_services(home, tmp_path))

    report = codex_capture.run_codex_capture(NOW, backfill=True, dry_run=True)

    assert report.eligible == 1
    assert report.skipped_missing_source == 0
    assert report.infrastructure_failed == 1
    (failure,) = report.failures
    assert (failure.task_id, failure.stage) == ("corrupt", "source_read")
    assert failure.error_type != "CodexSourceMissing"


def test_plain_file_not_found_from_the_reader_still_fails(tmp_path, monkeypatch):
    home = _real_codex_home(tmp_path)
    _add_task(home, "other", home / "sessions" / f"rollout-{TASK_ID}.jsonl")

    class _Source(CodexDesktopSource):
        def fetch_task(self, ref):
            if ref.native_task_id == "other":
                raise FileNotFoundError("unrelated missing file")
            return super().fetch_task(ref)

    _install_services(monkeypatch, _dry_services(home, tmp_path, _Source(home)))

    report = codex_capture.run_codex_capture(NOW, backfill=True, dry_run=True)

    assert report.skipped_missing_source == 0
    assert report.infrastructure_failed == 1
    assert report.failures[0].error_type == "FileNotFoundError"


# --- real capture path -----------------------------------------------------


def test_compression_after_capture_does_not_fail_or_redo_semantics(
    test_db, clean_tables, tmp_path, monkeypatch
):
    home = _real_codex_home(tmp_path, turn_indexes=(0, 1))
    semantic = _SemanticScript(lambda previous, turns: _one_segment(previous, turns))
    _install_services(monkeypatch, _services(home, semantic, tmp_path / "capture.lock"))
    first = codex_capture.run_codex_capture(NOW, backfill=True)

    _compress(home / "sessions" / f"rollout-{TASK_ID}.jsonl")
    after = codex_capture.run_codex_capture(NOW, backfill=True)

    assert (first.captured, first.semantic_processed) == (1, 1)
    assert after.failed == 0
    assert after.refreshed + after.unchanged == 1
    assert after.semantic_processed == 0
    assert len(semantic.calls) == 1


def test_never_captured_compressed_task_is_captured(
    test_db, clean_tables, tmp_path, monkeypatch
):
    home = tmp_path / ".codex"
    home.mkdir()
    rollout = _write_rollout(home, turn_indexes=(0, 1, 2))
    _write_state_database(home, MANIFEST, rollout)
    _compress(rollout)
    semantic = _SemanticScript(lambda previous, turns: _one_segment(previous, turns))
    _install_services(monkeypatch, _services(home, semantic, tmp_path / "capture.lock"))

    report = codex_capture.run_codex_capture(NOW, backfill=True)

    assert (report.captured, report.failed) == (1, 0)
    _, _, metadata, _, _ = _task_row()
    assert len(metadata["turns"]) == 3
