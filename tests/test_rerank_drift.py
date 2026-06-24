"""DB-backed drift guard for the rerank scorer.

Asserts that the production scorer (``src.search.rerank``) and the evaluation
scorer (``scripts.eval.eval_common.rerank_with_overrides`` driven by
``PRODUCTION_WEIGHTS``) compute identical scores on realistic, DB-sourced inputs.
If the two scoring paths ever diverge, this test fails loudly.

This module is built in two parts:

* Task 6.1 (this file) seeds purpose-built rows that exercise every
  additive-magnitude branch of the scorer (type boost, mem_class boost,
  cross-project penalty, supersession penalty, staleness penalty, reinforcement)
  and provides a placeholder retrievability check so the file runs green.
* Task 6.2 adds the production-vs-eval score-agreement assertions.

Validates: Requirements 3.4, 3.5
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.db as db
from src.search import hybrid_search, rerank
from src.rerank_weights import TYPE_BOOST_TYPES, MEM_CLASS_BOOST

from tests.conftest import _deterministic_embedding


# The query whose tokens every seeded row shares, so all seeded rows surface in
# hybrid_search results (via full-text overlap) for a single retrieval.
BASE_QUERY = "graph database indexing query optimization techniques"

# The project the *query* runs under. Rows in a different non-NULL project incur
# the cross-project penalty; rows in this project or with NULL project do not.
QUERY_PROJECT = "project-alpha"


def _content(unique_suffix: str) -> str:
    """Build content that shares the query's tokens (so it is retrievable) while
    staying unique in its first 300 chars (so hybrid_search dedup keeps it)."""
    return f"{BASE_QUERY} — {unique_suffix}"


# Each spec describes one seeded row and the additive-magnitude branch it covers.
# ``backdate_days`` / ``access_count`` / ``last_accessed_days_ago`` are applied
# with a direct SQL UPDATE after creation because create_memory does not accept
# created_at / access_count / last_accessed_at.
SEED_SPECS = [
    {
        "key": "idea_semantic_alpha_ctx",
        "type": "idea",                  # in TYPE_BOOST_TYPES
        "mem_class": "semantic",         # MEM_CLASS_BOOST semantic
        "project": QUERY_PROJECT,        # same project -> no penalty
        "encoding_context": "captured while benchmarking graph databases",
        "access_count": 4,               # exercises reinforcement
        "last_accessed_days_ago": 7,
        "branch": "type_boost+semantic+reinforcement+has_context",
    },
    {
        "key": "synthesis_procedural_noctx",
        "type": "synthesis",             # in TYPE_BOOST_TYPES
        "mem_class": "procedural",       # MEM_CLASS_BOOST procedural
        "project": None,
        "encoding_context": None,        # no encoding_context
        "branch": "type_boost+procedural+no_context",
    },
    {
        "key": "insight_episodic",
        "type": "insight",               # in TYPE_BOOST_TYPES
        "mem_class": "episodic",         # not in MEM_CLASS_BOOST -> 0
        "project": None,
        "branch": "type_boost+episodic(no_class_boost)",
    },
    {
        "key": "decision_noclass_beta",
        "type": "decision",              # in TYPE_BOOST_TYPES
        "mem_class": None,               # None -> 0 class boost
        "project": "project-beta",       # different project -> cross-project penalty
        "branch": "type_boost+none_class+project_penalty",
    },
    {
        "key": "note_nonboosted_semantic",
        "type": "note",                  # NOT in TYPE_BOOST_TYPES -> 0 type boost
        "mem_class": "semantic",
        "project": None,
        "branch": "non_boosted_type+semantic",
    },
    {
        "key": "superseded_idea",
        "type": "idea",
        "mem_class": None,
        "project": None,
        "status": "superseded",          # supersession penalty
        "branch": "superseded_penalty",
    },
    {
        "key": "stale_unretrieved",
        "type": "note",
        "mem_class": None,
        "project": None,
        "access_count": 0,               # never retrieved
        "backdate_days": 120,            # > 90 days -> staleness penalty
        "branch": "staleness_penalty",
    },
]


def _apply_post_create_fields(memory_id, spec):
    """Apply created_at / access_count / last_accessed_at that create_memory
    does not accept, via a direct SQL UPDATE."""
    sets = []
    params = []
    if "backdate_days" in spec:
        sets.append("created_at = %s")
        params.append(datetime.now(timezone.utc) - timedelta(days=spec["backdate_days"]))
    if "access_count" in spec:
        sets.append("access_count = %s")
        params.append(spec["access_count"])
    if "last_accessed_days_ago" in spec:
        sets.append("last_accessed_at = %s")
        params.append(datetime.now(timezone.utc) - timedelta(days=spec["last_accessed_days_ago"]))
    if not sets:
        return
    params.append(memory_id)
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE memories SET {', '.join(sets)} WHERE id = %s",
                params,
            )
        conn.commit()


@pytest.fixture()
def seeded_memories(test_db, clean_tables):
    """Seed branch-covering rows and return coverage metadata.

    Returns a dict:
        {
          "query": str,
          "query_project": str,
          "by_key": {key: memory_id},
          "by_id": {memory_id: spec},
        }
    """
    by_key = {}
    by_id = {}
    for spec in SEED_SPECS:
        content = _content(spec["key"])
        memory_id = db.create_memory(
            type=spec["type"],
            title=f"{spec['key']} :: {BASE_QUERY}",
            content=content,
            embedding=str(_deterministic_embedding(content)),
            status=spec.get("status", "active"),
            mem_class=spec.get("mem_class"),
            project=spec.get("project"),
            encoding_context=spec.get("encoding_context"),
        )
        _apply_post_create_fields(memory_id, spec)
        by_key[spec["key"]] = memory_id
        by_id[memory_id] = spec

    return {
        "query": BASE_QUERY,
        "query_project": QUERY_PROJECT,
        "by_key": by_key,
        "by_id": by_id,
    }


def _production_results(seeded):
    """Run the production retrieval path over the seeded rows."""
    query = seeded["query"]
    emb = _deterministic_embedding(query)
    # No project filter on hybrid_search so the cross-project row is retrieved;
    # the cross-project penalty is driven by query_project on rerank().
    results = hybrid_search(query, emb, limit=50)
    return rerank(results, query, query_project=seeded["query_project"])


def test_seeded_rows_are_retrievable(seeded_memories):
    """Placeholder coverage check (task 6.1): every seeded row is retrievable via
    hybrid_search + rerank, and every additive-magnitude branch is present among
    the scored results. Task 6.2 adds production-vs-eval score agreement."""
    results = _production_results(seeded_memories)

    result_ids = {str(r["id"]) for r in results}
    seeded_ids = set(seeded_memories["by_id"].keys())
    missing = seeded_ids - result_ids
    assert not missing, f"seeded rows not retrieved: {missing}"

    # Map back to the spec for each retrieved seeded row.
    by_id = seeded_memories["by_id"]
    seeded_results = [r for r in results if str(r["id"]) in by_id]

    # Every row carries the full set of intermediate underscore signals.
    required_signals = {
        "_overlap", "_title_overlap", "_context_overlap", "_recency",
        "_length_score", "_depth_score", "_type_boost", "_mem_class_boost",
        "_reinforcement", "_spacing_bonus", "_project_penalty",
        "_superseded_penalty", "_staleness_penalty",
    }
    for r in seeded_results:
        assert required_signals <= set(r.keys()), (
            f"missing signals on {r['id']}: {required_signals - set(r.keys())}"
        )

    # --- Assert each additive-magnitude branch is actually exercised. ---

    # Type boost: at least one boosted type and at least one non-boosted type.
    assert any(r["_type_boost"] > 0 for r in seeded_results), "no type-boosted row present"
    assert any(r["_type_boost"] == 0 for r in seeded_results), "no non-boosted-type row present"
    # And every TYPE_BOOST_TYPES value is represented among seeded rows.
    seeded_types = {by_id[str(r["id"])]["type"] for r in seeded_results}
    assert set(TYPE_BOOST_TYPES) <= seeded_types, (
        f"not every TYPE_BOOST_TYPES value seeded: missing {set(TYPE_BOOST_TYPES) - seeded_types}"
    )

    # mem_class boost: semantic (0.04), procedural (0.02), and episodic/None (0.0).
    class_boosts = {round(r["_mem_class_boost"], 4) for r in seeded_results}
    assert MEM_CLASS_BOOST["semantic"] in class_boosts, "no semantic mem_class row scored"
    assert MEM_CLASS_BOOST["procedural"] in class_boosts, "no procedural mem_class row scored"
    assert 0.0 in class_boosts, "no episodic/None mem_class row scored"

    # Cross-project penalty present.
    assert any(r["_project_penalty"] < 0 for r in seeded_results), "no cross-project penalty exercised"

    # Supersession penalty present.
    assert any(r["_superseded_penalty"] < 0 for r in seeded_results), "no superseded penalty exercised"

    # Staleness penalty present.
    assert any(r["_staleness_penalty"] < 0 for r in seeded_results), "no staleness penalty exercised"

    # Reinforcement present (access_count > 0 row).
    assert any(r["_reinforcement"] > 0 for r in seeded_results), "no reinforcement exercised"

    # Encoding-context overlap: at least one row with context overlap and one without.
    assert any(r["_context_overlap"] > 0 for r in seeded_results), "no row with encoding_context overlap"
    assert any(r["_context_overlap"] == 0 for r in seeded_results), "no row without encoding_context"


def test_production_and_eval_scorers_agree(seeded_memories):
    """Drift guard (task 6.2): the production scorer (src.search.rerank) and the
    eval scorer (rerank_with_overrides driven by PRODUCTION_WEIGHTS) must compute
    identical rerank_scores on realistic, DB-sourced inputs.

    rerank_with_overrides recomputes reinforcement purely from stored fields
    (access_count, _spacing_bonus) and otherwise reads the stored underscore
    signals, so the comparison is clock-independent — no time freezing needed.

    Validates: Requirements 3.1, 3.2, 3.3, 3.6
    """
    from scripts.eval.eval_common import rerank_with_overrides, PRODUCTION_WEIGHTS

    # 1. Production scores (capture before any re-scoring mutates anything).
    results = _production_results(seeded_memories)
    by_id = seeded_memories["by_id"]
    production_scores = {
        str(r["id"]): r["rerank_score"] for r in results if str(r["id"]) in by_id
    }

    # Confirm every seeded row surfaced so coverage can't silently vanish.
    seeded_ids = set(by_id.keys())
    missing = seeded_ids - set(production_scores.keys())
    assert not missing, f"seeded rows not retrieved (coverage gap): {missing}"

    # 2. Re-score a deep copy via the eval path; PRODUCTION_WEIGHTS at baseline
    #    must reproduce the production formula exactly. Deep-copy so production
    #    results are not mutated by the in-place re-score.
    copy = [dict(r) for r in results]
    rescored = rerank_with_overrides(copy, PRODUCTION_WEIGHTS)
    eval_scores = {
        str(r["id"]): r["rerank_score"] for r in rescored if str(r["id"]) in by_id
    }

    # 3. Per-id score agreement within 1e-9.
    assert set(eval_scores.keys()) == set(production_scores.keys()), (
        "eval scorer dropped/added seeded ids: "
        f"{set(production_scores.keys()) ^ set(eval_scores.keys())}"
    )
    for mem_id, prod_score in production_scores.items():
        assert eval_scores[mem_id] == pytest.approx(prod_score, abs=1e-9), (
            f"score drift on {mem_id}: production={prod_score} eval={eval_scores[mem_id]}"
        )

    # 4. Assert every additive-magnitude branch is actually present among the
    #    scored results, so the agreement above can't be vacuously satisfied by
    #    a corpus that never exercises a branch.
    seeded_results = [r for r in results if str(r["id"]) in by_id]

    # Type boost: boosted (>0) and non-boosted (==0) both present.
    assert any(r["_type_boost"] > 0 for r in seeded_results), "no type-boosted row present"
    assert any(r["_type_boost"] == 0 for r in seeded_results), "no non-boosted-type row present"

    # mem_class boost: semantic, procedural, and episodic/None (==0) all present.
    class_boosts = {round(r["_mem_class_boost"], 4) for r in seeded_results}
    assert MEM_CLASS_BOOST["semantic"] in class_boosts, "no semantic mem_class row scored"
    assert MEM_CLASS_BOOST["procedural"] in class_boosts, "no procedural mem_class row scored"
    assert 0.0 in class_boosts, "no episodic/None mem_class row scored"

    # Penalties: cross-project, supersession, staleness all exercised.
    assert any(r["_project_penalty"] < 0 for r in seeded_results), "no cross-project penalty exercised"
    assert any(r["_superseded_penalty"] < 0 for r in seeded_results), "no superseded penalty exercised"
    assert any(r["_staleness_penalty"] < 0 for r in seeded_results), "no staleness penalty exercised"

    # Reinforcement: at least one row with access_count > 0.
    assert any(r["_reinforcement"] > 0 for r in seeded_results), "no reinforcement exercised"

    # Encoding-context overlap: one row with context overlap and one without.
    assert any(r["_context_overlap"] > 0 for r in seeded_results), "no row with encoding_context overlap"
    assert any(r["_context_overlap"] == 0 for r in seeded_results), "no row without encoding_context"
