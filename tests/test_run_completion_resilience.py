"""A judged run always closes its run row with real counts.

Review C S2 (digest refusal marked a fully judged run aborted), E N1 / C risk 3
(SIGTERM from the job wrapper left ``completed_at`` NULL), F g2 (the generic
exception arm was untested) and F-065 (``feedback_injected`` passed to the
digest was untested). Synthetic doubles only; no database, no model calls.
"""
from __future__ import annotations

import os
import signal
from pathlib import Path
from unittest.mock import patch

import pytest

from src.dream_cycle import digest as digest_module
from src.dream_cycle.digest import write_digest
from src.dream_cycle.orchestrator import DreamCycleOrchestrator, RunInterrupted
from src.models import CandidateInsight, EvaluatorVerdict, MemorySlice


# ---------------------------------------------------------------------------
# write_digest: private subdirectory by default; never refuses the logs dir
# ---------------------------------------------------------------------------

def test_default_digest_dir_is_a_private_subdirectory_of_logs(tmp_path, monkeypatch):
    fake_repo = tmp_path / "repo"
    (fake_repo / "logs").mkdir(parents=True, mode=0o755)
    (fake_repo / "logs").chmod(0o755)  # the installed logs/ is 0755
    monkeypatch.setattr(digest_module, "__file__", str(fake_repo / "src" / "dream_cycle" / "digest.py"))
    monkeypatch.delenv("SECOND_BRAIN_DIGEST_DIR", raising=False)
    result = Path(write_digest("run-1", "text", date="2026-10-05"))
    assert result.parent == fake_repo / "logs" / "digests"
    assert (fake_repo / "logs").stat().st_mode & 0o777 == 0o755  # untouched
    assert result.parent.stat().st_mode & 0o777 == 0o700
    assert result.stat().st_mode & 0o777 == 0o600
    assert result.read_text() == "text"


def test_explicit_non_private_directory_is_used_with_a_private_file(tmp_path, monkeypatch, caplog):
    output = tmp_path / "shared"
    output.mkdir(mode=0o755)
    output.chmod(0o755)
    monkeypatch.setenv("SECOND_BRAIN_DIGEST_DIR", str(output))
    with caplog.at_level("WARNING", logger="src.dream_cycle.digest"):
        result = Path(write_digest("run-2", "text", date="2026-10-05"))
    assert result.parent == output and result.read_text() == "text"
    assert result.stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o755
    assert any("not private" in r.getMessage() for r in caplog.records)


def test_symlinked_digest_directory_is_still_refused(tmp_path, monkeypatch):
    real = tmp_path / "real"; real.mkdir(mode=0o700)
    link = tmp_path / "link"; link.symlink_to(real)
    monkeypatch.setenv("SECOND_BRAIN_DIGEST_DIR", str(link))
    with pytest.raises(ValueError, match="symlink"):
        write_digest("run-3", "text", date="2026-10-05")


# ---------------------------------------------------------------------------
# Orchestrator completion under failures
# ---------------------------------------------------------------------------

def _candidate(title="c1"):
    return CandidateInsight(title=title, type="insight", operation="CREATE", target_memory_id=None,
                            content="Body of the candidate", schema_operation="assimilation", schema_note="n",
                            confidence="high", confidence_reasoning="r", supersedes_reason=None,
                            source_memories=[SOURCE_ID], relationships=[],
                            strategy_that_found_it="arrivals_synthesis")


def _verdict(role, verdict="ACCEPT"):
    return EvaluatorVerdict(role=role, verdict=verdict, reasoning="ok", score=90, blocking_issues=[], advisory_issues=[])


SOURCE_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"


SNAPSHOTS = {SOURCE_ID: {"id": SOURCE_ID, "content": "evidence one"},
             OTHER_ID: {"id": OTHER_ID, "content": "evidence two"}}


def _orchestrator():
    orch = DreamCycleOrchestrator()
    orch._allocate_attention = lambda run_id, run_type, started_at, slices: slices
    return orch


def _thinking(orch, produce):
    """A Thinker double that, like the real one, caches the slice snapshots it read
    (what authorizes a candidate's citations) before returning candidates."""
    def think(slice_):
        orch._slice_memory_cache.update(SNAPSHOTS)
        return produce(slice_)
    return think


def _stats():
    return {"total_count": 5, "recent_activity": 1, "date_range": {"min": None, "max": None}, "type_distribution": {}}


