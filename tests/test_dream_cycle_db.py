"""Unit tests for src/dream_cycle_db.py.

Uses unittest.mock to mock the database connection layer.
Requirements: 1.1, 1.5, 1.6, 4.1, 10.1, 11.5, 11.6, 14.5
"""

import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call
from uuid import uuid4

from hypothesis import given, settings, strategies as st

from src.dream_cycle_db import (
    create_run,
    complete_run,
    store_candidate,
    get_recent_rejections,
    get_user_rejections,
    get_deferred_candidates,
    should_run_briefing,
    mark_user_rejected,
    get_evaluator_verdicts_for_run,
    was_feedback_injected,
    get_previous_run_id,
)


class TestCreateRun(unittest.TestCase):
    """Test create_run returns valid UUID and sets started_at."""

    @patch("src.dream_cycle_db.get_connection")
    def test_create_run_returns_uuid(self, mock_get_conn):
        expected_id = uuid4()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (expected_id,)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = create_run("scheduled")

        self.assertEqual(result, str(expected_id))
        mock_conn.commit.assert_called_once()
        mock_conn.close.assert_called_once()

    @patch("src.dream_cycle_db.get_connection")
    def test_create_run_executes_correct_sql(self, mock_get_conn):
        expected_id = uuid4()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (expected_id,)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        create_run("post_learn")

        mock_cur.execute.assert_called_once()
        sql_arg = mock_cur.execute.call_args[0][0]
        params = mock_cur.execute.call_args[0][1]
        self.assertIn("INSERT INTO dream_cycle_runs", sql_arg)
        self.assertIn("RETURNING id", sql_arg)
        self.assertEqual(params, ("post_learn",))


