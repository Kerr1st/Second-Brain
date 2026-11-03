"""Attention ledger tests: pure metrics (hermetic) and DB round trip (test DB)."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import src.db as db
import src.dream_cycle_db as dream_cycle_db
from src.dream_cycle import ledger
from src.models import MemorySlice

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
WINDOW = NOW - timedelta(days=1)


def _row(mid, *, type="source", source_type="slack", created=NOW, parent=None, project=None):
    return {"id": mid, "type": type, "source_type": source_type, "created_at": created,
            "parent_id": parent, "project": project}


class TestSliceMetrics:
    def test_arrival_source_episodic_parent_and_revisit_shares(self):
        old = NOW - timedelta(days=30)
        ids = ["a", "b", "c", "d", "ghost"]
        prov = {
            "a": _row("a", source_type="slack", parent="p1"),
            "b": _row("b", source_type="slack", parent="p1"),
            "c": _row("c", source_type="outlook_email"),
            "d": _row("d", type="insight", source_type="distilled_chat", created=old, project="second-brain"),
        }
        m = ledger.slice_metrics(ids, prov, WINDOW, previous_ids={"d", "zzz"})
        assert m["requested"] == 5 and m["resolved"] == 4          # ghost id dropped
        assert m["arrival_share"] == 0.75                           # a, b, c new; d old
        assert m["source_mix"] == {"slack": 2, "outlook_email": 1, "distilled_chat": 1}
        assert m["source_types"] == 3
        assert m["concentration"] == 0.5 and m["dominant_source"] == "slack"
        assert m["episodic_share"] == 0.75                          # d is semantic
        assert m["distinct_parents"] == 3                           # p1 (a,b), c, d
        assert m["revisit_share"] == 0.2                            # d of 5 requested
        assert m["top_project"] == "second-brain"

    def test_baseline_shape_all_old_syntheses_from_one_source(self):
        """The 2026-09-14..17 pattern: every memory an old synthesis from one channel."""
        old = NOW - timedelta(days=90)
        ids = [f"m{i}" for i in range(12)]
        prov = {i: _row(i, type="insight", source_type="distilled_chat", created=old) for i in ids}
        m = ledger.slice_metrics(ids, prov, WINDOW)
        assert m["arrival_share"] == 0.0 and m["source_types"] == 1
        assert m["concentration"] == 1.0 and m["episodic_share"] == 0.0

    def test_naive_created_at_is_treated_as_utc(self):
        prov = {"a": _row("a", created=NOW.replace(tzinfo=None))}
        assert ledger.slice_metrics(["a"], prov, WINDOW.replace(tzinfo=None))["arrival_share"] == 1.0

    def test_empty_slice(self):
        m = ledger.slice_metrics([], {}, WINDOW)
        assert m == {"requested": 0, "resolved": 0, "arrival_share": 0.0, "source_mix": {},
                     "source_types": 0, "concentration": 0.0, "dominant_source": "",
                     "episodic_share": 0.0, "distinct_parents": 0, "revisit_share": 0.0,
                     "top_project": None}


class TestRunMetrics:
    def test_memory_weighted_aggregation_and_anchored_count(self):
        s1 = {"metrics": {"resolved": 10, "arrival_share": 0.8, "source_mix": {"slack": 6, "outlook_email": 4},
                          "source_types": 2, "episodic_share": 1.0, "revisit_share": 0.0}}
        s2 = {"metrics": {"resolved": 10, "arrival_share": 0.0, "source_mix": {"distilled_chat": 10},
                          "source_types": 1, "episodic_share": 0.0, "revisit_share": 0.5}}
        r = ledger.run_metrics([s1, s2])
        assert r["slices"] == 2 and r["memories"] == 20
        assert r["arrival_share"] == 0.4 and r["episodic_share"] == 0.5 and r["revisit_share"] == 0.25
        assert r["source_types"] == 3 and r["concentration"] == 0.5     # distilled_chat 10/20
        assert r["arrival_anchored_slices"] == 1                          # only s1: >=60% new, >=2 sources

    def test_empty_run(self):
        assert ledger.run_metrics([])["memories"] == 0
        assert ledger.run_metrics([{"metrics": None}])["slices"] == 1

    def test_format_attention_section_mentions_every_slice(self):
        rows = [{"name": "Week in Slack", "strategy": "arrivals", "origin": "guaranteed_arrivals",
                 "metrics": {"resolved": 9, "requested": 10, "arrival_share": 0.9, "source_mix": {"slack": 9},
                             "source_types": 1, "concentration": 1.0, "dominant_source": "slack",
                             "episodic_share": 1.0, "distinct_parents": 9, "revisit_share": 0.0}}]
        text = "\n".join(ledger.format_attention_section(ledger.run_metrics(rows), rows))
        assert "## Attention" in text and "Week in Slack" in text and "[guaranteed_arrivals]" in text
        assert "Arrival share (created since previous run): 90%" in text


@pytest.mark.usefixtures("clean_tables")
class TestLedgerRoundTrip:
    """Against the real test database (migration 012 applied by conftest)."""

    @pytest.fixture(autouse=True)
    def _clean_runs(self, test_db):
        # clean_tables only clears memories; runs persist across tests and would
        # contaminate the previous-run lookup.
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM dream_cycle_slices")
                cur.execute("DELETE FROM dream_cycle_candidates")
                cur.execute("DELETE FROM dream_cycle_runs")
            conn.commit()
        yield

    def _run(self, run_type="scheduled", started_at=None):
        rid = dream_cycle_db.create_run(run_type)
        if started_at is not None:
            with db.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE dream_cycle_runs SET started_at=%s WHERE id=%s", (started_at, rid))
                conn.commit()
        return rid

    def _complete(self, rid):
        dream_cycle_db.complete_run(rid, stats={}, digest="")

    def test_record_and_read_back_with_metrics(self, sample_memory_factory):
        old_run = self._run(started_at=NOW - timedelta(days=2)); self._complete(old_run)
        rid = self._run(started_at=NOW)
        new_mem = sample_memory_factory(title="Slack: DM — today", source_type="slack")
        old_mem = sample_memory_factory(title="An old insight", type="insight", source_type="distilled_chat")
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE memories SET created_at=%s WHERE id=%s", (NOW - timedelta(hours=3), new_mem))
                cur.execute("UPDATE memories SET created_at=%s WHERE id=%s", (NOW - timedelta(days=40), old_mem))
            conn.commit()
        # A previous run "looked at" old_mem -> it is a revisit now.
        dream_cycle_db.record_slices(old_run, [{"name": "prev", "strategy": "x", "memory_ids": [str(old_mem)], "metrics": None}])

        slices = [MemorySlice(name="Mixed", strategy="temporal_juxtaposition",
                              memory_ids=[str(new_mem), str(old_mem), str(uuid.uuid4())],
                              memory_titles=["a", "b", "c"], hypothesis="h")]
        run = ledger.record_run_slices(rid, "scheduled", slices, started_at=NOW)

        assert run["slices"] == 1 and run["memories"] == 2
        assert run["arrival_share"] == 0.5 and run["source_types"] == 2 and run["revisit_share"] == pytest.approx(1 / 3, abs=1e-3)
        rows = dream_cycle_db.get_slices_for_run(rid)
        assert len(rows) == 1 and rows[0]["origin"] == "explorer" and rows[0]["metrics"]["resolved"] == 2
        assert rows[0]["memory_ids"][0] == str(new_mem)

    def test_arrival_window_prefers_previous_run_of_same_type(self):
        sched = self._run("scheduled", started_at=NOW - timedelta(days=3)); self._complete(sched)
        user = self._run("user_triggered", started_at=NOW - timedelta(hours=2)); self._complete(user)
        rid = self._run("scheduled", started_at=NOW)
        assert ledger.arrival_window_start(rid, "scheduled", NOW) == NOW - timedelta(days=3)
        assert ledger.arrival_window_start(rid, "post_learn", NOW) == NOW - ledger.DEFAULT_ARRIVAL_WINDOW

    def test_record_replaces_positions_on_retry(self):
        rid = self._run()
        dream_cycle_db.record_slices(rid, [{"name": "first", "strategy": "s", "memory_ids": [], "metrics": None}])
        dream_cycle_db.record_slices(rid, [{"name": "second", "strategy": "s", "memory_ids": [], "metrics": None}])
        rows = dream_cycle_db.get_slices_for_run(rid)
        assert [r["name"] for r in rows] == ["second"]

    def test_ledger_recovers_from_storage_failure(self):
        with patch.object(dream_cycle_db, "record_slices", side_effect=OSError("db down")):
            assert ledger.record_run_slices(str(uuid.uuid4()), "scheduled", [], started_at=NOW) == {}
