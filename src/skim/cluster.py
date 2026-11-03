"""Cluster a window's arrivals by their existing embeddings — no model calls.

Phase 3 of docs/PLAN-skim-stage.md (optional item): the inventory tags documents
that are about the same matter with ``[c<k>]`` so the Explorer can see a Slack
thread, an email and a calendar event as one story. The vectors already exist —
the ingest embeds every parent memory or its chunks (pgvector ``vector(1024)``,
verified on the live schema 2026-09-20: 329 of 868 window arrivals carry their
own embedding, the other 539 have embedded chunks; ``COALESCE(own, avg(chunks))``
covers 100%) — so this costs one SQL round trip and a matrix product.

:func:`greedy_clusters` is the pure algorithm (numpy): documents are visited in
inventory order; each joins the existing cluster whose *seed* (first member) is
most similar when that cosine similarity is at least ``threshold``, otherwise
it seeds a new cluster. Only clusters with two or more members get a label
(``c1``, ``c2``, … in order of first appearance); singletons stay ``None``.
Seed comparison (rather than centroid) keeps the result deterministic and
cheap, and a cluster can never drift away from the document that started it.

:func:`cluster_window` does the I/O through :mod:`src.skim.store` and never
raises for a data problem: with fewer than two vectors it returns ``{}``.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

import numpy as np

from src.skim import store as default_store

logger = logging.getLogger(__name__)

#: Cosine similarity at or above which two arrivals are "the same matter".
CLUSTER_THRESHOLD = 0.86


def greedy_clusters(order: Iterable[str], vectors: dict[str, Any],
                    threshold: float = CLUSTER_THRESHOLD) -> dict[str, str | None]:
    """``{id: "c<k>" | None}`` for every id in ``order`` that has a vector.

    ``order`` fixes both the visiting order and the label numbering; ids
    without a vector are skipped. A cluster label is assigned only when the
    cluster ends up with at least two members.
    """
    ids = [str(i) for i in order if str(i) in vectors]
    if len(ids) < 2:
        return {i: None for i in ids}
    matrix = np.asarray([vectors[i] for i in ids], dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[1] == 0:
        return {i: None for i in ids}
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0
    unit = matrix / norms[:, None]

    seeds: list[int] = []               # row index of each cluster's seed
    members: list[list[int]] = []
    for row in range(len(ids)):
        best, best_sim = -1, threshold
        if seeds:
            sims = unit[seeds] @ unit[row]
            k = int(np.argmax(sims))
            if sims[k] >= best_sim:
                best = k
        if best >= 0:
            members[best].append(row)
        else:
            seeds.append(row)
            members.append([row])

    out: dict[str, str | None] = {i: None for i in ids}
    label = 0
    for group in members:
        if len(group) < 2:
            continue
        label += 1
        for row in group:
            out[ids[row]] = f"c{label}"
    return out


def cluster_window(records: list[dict], *, model: str, prompt_version: str,
                   threshold: float = CLUSTER_THRESHOLD, store=None, persist: bool = True) -> dict[str, str | None]:
    """Cluster the window's skim ``records`` (``store.fetch_window_records`` rows,
    any order) and write the labels into ``record["cluster_id"]`` in place.

    Vectors come from :func:`src.skim.store.fetch_window_centroids`; with
    ``persist`` the labels are also written to ``memory_skim.cluster_id``.
    Returns the ``{id: label}`` mapping (labelled ids only, plus None entries
    for singletons). Logs counts only.
    """
    store = store if store is not None else default_store
    ids = [str(r.get("id")) for r in records]
    if len(ids) < 2:
        return {}
    vectors = store.fetch_window_centroids(ids)
    labels = greedy_clusters(ids, vectors, threshold)
    for r in records:
        r["cluster_id"] = labels.get(str(r.get("id")))
    n_clusters = len({v for v in labels.values() if v})
    n_clustered = sum(1 for v in labels.values() if v)
    if persist and labels:
        try:
            store.set_cluster_ids(labels, model, prompt_version)
        except Exception:  # noqa: BLE001 — the labels are already on the records; persistence is a bonus
            logger.warning("Cluster ids could not be persisted (non-fatal)", exc_info=True)
    logger.info("Skim clusters: %d of %d documents with vectors; %d documents in %d clusters (threshold %.2f)",
                len(vectors), len(ids), n_clustered, n_clusters, threshold)
    return labels