class TestCompleteRun(unittest.TestCase):
    """Test complete_run updates all stats fields."""

    @patch("src.dream_cycle_db.get_connection")
    def test_complete_run_updates_stats(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        run_id = str(uuid4())
        stats = {
            "candidates_generated": 8,
            "candidates_accepted": 3,
            "candidates_deferred": 2,
            "candidates_rejected": 3,
        }
        digest = "# Dream Cycle Digest"

        complete_run(run_id, stats, digest)

        mock_cur.execute.assert_called_once()
        sql_arg = mock_cur.execute.call_args[0][0]
        params = mock_cur.execute.call_args[0][1]
        self.assertIn("UPDATE dream_cycle_runs", sql_arg)
        self.assertIn("completed_at", sql_arg)
        self.assertEqual(params, (8, 3, 2, 3, digest, None, None, run_id))
        mock_conn.commit.assert_called_once()
        mock_conn.close.assert_called_once()

    @patch("src.dream_cycle_db.get_connection")
    def test_complete_run_defaults_missing_stats_to_zero(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        run_id = str(uuid4())
        complete_run(run_id, {}, "empty digest")

        params = mock_cur.execute.call_args[0][1]
        # All stats default to 0
        self.assertEqual(params, (0, 0, 0, 0, "empty digest", None, None, run_id))


class TestStoreCandidate(unittest.TestCase):
    """Test store_candidate with all verdict combinations."""

    def _make_candidate(self, operation="CREATE", target_memory_id=None,
                        schema_operation="assimilation"):
        return {
            "title": "Test Insight",
            "content": "Some content",
            "operation": operation,
            "target_memory_id": target_memory_id,
            "schema_operation": schema_operation,
        }

    def _make_verdicts(self, a="ACCEPT", b="ACCEPT", c="ACCEPT"):
        return {
            "evaluator_a_verdict": a,
            "evaluator_a_reasoning": f"Reasoning for {a}",
            "evaluator_b_verdict": b,
            "evaluator_b_reasoning": f"Reasoning for {b}",
            "evaluator_c_verdict": c,
            "evaluator_c_reasoning": f"Reasoning for {c}",
        }

    @patch("src.dream_cycle_db.get_connection")
    def test_store_accepted_candidate(self, mock_get_conn):
        candidate_id = uuid4()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (candidate_id,)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        run_id = str(uuid4())
        memory_id = str(uuid4())
        candidate = self._make_candidate()
        verdicts = self._make_verdicts()

        result = store_candidate(run_id, candidate, verdicts, "ACCEPTED", memory_id)

        self.assertEqual(result, str(candidate_id))
        sql_arg = mock_cur.execute.call_args[0][0]
        params = mock_cur.execute.call_args[0][1]
        self.assertIn("INSERT INTO dream_cycle_candidates", sql_arg)
        self.assertEqual(params[0], run_id)
        self.assertEqual(json.loads(params[1]), candidate)
        self.assertEqual(params[-2], "ACCEPTED")
        self.assertEqual(params[-1], memory_id)
        mock_conn.commit.assert_called_once()

    @patch("src.dream_cycle_db.get_connection")
    def test_store_deferred_candidate(self, mock_get_conn):
        candidate_id = uuid4()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (candidate_id,)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        run_id = str(uuid4())
        candidate = self._make_candidate()
        verdicts = self._make_verdicts(a="ACCEPT", b="ACCEPT", c="REJECT")

        result = store_candidate(run_id, candidate, verdicts, "DEFERRED")

        params = mock_cur.execute.call_args[0][1]
        self.assertEqual(params[-2], "DEFERRED")
        self.assertIsNone(params[-1])  # no created_memory_id

    @patch("src.dream_cycle_db.get_connection")
    def test_store_rejected_candidate(self, mock_get_conn):
        candidate_id = uuid4()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (candidate_id,)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        run_id = str(uuid4())
        candidate = self._make_candidate()
        verdicts = self._make_verdicts(a="REJECT", b="REJECT", c="REJECT")

        result = store_candidate(run_id, candidate, verdicts, "REJECTED")

        params = mock_cur.execute.call_args[0][1]
        self.assertEqual(params[-2], "REJECTED")
        self.assertIsNone(params[-1])

    @patch("src.dream_cycle_db.get_connection")
    def test_store_candidate_extracts_operation_fields(self, mock_get_conn):
        candidate_id = uuid4()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (candidate_id,)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        target_id = str(uuid4())
        candidate = self._make_candidate(
            operation="SUPERSEDE",
            target_memory_id=target_id,
            schema_operation="accommodation",
        )
        verdicts = self._make_verdicts()

        store_candidate(str(uuid4()), candidate, verdicts, "ACCEPTED")

        params = mock_cur.execute.call_args[0][1]
        # params[2] = operation, params[3] = target_memory_id, params[4] = schema_operation
        self.assertEqual(params[2], "SUPERSEDE")
        self.assertEqual(params[3], target_id)
        self.assertEqual(params[4], "accommodation")


class TestGetRecentRejections(unittest.TestCase):
    """Test get_recent_rejections returns correct cycles with reasoning."""

    @patch("src.dream_cycle_db.get_connection")
    def test_returns_rejections_with_reasoning(self, mock_get_conn):
        run_id = uuid4()
        candidate_id = uuid4()
        completed_at = datetime.now(timezone.utc)
        expected_rows = [
            {
                "run_id": run_id,
                "run_type": "scheduled",
                "completed_at": completed_at,
                "candidate_id": candidate_id,
                "final_verdict": "REJECTED",
                "evaluator_a_verdict": "REJECT",
                "evaluator_a_reasoning": "Not grounded in evidence",
                "evaluator_b_verdict": "ACCEPT",
                "evaluator_b_reasoning": "Relevant to user",
                "evaluator_c_verdict": "REJECT",
                "evaluator_c_reasoning": "Not falsifiable",
            }
        ]

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = expected_rows
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = get_recent_rejections(n_cycles=3)

        self.assertEqual(result, expected_rows)
        sql_arg = mock_cur.execute.call_args[0][0]
        params = mock_cur.execute.call_args[0][1]
        self.assertIn("REJECTED", sql_arg)
        self.assertIn("DEFERRED", sql_arg)
        self.assertEqual(params, (3,))
        mock_conn.close.assert_called_once()

    @patch("src.dream_cycle_db.get_connection")
    def test_returns_empty_when_no_cycles(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = get_recent_rejections()

        self.assertEqual(result, [])


class TestGetDeferredCandidates(unittest.TestCase):
    """Test get_deferred_candidates filters by run_id and DEFERRED status."""

    @patch("src.dream_cycle_db.get_connection")
    def test_returns_deferred_for_run(self, mock_get_conn):
        run_id = str(uuid4())
        expected_rows = [
            {
                "id": uuid4(),
                "candidate_json": {"title": "Deferred insight"},
                "evaluator_a_verdict": "ACCEPT",
                "evaluator_a_reasoning": "Good",
                "evaluator_b_verdict": "ACCEPT",
                "evaluator_b_reasoning": "Relevant",
                "evaluator_c_verdict": "REJECT",
                "evaluator_c_reasoning": "Needs more evidence",
            }
        ]

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = expected_rows
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = get_deferred_candidates(run_id)

        self.assertEqual(result, expected_rows)
        sql_arg = mock_cur.execute.call_args[0][0]
        params = mock_cur.execute.call_args[0][1]
        self.assertIn("DEFERRED", sql_arg)
        self.assertIn("run_id = %s", sql_arg)
        self.assertEqual(params, (run_id,))
        mock_conn.close.assert_called_once()

    @patch("src.dream_cycle_db.get_connection")
    def test_returns_empty_when_no_deferred(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = get_deferred_candidates(str(uuid4()))

        self.assertEqual(result, [])


class TestShouldRunBriefing(unittest.TestCase):
    """Test should_run_briefing enforces 24h cap and new-content condition."""

    @patch("src.dream_cycle_db.get_connection")
    @patch("src.dream_cycle_db.get_last_briefing_time")
    def test_first_run_always_allowed(self, mock_last_briefing, mock_get_conn):
        mock_last_briefing.return_value = None

        result = should_run_briefing()

        self.assertTrue(result)
        # Should not need DB connection since it returns early
        mock_get_conn.assert_not_called()

    @patch("src.dream_cycle_db.get_connection")
    @patch("src.dream_cycle_db.get_last_briefing_time")
    def test_within_24h_returns_false(self, mock_last_briefing, mock_get_conn):
        # Last briefing was 12 hours ago
        mock_last_briefing.return_value = datetime.now(timezone.utc) - timedelta(hours=12)

        result = should_run_briefing()

        self.assertFalse(result)
        # Should not check for new content since 24h cap not met
        mock_get_conn.assert_not_called()

    @patch("src.dream_cycle_db.get_connection")
    @patch("src.dream_cycle_db.get_last_briefing_time")
    def test_after_24h_with_new_memories_returns_true(self, mock_last_briefing, mock_get_conn):
        last_time = datetime.now(timezone.utc) - timedelta(hours=25)
        mock_last_briefing.return_value = last_time

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # First query: new memories exist
        mock_cur.fetchone.return_value = (True,)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = should_run_briefing()

        self.assertTrue(result)
        mock_conn.close.assert_called_once()

    @patch("src.dream_cycle_db.get_connection")
    @patch("src.dream_cycle_db.get_last_briefing_time")
    def test_after_24h_with_dream_cycle_ran_returns_true(self, mock_last_briefing, mock_get_conn):
        last_time = datetime.now(timezone.utc) - timedelta(hours=25)
        mock_last_briefing.return_value = last_time

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # First query: no new memories; second query: dream cycle ran
        mock_cur.fetchone.side_effect = [(False,), (True,)]
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = should_run_briefing()

        self.assertTrue(result)

    @patch("src.dream_cycle_db.get_connection")
    @patch("src.dream_cycle_db.get_last_briefing_time")
    def test_after_24h_no_new_content_returns_false(self, mock_last_briefing, mock_get_conn):
        last_time = datetime.now(timezone.utc) - timedelta(hours=25)
        mock_last_briefing.return_value = last_time

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # No new memories, no dream cycle ran
        mock_cur.fetchone.side_effect = [(False,), (False,)]
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = should_run_briefing()

        self.assertFalse(result)


class TestMarkUserRejected(unittest.TestCase):
    """Test mark_user_rejected preserves memory, records timestamp and reason."""

    @patch("src.dream_cycle_db.get_connection")
    def test_updates_candidate_and_memory_in_transaction(self, mock_get_conn):
        candidate_id = str(uuid4())
        memory_id = uuid4()
        reason = "Not relevant to my work"

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # First execute returns the created_memory_id
        mock_cur.fetchone.return_value = (memory_id,)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        mark_user_rejected(candidate_id, reason)

        # Should have two execute calls: candidate update + memory update
        self.assertEqual(mock_cur.execute.call_count, 2)

        # First call: update candidate with rejection
        first_sql = mock_cur.execute.call_args_list[0][0][0]
        first_params = mock_cur.execute.call_args_list[0][0][1]
        self.assertIn("UPDATE dream_cycle_candidates", first_sql)
        self.assertIn("user_rejected_at", first_sql)
        self.assertIn("user_rejection_reason", first_sql)
        self.assertEqual(first_params, (reason, candidate_id))

        # Second call: update memory status
        second_sql = mock_cur.execute.call_args_list[1][0][0]
        second_params = mock_cur.execute.call_args_list[1][0][1]
        self.assertIn("UPDATE memories", second_sql)
        self.assertIn("user_rejected", second_sql)
        self.assertEqual(second_params, (memory_id,))

        # Single commit = single transaction
        mock_conn.commit.assert_called_once()
        mock_conn.close.assert_called_once()

    @patch("src.dream_cycle_db.get_connection")
    def test_no_memory_update_when_no_created_memory(self, mock_get_conn):
        candidate_id = str(uuid4())

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # No created_memory_id
        mock_cur.fetchone.return_value = (None,)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        mark_user_rejected(candidate_id, "Bad insight")

        # Only one execute: candidate update, no memory update
        self.assertEqual(mock_cur.execute.call_count, 1)
        mock_conn.commit.assert_called_once()

    @patch("src.dream_cycle_db.get_connection")
    def test_no_memory_update_when_no_row_returned(self, mock_get_conn):
        candidate_id = str(uuid4())

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # No row returned (candidate doesn't exist)
        mock_cur.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        mark_user_rejected(candidate_id, "Bad insight")

        # Only one execute: candidate update, no memory update
        self.assertEqual(mock_cur.execute.call_count, 1)
        mock_conn.commit.assert_called_once()


class TestGetUserRejections(unittest.TestCase):
    """Test get_user_rejections returns user-rejected candidates from recent cycles."""

    @patch("src.dream_cycle_db.get_connection")
    def test_returns_user_rejections_with_reason_and_json(self, mock_get_conn):
        candidate_id = uuid4()
        expected_rows = [
            {
                "candidate_id": candidate_id,
                "user_rejection_reason": "Not relevant to my work",
                "candidate_json": {"title": "Some insight"},
            }
        ]

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = expected_rows
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = get_user_rejections(n_cycles=3)

        self.assertEqual(result, expected_rows)
        sql_arg = mock_cur.execute.call_args[0][0]
        params = mock_cur.execute.call_args[0][1]
        self.assertIn("user_rejected_at IS NOT NULL", sql_arg)
        self.assertIn("ACCEPTED", sql_arg)
        self.assertEqual(params, (3,))
        mock_conn.close.assert_called_once()

    @patch("src.dream_cycle_db.get_connection")
    def test_returns_empty_when_no_user_rejections(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = get_user_rejections()

        self.assertEqual(result, [])


class TestGetEvaluatorVerdictsForRun(unittest.TestCase):
    """Test get_evaluator_verdicts_for_run returns correct dict structure."""

    @patch("src.dream_cycle_db.get_connection")
    def test_returns_verdicts_keyed_by_title(self, mock_get_conn):
        run_id = str(uuid4())
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            (
                json.dumps({"title": "Insight A"}),
                "ACCEPT", "Solid reasoning",
                "ACCEPT", "User relevant",
                "ACCEPT", "Epistemically sound",
            ),
        ]
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = get_evaluator_verdicts_for_run(run_id)

        self.assertIn("Insight A", result)
        self.assertEqual(result["Insight A"]["skeptic"], {"verdict": "ACCEPT", "reasoning": "Solid reasoning"})
        self.assertEqual(result["Insight A"]["advocate"], {"verdict": "ACCEPT", "reasoning": "User relevant"})
        self.assertEqual(result["Insight A"]["epistemologist"], {"verdict": "ACCEPT", "reasoning": "Epistemically sound"})
        mock_conn.close.assert_called_once()

    @patch("src.dream_cycle_db.get_connection")
    def test_returns_empty_dict_when_no_accepted(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = get_evaluator_verdicts_for_run(str(uuid4()))

        self.assertEqual(result, {})

    @patch("src.dream_cycle_db.get_connection")
    def test_handles_dict_candidate_json(self, mock_get_conn):
        """When candidate_json is already a dict (psycopg2 JSONB auto-parse)."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            (
                {"title": "Dict Insight"},
                "ACCEPT", "Good",
                "REJECT", "Bad",
                "ACCEPT", "OK",
            ),
        ]
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = get_evaluator_verdicts_for_run(str(uuid4()))

        self.assertIn("Dict Insight", result)
        self.assertEqual(result["Dict Insight"]["advocate"]["verdict"], "REJECT")

    @patch("src.dream_cycle_db.get_connection")
    def test_handles_null_candidate_json(self, mock_get_conn):
        """When candidate_json is None."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            (None, "ACCEPT", "R1", "ACCEPT", "R2", "ACCEPT", "R3"),
        ]
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = get_evaluator_verdicts_for_run(str(uuid4()))

        # Title defaults to empty string
        self.assertIn("", result)

    @patch("src.dream_cycle_db.get_connection")
    def test_multiple_candidates(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            (json.dumps({"title": "A"}), "ACCEPT", "r1", "ACCEPT", "r2", "ACCEPT", "r3"),
            (json.dumps({"title": "B"}), "ACCEPT", "r4", "REJECT", "r5", "ACCEPT", "r6"),
        ]
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = get_evaluator_verdicts_for_run(str(uuid4()))

        self.assertEqual(len(result), 2)
        self.assertIn("A", result)
        self.assertIn("B", result)


class TestWasFeedbackInjected(unittest.TestCase):
    """Test was_feedback_injected returns True/False correctly."""

    @patch("src.dream_cycle_db.get_connection")
    def test_returns_true_when_feedback_present(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("Some feedback text",)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = was_feedback_injected(str(uuid4()))

        self.assertTrue(result)
        mock_conn.close.assert_called_once()

    @patch("src.dream_cycle_db.get_connection")
    def test_returns_false_when_feedback_empty(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("",)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = was_feedback_injected(str(uuid4()))

        self.assertFalse(result)

    @patch("src.dream_cycle_db.get_connection")
    def test_returns_false_when_feedback_none(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (None,)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = was_feedback_injected(str(uuid4()))

        self.assertFalse(result)

    @patch("src.dream_cycle_db.get_connection")
    def test_returns_false_when_no_row(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = was_feedback_injected(str(uuid4()))

        self.assertFalse(result)


class TestGetPreviousRunId(unittest.TestCase):
    """Test get_previous_run_id returns UUID string or None."""

    @patch("src.dream_cycle_db.get_connection")
    def test_returns_uuid_string_when_exists(self, mock_get_conn):
        expected_id = uuid4()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (expected_id,)
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = get_previous_run_id("scheduled")

        self.assertEqual(result, str(expected_id))
        sql_arg = mock_cur.execute.call_args[0][0]
        params = mock_cur.execute.call_args[0][1]
        self.assertIn("run_type = %s", sql_arg)
        self.assertIn("completed_at IS NOT NULL", sql_arg)
        self.assertIn("ORDER BY completed_at DESC", sql_arg)
        self.assertIn("LIMIT 1", sql_arg)
        self.assertEqual(params, ("scheduled",))
        mock_conn.close.assert_called_once()

    @patch("src.dream_cycle_db.get_connection")
    def test_returns_none_when_no_completed_run(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = get_previous_run_id("post_learn")

        self.assertIsNone(result)
        mock_conn.close.assert_called_once()

    @patch("src.dream_cycle_db.get_connection")
    def test_queries_correct_run_type(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        get_previous_run_id("user_triggered")

        params = mock_cur.execute.call_args[0][1]
        self.assertEqual(params, ("user_triggered",))


class TestDreamCycleDbQueryProperties(unittest.TestCase):
    """Property-based tests for new dream_cycle_db query functions.

    **Feature: dream-cycle-decomposition, Property 6: New dream_cycle_db query functions return correct results**
    **Validates: Requirements 4.1, 4.2, 8.4**
    """

    @given(
        run_type=st.sampled_from(["scheduled", "post_learn", "session_start", "user_triggered"]),
        has_result=st.booleans(),
    )
    @settings(max_examples=100)
    @patch("src.dream_cycle_db.get_connection")
    def test_get_previous_run_id_returns_str_or_none(self, mock_get_conn, run_type, has_result):
        """get_previous_run_id returns str(uuid) when row exists, None otherwise."""
        expected_id = uuid4()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (expected_id,) if has_result else None
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = get_previous_run_id(run_type)

        if has_result:
            self.assertEqual(result, str(expected_id))
            self.assertIsInstance(result, str)
        else:
            self.assertIsNone(result)

        params = mock_cur.execute.call_args[0][1]
        self.assertEqual(params, (run_type,))
        mock_conn.close.assert_called()

    @given(
        feedback_value=st.one_of(
            st.none(),
            st.just(""),
            st.text(min_size=1, max_size=200),
        ),
        row_exists=st.booleans(),
    )
    @settings(max_examples=100)
    @patch("src.dream_cycle_db.get_connection")
    def test_was_feedback_injected_bool_correctness(self, mock_get_conn, feedback_value, row_exists):
        """was_feedback_injected returns True iff row exists and value is truthy."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        if row_exists:
            mock_cur.fetchone.return_value = (feedback_value,)
        else:
            mock_cur.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = was_feedback_injected(str(uuid4()))

        expected = bool(row_exists and feedback_value)
        self.assertEqual(result, expected)
        mock_conn.close.assert_called()

    @given(
        num_candidates=st.integers(min_value=0, max_value=5),
    )
    @settings(max_examples=100)
    @patch("src.dream_cycle_db.get_connection")
    def test_get_evaluator_verdicts_returns_correct_structure(self, mock_get_conn, num_candidates):
        """get_evaluator_verdicts_for_run returns dict with correct keys per candidate."""
        rows = []
        expected_titles = []
        for i in range(num_candidates):
            title = f"Insight {i}"
            expected_titles.append(title)
            rows.append((
                json.dumps({"title": title}),
                "ACCEPT", f"skeptic reason {i}",
                "ACCEPT", f"advocate reason {i}",
                "ACCEPT", f"epistemologist reason {i}",
            ))

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = rows
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        result = get_evaluator_verdicts_for_run(str(uuid4()))

        self.assertEqual(len(result), num_candidates)
        for title in expected_titles:
            self.assertIn(title, result)
            entry = result[title]
            for role in ("skeptic", "advocate", "epistemologist"):
                self.assertIn(role, entry)
                self.assertIn("verdict", entry[role])
                self.assertIn("reasoning", entry[role])
        mock_conn.close.assert_called()