SLICES = [MemorySlice(name="s1", strategy="arrivals_synthesis", memory_ids=[SOURCE_ID]),
          MemorySlice(name="s2", strategy="arrivals_synthesis", memory_ids=[SOURCE_ID, OTHER_ID])]


@pytest.fixture(autouse=True)
def _no_database_authorization(monkeypatch):
    """Keep the pure citation checks; skip the live-row read (no database here)."""
    from src.dream_cycle.storage import validate_candidate
    monkeypatch.setattr("src.dream_cycle.orchestrator.authorize_candidate", validate_candidate)


def _completion(mock_db):
    mock_db.complete_run.assert_called_once()
    kwargs = mock_db.complete_run.call_args.kwargs
    return kwargs["stats"], kwargs["digest"]


def test_digest_failure_does_not_abort_a_judged_run():
    orch = _orchestrator()
    with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
         patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
         patch.object(orch, "_skim_arrivals", return_value=None), \
         patch.object(orch, "invoke_explorer", return_value=SLICES[:1]), \
         patch.object(orch, "invoke_thinker", side_effect=_thinking(orch, lambda s: [_candidate()])), \
         patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
         patch.object(orch, "_invoke_evaluator_safe", side_effect=lambda c, role: _verdict(role)), \
         patch("src.dream_cycle.orchestrator.store_accepted", return_value="mem-1"), \
         patch("src.dream_cycle.orchestrator.generate_digest", side_effect=PermissionError("logs/ is 0755")):
        mock_db.create_run.return_value = "run-digest"
        mock_db.get_memory_stats.return_value = _stats()
        result = orch.run("scheduled")
    assert result.aborted_early is False
    assert (result.candidates_generated, result.candidates_accepted, result.candidates_rejected) == (1, 1, 0)
    assert result.digest_path == ""
    stats, digest = _completion(mock_db)
    assert stats["aborted"] is False and digest == ""
    assert (stats["candidates_generated"], stats["candidates_accepted"], stats["candidates_rejected"]) == (1, 1, 0)


@pytest.mark.parametrize("feedback, expected", [("Earlier rejections: ...", True), ("", False)])
def test_feedback_injected_flag_is_passed_to_the_digest(feedback, expected):
    """F-065: the digest must not fall back to reading an unwritten completion row."""
    orch = _orchestrator()
    with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
         patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=feedback), \
         patch.object(orch, "_skim_arrivals", return_value=None), \
         patch.object(orch, "invoke_explorer", return_value=SLICES[:1]), \
         patch.object(orch, "invoke_thinker", side_effect=_thinking(orch, lambda s: [_candidate()])), \
         patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
         patch.object(orch, "_invoke_evaluator_safe", side_effect=lambda c, role: _verdict(role, "REJECT")), \
         patch("src.dream_cycle.orchestrator.generate_digest", return_value="/tmp/d.md") as digest:
        mock_db.create_run.return_value = "run-fb"
        mock_db.get_memory_stats.return_value = _stats()
        orch.run("scheduled")
    assert digest.call_args.kwargs["feedback_injected"] is expected
    mock_db.was_feedback_injected.assert_not_called()


