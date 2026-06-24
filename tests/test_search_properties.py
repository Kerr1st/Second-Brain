"""Property tests for data-layer-decomposition correctness properties.

Validates structural correctness of the search module extraction from db.py.
"""

import inspect


class TestHybridSearchBehavioralEquivalence:
    """Property 1: hybrid_search behavioral equivalence after extraction.

    **Validates: Requirements 2.1, 2.3**
    """

    def test_hybrid_search_importable_from_search(self):
        """hybrid_search is importable from src.search with correct signature."""
        from src.search import hybrid_search

        sig = inspect.signature(hybrid_search)
        params = list(sig.parameters.keys())
        assert params == ["query_text", "query_embedding", "limit", "type", "status", "project", "source_type", "created_after"]

    def test_hybrid_search_signature_defaults(self):
        """hybrid_search has correct default values for optional params."""
        from src.search import hybrid_search

        sig = inspect.signature(hybrid_search)
        assert sig.parameters["limit"].default == 10
        assert sig.parameters["type"].default is None
        assert sig.parameters["status"].default is None
        assert sig.parameters["project"].default is None
        assert sig.parameters["source_type"].default is None
        assert sig.parameters["created_after"].default is None

    def test_hybrid_search_uses_get_connection_from_db(self):
        """hybrid_search module imports get_connection from src.db."""
        import src.search as mod

        assert hasattr(mod, "get_connection")
        from src.db import get_connection

        assert mod.get_connection is get_connection


import copy
import math
import re
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from hypothesis import given, settings, strategies as st


@st.composite
def result_dicts(draw):
    """Generate a list of result dicts suitable for rerank()."""
    n = draw(st.integers(min_value=1, max_value=10))
    results = []
    for _ in range(n):
        created = draw(st.one_of(
            st.none(),
            st.datetimes(
                min_value=datetime(2020, 1, 1),
                max_value=datetime(2026, 3, 17),
                timezones=st.just(timezone.utc),
            ),
        ))
        results.append({
            "rrf_score": draw(st.floats(min_value=0.0, max_value=1.0)),
            "content": draw(st.text(min_size=0, max_size=200)),
            "title": draw(st.text(min_size=0, max_size=50)),
            "created_at": created,
            "type": draw(st.sampled_from([
                "idea", "synthesis", "insight", "decision",
                "source", "research", "question",
            ])),
            "access_count": draw(st.one_of(
                st.none(),
                st.integers(min_value=0, max_value=1000),
            )),
        })
    return results


class TestRerankDeterministicScoring:
    """Property 2: rerank is a pure function with deterministic scoring.

    **Validates: Requirements 3.1, 3.2, 3.5**
    """

    @given(results=result_dicts(), query=st.text(min_size=1, max_size=100))
    @settings(max_examples=50)
    def test_rerank_returns_sorted_by_rerank_score(self, results, query):
        """rerank output is sorted descending by rerank_score for any inputs."""
        from src.search import rerank

        data = copy.deepcopy(results)
        output = rerank(data, query)
        scores = [r["rerank_score"] for r in output]
        for i in range(len(scores) - 1):
            assert scores[i] >= scores[i + 1], (
                f"Not sorted descending at index {i}: {scores[i]} < {scores[i + 1]}"
            )

    def test_rerank_empty_list_returns_empty(self):
        """rerank([]) returns []."""
        from src.search import rerank

        assert rerank([], "any query") == []

    @given(results=result_dicts(), query=st.text(min_size=1, max_size=100))
    @settings(max_examples=30)
    def test_rerank_makes_zero_db_calls(self, results, query):
        """rerank makes no database calls (get_connection never called)."""
        from src.search import rerank

        data = copy.deepcopy(results)
        with patch("src.search.get_connection") as mock_conn:
            rerank(data, query)
            mock_conn.assert_not_called()

    @given(results=result_dicts(), query=st.text(min_size=1, max_size=100))
    @settings(max_examples=50)
    def test_rerank_deterministic(self, results, query):
        """Calling rerank twice with same inputs produces same scores."""
        from src.search import rerank

        data1 = copy.deepcopy(results)
        data2 = copy.deepcopy(results)
        out1 = rerank(data1, query)
        out2 = rerank(data2, query)
        assert len(out1) == len(out2)
        for r1, r2 in zip(out1, out2):
            assert abs(r1["rerank_score"] - r2["rerank_score"]) < 1e-9, (
                f"Non-deterministic: {r1['rerank_score']} != {r2['rerank_score']}"
            )

    @given(results=result_dicts(), query=st.text(min_size=1, max_size=100))
    @settings(max_examples=50)
    def test_rerank_adds_rerank_score_field(self, results, query):
        """Each result dict gets a rerank_score field added after rerank."""
        from src.search import rerank

        data = copy.deepcopy(results)
        output = rerank(data, query)
        for r in output:
            assert "rerank_score" in r, "rerank_score field missing from result"
            assert isinstance(r["rerank_score"], float)


