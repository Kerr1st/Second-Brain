"""R4 gating/visibility fixes: Codex task_complete-without-final is a counted drop
(fix 8, F-085), never-indexed Crawlee sources are bounded per run (fix 10, D N-6)
and a bad header line fails one item rather than the channel (D N-9).
"""
from __future__ import annotations

import json
import os
import time
from unittest.mock import Mock

import pytest

from src.capture.common import exit_code


# --------------------------------------------------------------------------- #
# Fix 8 — F-085 visibility: task_complete without a final answer is counted      #
# --------------------------------------------------------------------------- #
def test_task_complete_without_final_answer_is_a_counted_drop():
    from src.capture.sources.codex_turns import RolloutTurnAssembler
    a = RolloutTurnAssembler("task-1")
    a.feed({"timestamp": "2026-09-10T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}})
    a.feed({"type": "response_item", "payload": {"type": "message", "role": "user", "id": "m1",
                                                 "internal_chat_message_metadata_passthrough": {"turn_id": "t1", "content_item_kinds": ["user.text"]},
                                                 "content": [{"type": "input_text", "text": "Do the thing"}]}})
    a.feed({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1"}})
    assert a.turns == [] and not a.incomplete
    assert a.drops["task_complete_without_final"] == 1
    # And an image-bearing prompt remains a counted, not silent, drop.
    a.feed({"type": "response_item", "payload": {"type": "message", "role": "user", "id": "m2",
                                                 "internal_chat_message_metadata_passthrough": {"turn_id": "t2", "content_item_kinds": ["user.text", "user.image"]},
                                                 "content": [{"type": "input_text", "text": "see image"}]}})
    assert a.drops["unsupported_or_unauthored_user"] == 1


# --------------------------------------------------------------------------- #
# Fix 10 — never-indexed Crawlee sources are gated per run (N-6)                 #
# --------------------------------------------------------------------------- #
def _crawlee_store(tmp_path, monkeypatch, unindexed=3):
    from src.parsers import crawlee
    knowledge = tmp_path / "knowledge"; sources = knowledge / "sources"; output = tmp_path / "output"
    sources.mkdir(parents=True); output.mkdir()
    for name, mod in (("CRAWLEE_DIR", tmp_path), ("KNOWLEDGE_DIR", knowledge), ("INDEX_PATH", knowledge / "index.md"), ("OUTPUT_DIR", output)):
        monkeypatch.setattr(crawlee, name, str(mod))
    (knowledge / "index.md").write_text("| Title | Source | Type | Summary | Date |\n|---|---|---|---|---|\n"
                                        "| Indexed | [u](https://example.org/indexed) | web-page | s | 2026-01-01 |\n")
    def doc(url, title):
        return f"# {title}\n\nSource: {url}\nType: web-page\nDate: 2026-01-01\n\n---\n\nBody of {title}.\n"
    (sources / "indexed.md").write_text(doc("https://example.org/indexed", "Indexed"))
    for i in range(unindexed):
        (sources / f"cookie{i}.md").write_text(doc(f"https://internal.example/doc{i}", f"Cookie {i}"))
    return crawlee


def test_parser_bounds_never_indexed_sources_and_reports_the_deferral(tmp_path, monkeypatch):
    crawlee = _crawlee_store(tmp_path, monkeypatch, unindexed=3)
    urls = [u for u, _, _ in crawlee.parse_all(max_unindexed_sources=1)]
    assert urls == ["https://example.org/indexed", "https://internal.example/doc0"]
    assert crawlee.last_parse_stats["unindexed_sources"] == 3
    assert crawlee.last_parse_stats["unindexed_sources_new"] == 3
    assert crawlee.last_parse_stats["unindexed_sources_deferred"] == 2
    # Already-captured never-indexed docs do not count against the bound.
    urls = [u for u, _, _ in crawlee.parse_all(already_processed={"https://internal.example/doc0"}, max_unindexed_sources=1)]
    assert urls == ["https://example.org/indexed", "https://internal.example/doc1"]
    assert crawlee.last_parse_stats["unindexed_sources_deferred"] == 1
    # Unbounded remains available and explicit.
    assert len(list(crawlee.parse_all(max_unindexed_sources=None))) == 4
    with pytest.raises(ValueError):
        list(crawlee.parse_all(max_unindexed_sources=-1))


def test_crawlee_channel_dry_run_reports_the_gate_and_prefilters_both_channels(tmp_path, monkeypatch):
    _crawlee_store(tmp_path, monkeypatch, unindexed=3)
    from src.capture import crawlee as channel
    import src.db
    asked = []
    monkeypatch.setattr(src.db, "get_processed_source_urls",
                        lambda c: asked.append(c) or ({"https://example.org/indexed"} if c == "article" else set()))
    result = channel.capture(dry_run=True, max_unindexed=2)
    assert sorted(asked) == ["article", "youtube"]
    assert result["article"]["planned"] == 2
    assert result["source_files"] == {"failed": 0, "unresolved": 0, "unindexed_output": 0, "unindexed_sources": 3,
                                      "unindexed_sources_new": 3, "unindexed_sources_deferred": 1, "max_unindexed_per_run": 2}
    assert exit_code(result) == 0


def test_crawlee_unresolved_index_rows_are_reported_not_failed(tmp_path, monkeypatch):
    """An index row without a source file is a store condition (66 live today),
    not a failure of tonight's run; R1 reported it the same way."""
    crawlee = _crawlee_store(tmp_path, monkeypatch, unindexed=0)
    with open(crawlee.INDEX_PATH, "a") as handle:
        handle.write("| Gone | [u](https://example.org/no-file) | web-page | s | 2026-01-01 |\n")
    from src.capture import crawlee as channel
    import src.db
    monkeypatch.setattr(src.db, "get_processed_source_urls", lambda c: set())
    result = channel.capture(dry_run=True)
    assert result["source_files"]["unresolved"] == 1 and result["source_files"]["failed"] == 0
    assert exit_code(result) == 0


# --------------------------------------------------------------------------- #
# N-9 — a bad header line fails one item, not the channel                        #
# --------------------------------------------------------------------------- #
def test_header_normalization_failure_is_per_item():
    from src.capture.framework import run_channel
    writer = Mock(return_value={"processed": 1, "skipped": 0, "failed": 0})
    bad = "# Title\n\n: value without a key\n\n---\n\nbody"
    good = "# Title\n\nKey: value\n\n---\n\nbody"
    stats = run_channel("article", [("https://x/bad", bad), ("https://x/good", good)], ingest=writer)
    assert stats == {"processed": 1, "skipped": 0, "failed": 1}
    assert [u for u, _ in writer.call_args.args[0]] == ["https://x/good"]