def test_unexpected_exception_after_a_judged_candidate_keeps_real_counts():
    """F g2: the generic-exception abort arm records the work already persisted."""
    orch = _orchestrator()

    def evaluator(candidate, role):
        if candidate.title == "c2":
            raise KeyError("boom")  # not a transient (TimeoutError/RuntimeError/ValueError): escapes the retry loop
        return _verdict(role)

    with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
         patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
         patch.object(orch, "_skim_arrivals", return_value=None), \
         patch.object(orch, "invoke_explorer", return_value=SLICES), \
         patch.object(orch, "invoke_thinker", side_effect=_thinking(orch, lambda s: [_candidate("c1")] if s.name == "s1" else [_candidate("c2")])), \
         patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
         patch.object(orch, "_invoke_evaluator_safe", side_effect=evaluator), \
         patch("src.dream_cycle.orchestrator.store_accepted", return_value="mem-1"):
        mock_db.create_run.return_value = "run-exc"
        mock_db.get_memory_stats.return_value = _stats()
        result = orch.run("scheduled")
    assert result.aborted_early is True
    assert (result.candidates_generated, result.candidates_accepted) == (2, 1)
    stats, digest = _completion(mock_db)
    assert stats["aborted"] is True and digest == ""
    assert (stats["candidates_generated"], stats["candidates_accepted"], stats["candidates_rejected"]) == (2, 1, 0)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_mid_run_closes_the_run_as_aborted_with_real_counts(signum):
    """No completed_at NULL orphan when the job wrapper or launchd stops the process."""
    orch = _orchestrator()

    def produce(slice_):
        if slice_.name == "s2":
            os.kill(os.getpid(), signum)  # delivered to this thread; handler raises RunInterrupted
            raise AssertionError("signal handler did not interrupt the Thinker")
        return [_candidate("c1")]
    thinker = _thinking(orch, produce)

    with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
         patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
         patch.object(orch, "_skim_arrivals", return_value=None), \
         patch.object(orch, "invoke_explorer", return_value=SLICES), \
         patch.object(orch, "invoke_thinker", side_effect=thinker), \
         patch.object(orch, "_invoke_evaluator_safe") as evaluator:
        mock_db.create_run.return_value = "run-sig"
        mock_db.get_memory_stats.return_value = _stats()
        before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        result = orch.run("scheduled")
    evaluator.assert_not_called()  # no paid judging after the stop request
    assert result.aborted_early is True and result.candidates_generated == 1
    stats, digest = _completion(mock_db)
    assert stats["aborted"] is True and digest == ""
    assert stats["candidates_generated"] == 1 and stats["interrupted_by"] == signal.Signals(signum).name
    assert "cost_usd" in stats and "spend_reserved_usd" in stats
    # Handlers are restored after the run.
    assert {s: signal.getsignal(s) for s in before} == before


def test_run_interrupted_is_not_swallowed_by_stage_fallbacks():
    """The per-stage ``except Exception`` fallbacks must not catch the stop request."""
    assert not issubclass(RunInterrupted, Exception)
    assert issubclass(RunInterrupted, BaseException)


# ---------------------------------------------------------------------------
# Storage failures after the panel: the reason is logged and persisted
# ---------------------------------------------------------------------------

def test_failure_message_is_one_line_and_truncated():
    from src.dream_cycle.orchestrator import MAX_FAILURE_MESSAGE_CHARS, _failure_message
    assert _failure_message(ValueError("bad\n  target")) == "ValueError: bad target"
    long = _failure_message(RuntimeError("x" * 1000))
    assert long.startswith("RuntimeError: xxx") and len(long) == MAX_FAILURE_MESSAGE_CHARS and long.endswith("…")


def test_storage_failure_logs_and_persists_the_exception_message(caplog):
    from src.dream_cycle.storage import CandidateAuthorizationError
    orch = _orchestrator()
    reason = "Every source and target must exist and be active " + "y" * 400
    with patch("src.dream_cycle.orchestrator.dream_cycle_db") as mock_db, \
         patch("src.dream_cycle.orchestrator.build_feedback_injection", return_value=""), \
         patch.object(orch, "_skim_arrivals", return_value=None), \
         patch.object(orch, "invoke_explorer", return_value=SLICES[:1]), \
         patch.object(orch, "invoke_thinker", side_effect=_thinking(orch, lambda s: [_candidate()])), \
         patch.object(orch, "_ground_candidate", side_effect=lambda c: c), \
         patch.object(orch, "_invoke_evaluator_safe", side_effect=lambda c, role: _verdict(role)), \
         patch("src.dream_cycle.orchestrator.store_accepted", side_effect=CandidateAuthorizationError(reason)), \
         patch("src.dream_cycle.orchestrator.generate_digest", return_value=""), \
         caplog.at_level("WARNING", logger="src.dream_cycle.orchestrator"):
        mock_db.create_run.return_value = "run-storage"
        mock_db.get_memory_stats.return_value = _stats()
        result = orch.run("scheduled")
    assert result.candidates_accepted == 0 and not result.aborted_early
    stored, verdicts, final = mock_db.store_candidate.call_args.args[1:4]
    assert final == "BLOCKED" and verdicts  # the real votes are kept
    assert stored["storage_error"] == "CandidateAuthorizationError"
    assert stored["block_reason"].startswith("CandidateAuthorizationError: Every source and target must exist")
    assert len(stored["block_reason"]) == 300
    [record] = [r for r in caplog.records if "storage blocked" in r.getMessage()]
    assert stored["block_reason"] in record.getMessage()