class TestIncrementAccessCountNoOp:
    """Property 3: increment_access_count no-op on empty list.

    **Validates: Requirements 4.1, 4.2**
    """

    def test_empty_list_makes_zero_db_connections(self):
        """Calling increment_access_count([]) makes zero database connections."""
        from src.search import increment_access_count

        with patch("src.search.get_connection") as mock_conn:
            increment_access_count([])
            mock_conn.assert_not_called()


class TestDbPublicInterface:
    """Property 4: db.py retains correct public interface after extraction.

    **Validates: Requirements 1.2, 1.4, 5.1, 5.2, 5.3**
    """

    EXPECTED_PUBLIC = {
        "get_connection", "is_reachable",
        "create_memory", "get_memory", "update_memory", "list_memories",
        "search_similar",
        "create_relationship", "get_relationships",
        "get_processed_source_urls",
        "ALLOWED_UPDATE_FIELDS", "DB_CONFIG",
    }

    EXTRACTED = {"hybrid_search", "rerank", "increment_access_count"}

    def test_db_exports_expected_public_names(self):
        """src.db exports all expected public functions and constants."""
        import src.db as db
        for name in self.EXPECTED_PUBLIC:
            assert hasattr(db, name), f"src.db missing expected export: {name}"

    def test_db_does_not_export_extracted_functions(self):
        """src.db no longer exports hybrid_search, rerank, or increment_access_count."""
        import src.db as db
        for name in self.EXTRACTED:
            assert not hasattr(db, name), f"src.db still exports extracted function: {name}"


import pathlib


class TestExistingPatchesValid:
    """Property 5: All existing test patches remain valid after extraction.

    **Validates: Requirements 10.1, 10.2**
    """

    def test_no_test_patches_target_db_extracted_functions(self):
        """No test files patch src.db.hybrid_search, src.db.rerank, or src.db.increment_access_count."""
        test_dir = pathlib.Path("tests")
        # Build forbidden targets dynamically to avoid self-matching
        extracted_fns = ["hybrid_search", "rerank", "increment_access_count"]
        prefix = "src.db."
        forbidden_targets = []
        for fn in extracted_fns:
            target = prefix + fn
            forbidden_targets.append(f'patch("{target}"')
            forbidden_targets.append(f"patch('{target}'")
        violations = []
        for test_file in test_dir.glob("*.py"):
            if test_file.name == "test_search_properties.py":
                continue  # skip self — this file contains the target strings
            content = test_file.read_text()
            for target in forbidden_targets:
                if target in content:
                    violations.append(f"{test_file.name}: {target}")
        assert not violations, f"Found forbidden patch targets: {violations}"

    def test_dream_cycle_storage_search_similar_resolves(self):
        """src.dream_cycle.storage.search_similar patch target still resolves."""
        from src.dream_cycle import storage
        assert hasattr(storage, "search_similar"), "search_similar not found in storage module"
        assert callable(storage.search_similar)


class TestImportTopology:
    """Property 6: Import topology is correct after refactor.

    **Validates: Requirements 6.1, 6.2, 6.3, 7.1, 8.1**
    """

    def test_mcp_server_imports_search_from_search_module(self):
        """mcp_server.py imports hybrid_search, rerank, increment_access_count from src.search."""
        source = pathlib.Path("src/mcp_server.py").read_text()
        assert "from src.search import" in source, (
            "mcp_server.py should import from src.search"
        )
        # Verify all three search functions appear in a src.search import
        for fn in ("hybrid_search", "rerank", "increment_access_count"):
            # The function name must appear after 'from src.search import'
            assert fn in source, (
                f"mcp_server.py should import {fn} from src.search"
            )

    def test_mcp_server_does_not_import_search_similar(self):
        """mcp_server.py does NOT import search_similar (unused import removed)."""
        source = pathlib.Path("src/mcp_server.py").read_text()
        assert "search_similar" not in source, (
            "mcp_server.py should not import search_similar — it was unused"
        )

    def test_golden_queries_imports_from_search_module(self):
        """golden_queries.py imports hybrid_search, rerank from src.search."""
        source = pathlib.Path("scripts/eval/golden_queries.py").read_text()
        assert "from src.search import" in source, (
            "golden_queries.py should import from src.search"
        )
        for fn in ("hybrid_search", "rerank"):
            assert fn in source, (
                f"golden_queries.py should import {fn} from src.search"
            )

    def test_dream_cycle_storage_imports_unchanged(self):
        """dream_cycle/storage.py imports from src.db (not src.search)."""
        source = pathlib.Path("src/dream_cycle/storage.py").read_text()
        assert "from src.db import" in source, (
            "storage.py should import from src.db"
        )
        assert "from src.search" not in source, (
            "storage.py should NOT import from src.search — search_similar stays in db.py"
        )
