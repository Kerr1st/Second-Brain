"""Tests for src/skim/cluster.py — greedy cosine clustering of a window's arrivals.

Synthetic vectors and ids only; the store is faked (no database, no model calls).
"""

from __future__ import annotations

import logging
import math

import pytest

from src.skim import cluster
from src.skim.cluster import CLUSTER_THRESHOLD, cluster_window, greedy_clusters


def _vec(angle_deg: float, dims: int = 4, scale: float = 1.0) -> list[float]:
    """A unit-ish vector in the plane of the first two axes at ``angle_deg``."""
    a = math.radians(angle_deg)
    v = [math.cos(a), math.sin(a)] + [0.0] * (dims - 2)
    return [x * scale for x in v]


class TestGreedyClusters:
    def test_groups_by_threshold_in_visiting_order_and_labels_only_pairs(self):
        vectors = {
            "a": _vec(0), "b": _vec(10),          # cos 10° ≈ 0.985 → with a
            "c": _vec(90), "d": _vec(95),         # a second cluster
            "e": _vec(180),                       # alone
        }
        out = greedy_clusters(["a", "b", "c", "d", "e"], vectors, threshold=0.86)
        assert out == {"a": "c1", "b": "c1", "c": "c2", "d": "c2", "e": None}

    def test_labels_follow_the_visiting_order(self):
        vectors = {"a": _vec(0), "b": _vec(10), "c": _vec(90), "d": _vec(95)}
        out = greedy_clusters(["c", "d", "a", "b"], vectors)
        assert out == {"c": "c1", "d": "c1", "a": "c2", "b": "c2"}

    def test_joins_the_most_similar_seed_not_the_first_one_above_threshold(self):
        vectors = {"a": _vec(0), "b": _vec(40), "x": _vec(35)}   # x is 35° from a (cos .82) and 5° from b (cos .996)
        out = greedy_clusters(["a", "b", "x"], vectors, threshold=0.80)
        assert out["x"] == out["b"] and out["a"] is None

    def test_scale_does_not_matter_and_zero_vectors_do_not_crash(self):
        vectors = {"a": _vec(0, scale=0.01), "b": _vec(5, scale=100), "z": [0.0, 0.0, 0.0, 0.0]}
        out = greedy_clusters(["a", "b", "z"], vectors)
        assert out["a"] == out["b"] == "c1" and out["z"] is None

    def test_ids_without_vectors_are_skipped_and_small_inputs_are_unlabelled(self):
        assert greedy_clusters(["a", "b"], {"a": _vec(0)}) == {"a": None}
        assert greedy_clusters([], {}) == {}
        assert greedy_clusters(["a", "ghost", "b"], {"a": _vec(0), "b": _vec(1)}) == {"a": "c1", "b": "c1"}

    def test_default_threshold_is_the_plan_value(self):
        assert CLUSTER_THRESHOLD == 0.86
        vectors = {"a": _vec(0), "b": _vec(30)}                  # cos 30° = 0.866 ≥ 0.86
        assert greedy_clusters(["a", "b"], vectors) == {"a": "c1", "b": "c1"}
        vectors = {"a": _vec(0), "b": _vec(31)}                  # cos 31° = 0.857 < 0.86
        assert greedy_clusters(["a", "b"], vectors) == {"a": None, "b": None}

    def test_deterministic(self):
        vectors = {f"d{i}": _vec(i * 7) for i in range(30)}
        order = [f"d{i}" for i in range(30)]
        assert greedy_clusters(order, vectors) == greedy_clusters(order, vectors)


class FakeStore:
    def __init__(self, vectors, fail_persist=False):
        self.vectors = vectors
        self.fail_persist = fail_persist
        self.persisted: list[tuple] = []

    def fetch_window_centroids(self, ids):
        return {i: self.vectors[i] for i in ids if i in self.vectors}

    def set_cluster_ids(self, labels, model, prompt_version):
        if self.fail_persist:
            raise RuntimeError("db down")
        self.persisted.append((dict(labels), model, prompt_version))
        return len(labels)


def _records(*ids):
    return [{"id": i, "title": "t", "source_type": "slack", "skim": {}, "cluster_id": None} for i in ids]


class TestClusterWindow:
    def test_labels_records_in_place_and_persists(self, caplog):
        store = FakeStore({"a": _vec(0), "b": _vec(5), "c": _vec(120)})
        recs = _records("a", "b", "c")
        with caplog.at_level(logging.INFO, logger="src.skim.cluster"):
            labels = cluster_window(recs, model="m", prompt_version="v", store=store)
        assert labels == {"a": "c1", "b": "c1", "c": None}
        assert [r["cluster_id"] for r in recs] == ["c1", "c1", None]
        assert store.persisted == [({"a": "c1", "b": "c1", "c": None}, "m", "v")]
        assert "2 documents in 1 clusters" in caplog.text and "3 of 3 documents with vectors" in caplog.text

    def test_persistence_failure_is_non_fatal(self):
        store = FakeStore({"a": _vec(0), "b": _vec(5)}, fail_persist=True)
        recs = _records("a", "b")
        assert cluster_window(recs, model="m", prompt_version="v", store=store) == {"a": "c1", "b": "c1"}
        assert recs[0]["cluster_id"] == "c1"

    def test_persist_false_skips_the_write(self):
        store = FakeStore({"a": _vec(0), "b": _vec(5)})
        cluster_window(_records("a", "b"), model="m", prompt_version="v", store=store, persist=False)
        assert store.persisted == []

    def test_fewer_than_two_records_do_nothing(self):
        store = FakeStore({"a": _vec(0)})
        recs = _records("a")
        assert cluster_window(recs, model="m", prompt_version="v", store=store) == {}
        assert recs[0]["cluster_id"] is None and store.persisted == []

    def test_records_without_vectors_stay_unlabelled(self):
        store = FakeStore({"a": _vec(0), "b": _vec(5)})
        recs = _records("a", "b", "no-vector")
        cluster_window(recs, model="m", prompt_version="v", store=store)
        assert [r["cluster_id"] for r in recs] == ["c1", "c1", None]

    def test_default_store_is_the_skim_store_module(self):
        assert cluster.default_store.__name__ == "src.skim.store"
