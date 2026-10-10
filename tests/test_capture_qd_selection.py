"""F-090: the Quick Desktop facts stage mirrors the current upstream selection.

A ``qd://`` parent the selection no longer contains is retired (status
``superseded`` with its active chunks, attributable via ``metadata.capture_retired``)
and is reactivated when it is selected again. Synthetic upstream rows only.
"""
from __future__ import annotations

import sqlite3

import pytest

QD_SCHEMA = """
CREATE TABLE nodes (id INTEGER PRIMARY KEY);
CREATE TABLE memories (id INTEGER PRIMARY KEY, node INTEGER, memory_type TEXT NOT NULL, category TEXT,
    name TEXT, source TEXT DEFAULT 'global', trigger_text TEXT NOT NULL, confidence REAL DEFAULT 0.5,
    effective_confidence REAL DEFAULT 0.5, retrieval_count INTEGER DEFAULT 0);
CREATE TABLE entities (id INTEGER PRIMARY KEY, node INTEGER, category TEXT NOT NULL, name TEXT NOT NULL, properties BLOB);
CREATE TABLE search_content (node INTEGER, text_content TEXT);
"""


class Upstream:
    """A synthetic knowledge_v1.db the test edits between sync runs."""

    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as conn:
            conn.executescript(QD_SCHEMA)

    def execute(self, sql, params=()):
        with sqlite3.connect(self.path) as conn:
            conn.execute(sql, params)

    def fact(self, qd_id, confidence=0.9, category="people", text=None):
        self.execute("""INSERT OR REPLACE INTO memories (id, memory_type, category, trigger_text, confidence,
            effective_confidence) VALUES (?, 'fact', ?, ?, ?, ?)""",
                     (qd_id, category, text or f"Synthetic fact number {qd_id}.", confidence, confidence))

    def decision(self, entity_id):
        self.execute("INSERT INTO entities (id, node, category, name) VALUES (?, NULL, 'Decision', ?)",
                     (entity_id, f"Synthetic decision {entity_id}"))


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    from src.capture import quick_desktop
    import src.ingest
    store = Upstream(tmp_path / "knowledge_v1.db")
    store.decision(99)  # a stable decision keeps qd://entity/ in scope
    monkeypatch.setattr(quick_desktop, "QD_DB_PATH", store.path)
    monkeypatch.setattr(quick_desktop, "QD_PROFILE_FALLBACK", None)
    monkeypatch.delenv("SECOND_BRAIN_QD_ALLOW_MASS_RETIRE", raising=False)
    monkeypatch.setattr(src.ingest, "generate_embedding", lambda text: [0.1] * 1024)
    monkeypatch.setattr(src.ingest, "_discover_relationships", lambda *a: None)
    return store


def _counts(selection):
    """The retire/reactivate counts plus any refusal or skip markers of one reconcile."""
    keys = ("retire", "reactivate", "refused_prefixes", "retirement_skipped", "dry_run", "profile_fallback")
    return {k: selection[k] for k in keys if k in selection}


def _retired(conn, url, source_type=None):
    """metadata.capture_retired of one parent (None when not retired)."""
    with conn.cursor() as cur:
        cur.execute("""SELECT metadata->'capture_retired' FROM memories WHERE source_url = %s AND parent_id IS NULL
                       AND (%s::text IS NULL OR source_type = %s)""", (url, source_type, source_type))
        return cur.fetchone()[0]


def _rows(conn, url):
    """(parent status, parent retired?, [(chunk status, chunk retired?)]) for one identity."""
    with conn.cursor() as cur:
        cur.execute("""SELECT id, status, metadata ? 'capture_retired' FROM memories
                       WHERE source_url = %s AND parent_id IS NULL ORDER BY created_at, id""", (url,))
        parents = cur.fetchall()
        assert len(parents) == 1, "one parent per identity"
        pid, status, retired = parents[0]
        cur.execute("""SELECT status, metadata ? 'capture_retired' FROM memories WHERE parent_id = %s
                       ORDER BY created_at, id""", (pid,))
        return status, retired, cur.fetchall()


def test_dry_run_counts_rows_outside_the_selection_and_writes_nothing(test_conn, upstream):
    from src.capture import quick_desktop
    for qd_id in (1, 2, 3):
        upstream.fact(qd_id)
    upstream.decision(10)
    quick_desktop.capture()
    upstream.fact(2, confidence=0.3)          # below QD's own threshold now
    upstream.execute("DELETE FROM memories WHERE id = 3")  # deleted upstream
    preview = quick_desktop.capture(dry_run=True)
    assert _counts(preview["selection"]) == {"retire": 2, "reactivate": 0, "dry_run": True}
    assert preview["selection"]["by_reason"] == {"deleted_upstream": 1, "below_threshold": 1, "relabelled": 0}
    assert preview["selection"]["guard"]["qd://memory/"] == {"candidates": 2, "active": 3, "cap": 25, "refuse": False}
    assert preview["quick_desktop_people"]["dry_run"] is True
    for qd_id in (1, 2, 3):
        status, retired, chunks = _rows(test_conn, f"qd://memory/{qd_id}")
        assert (status, retired) == ("active", False) and all(s == "active" for s, _ in chunks)


def test_rows_outside_the_selection_are_retired_with_their_chunks_and_restored_on_return(test_conn, upstream):
    from src.capture import quick_desktop
    upstream.fact(1)
    upstream.fact(2)
    quick_desktop.capture()
    upstream.fact(2, text="Synthetic fact number 2, edited upstream.")  # leaves one superseded history chunk
    quick_desktop.capture()
    assert [s for s, _ in _rows(test_conn, "qd://memory/2")[2]] == ["superseded", "active"]

    upstream.fact(2, confidence=0.3, text="Synthetic fact number 2, edited upstream.")
    result = quick_desktop.capture()
    assert _counts(result["selection"]) == {"retire": 1, "reactivate": 0}
    assert _rows(test_conn, "qd://memory/2") == ("superseded", True, [("superseded", False), ("superseded", True)])
    assert _rows(test_conn, "qd://memory/1")[:2] == ("active", False)
    assert _counts(quick_desktop.capture()["selection"]) == {"retire": 0, "reactivate": 0}, "idempotent"

    upstream.fact(2, confidence=0.8, text="Synthetic fact number 2, edited upstream.")
    result = quick_desktop.capture()
    assert _counts(result["selection"]) == {"retire": 0, "reactivate": 1}
    # Only what retirement touched comes back; the older history chunk stays history.
    assert _rows(test_conn, "qd://memory/2") == ("active", False, [("superseded", False), ("active", False)])


def test_a_record_that_returns_with_new_content_has_exactly_one_active_chunk_set(test_conn, upstream):
    from src.capture import quick_desktop
    upstream.fact(1)  # keeps qd://memory/ non-empty while fact 5 is out
    upstream.fact(5)
    quick_desktop.capture()
    upstream.fact(5, confidence=0.2)
    quick_desktop.capture()
    upstream.fact(5, confidence=0.9, text="Synthetic fact number 5, rewritten while it was out.")
    assert quick_desktop.capture()["selection"]["reactivate"] == 1
    status, retired, chunks = _rows(test_conn, "qd://memory/5")
    assert (status, retired) == ("active", False)
    assert [s for s, _ in chunks] == ["superseded", "active"]


def test_a_relabelled_record_retires_its_old_label_row(test_conn, upstream):
    from src.capture import quick_desktop
    upstream.fact(7, category="anti-pattern")
    quick_desktop.capture()
    upstream.fact(7, category="preference")
    quick_desktop.capture()
    with test_conn.cursor() as cur:
        cur.execute("""SELECT source_type, status FROM memories WHERE source_url = 'qd://memory/7'
                       AND parent_id IS NULL ORDER BY source_type""")
        assert cur.fetchall() == [("quick_desktop_anti_pattern", "superseded"), ("quick_desktop_preference", "active")]


def test_an_empty_namespace_is_refused_not_emptied(test_conn, upstream):
    from src.capture import quick_desktop
    upstream.fact(1)
    upstream.decision(10)
    quick_desktop.capture()
    # Decisions not read this run: qd://entity/ is out of scope, not absent.
    assert _counts(quick_desktop.capture(decisions=False)["selection"]) == {"retire": 0, "reactivate": 0}
    upstream.execute("DELETE FROM memories")
    result = quick_desktop.capture()
    assert _counts(result["selection"]) == {"retire": 0, "reactivate": 0, "refused_prefixes": ["qd://memory/"]}
    assert _rows(test_conn, "qd://memory/1")[0] == "active"
    assert _rows(test_conn, "qd://entity/10")[0] == "active"


def test_rows_another_writer_superseded_are_never_reactivated(test_conn, upstream):
    from src.capture import quick_desktop
    upstream.fact(1)
    quick_desktop.capture()
    with test_conn.cursor() as cur:
        cur.execute("UPDATE memories SET status = 'superseded' WHERE source_url = 'qd://memory/1' AND parent_id IS NULL")
    assert _counts(quick_desktop.capture()["selection"]) == {"retire": 0, "reactivate": 0}
    assert _rows(test_conn, "qd://memory/1")[:2] == ("superseded", False)


def test_other_channels_are_outside_the_reconciled_namespace(test_conn, upstream):
    from src import db
    from src.capture import quick_desktop
    import src.ingest
    src.ingest.ingest_content("Synthetic chat transcript.", "quick_desktop_chat", "qd-chat://s-1", parse_header=False)
    upstream.fact(1)
    quick_desktop.capture()
    with test_conn.cursor() as cur:
        cur.execute("SELECT status FROM memories WHERE source_url = 'qd-chat://s-1' AND parent_id IS NULL")
        assert cur.fetchone() == ("active",)
    with pytest.raises(ValueError):
        db.reconcile_source_selection((), {("quick_desktop_people", "qd://memory/1")})


# --- Guards and per-row reasons (adversarial review of F-090) -------------------


def test_each_retired_row_records_its_reason_and_confidence(test_conn, upstream):
    from src.capture import quick_desktop
    upstream.fact(1)
    upstream.fact(2)
    upstream.fact(3)
    upstream.fact(4, category="anti-pattern")
    quick_desktop.capture()
    upstream.execute("DELETE FROM memories WHERE id = 2")
    upstream.fact(3, confidence=0.3)
    upstream.fact(4, category="preference")
    result = quick_desktop.capture()
    assert result["selection"]["by_reason"] == {"deleted_upstream": 1, "below_threshold": 1, "relabelled": 1}
    deleted, below = _retired(test_conn, "qd://memory/2"), _retired(test_conn, "qd://memory/3")
    relabelled = _retired(test_conn, "qd://memory/4", "quick_desktop_anti_pattern")
    assert (deleted["reason"], deleted["confidence"]) == ("deleted_upstream", None)
    assert (below["reason"], below["confidence"]) == ("below_threshold", pytest.approx(0.3))
    assert (relabelled["reason"], relabelled["confidence"]) == ("relabelled", pytest.approx(0.9))
    assert all(r["at"] for r in (deleted, below, relabelled))
    assert _retired(test_conn, "qd://memory/1") is None


def test_hysteresis_holds_rows_inside_the_band_both_ways(test_conn, upstream):
    from src.capture import quick_desktop
    from src.capture.quick_desktop import DEFAULT_MIN_CONFIDENCE, RETIRE_HYSTERESIS, REACTIVATE_HYSTERESIS
    assert (DEFAULT_MIN_CONFIDENCE, RETIRE_HYSTERESIS, REACTIVATE_HYSTERESIS) == (0.5, 0.05, 0.05)
    upstream.fact(1)
    upstream.fact(2)
    quick_desktop.capture()
    upstream.fact(2, confidence=0.47)                      # out of the selection, inside the band
    result = quick_desktop.capture()
    assert result["selection"]["retire"] == 0 and result["selection"]["held"]["below_threshold_band"] == 1
    assert _rows(test_conn, "qd://memory/2")[0] == "active"
    upstream.fact(2, confidence=0.40)                      # below 0.45: retired
    assert quick_desktop.capture()["selection"]["retire"] == 1
    upstream.fact(2, confidence=0.52)                      # selected again, but below 0.55: held
    result = quick_desktop.capture()
    assert result["selection"]["reactivate"] == 0
    assert result["selection"]["held"]["retired_below_threshold_band"] == 1
    assert _rows(test_conn, "qd://memory/2")[:2] == ("superseded", True)
    assert all(s == "superseded" for s, _ in _rows(test_conn, "qd://memory/2")[2]), "not refreshed while held"
    upstream.fact(2, confidence=0.56)
    assert quick_desktop.capture()["selection"]["reactivate"] == 1
    assert _rows(test_conn, "qd://memory/2")[:2] == ("active", False)


def test_a_retired_row_superseded_by_another_memory_is_never_reactivated(test_conn, upstream):
    from src import db
    from src.capture import quick_desktop
    upstream.fact(1)
    upstream.fact(2)
    quick_desktop.capture()
    upstream.execute("DELETE FROM memories WHERE id = 2")
    assert quick_desktop.capture()["selection"]["retire"] == 1
    with test_conn.cursor() as cur:
        cur.execute("SELECT id::text FROM memories WHERE source_url = 'qd://memory/2' AND parent_id IS NULL")
        old = cur.fetchone()[0]
    new = db.create_memory(type="insight", title="Replacement", content="Synthetic replacement memory.")
    db.create_relationship(old, new, "superseded_by", "synthetic")
    upstream.fact(2)                                       # returns upstream
    result = quick_desktop.capture()
    assert result["selection"]["reactivate"] == 0 and result["selection"]["held"]["superseded_by"] == 1
    status, retired, chunks = _rows(test_conn, "qd://memory/2")
    assert (status, retired) == ("superseded", True) and all(s == "superseded" for s, _ in chunks)


def _many(upstream, n):
    for qd_id in range(1, n + 1):
        upstream.fact(qd_id)


def test_mass_retire_is_refused_but_the_refresh_still_runs(test_conn, upstream):
    from src.capture import quick_desktop
    _many(upstream, 30)
    quick_desktop.capture()
    upstream.execute("DELETE FROM memories WHERE id > 3")  # 27 > max(25, 10% of 30)
    upstream.fact(1, text="Synthetic fact number 1, edited upstream.")
    preview = quick_desktop.capture(dry_run=True)
    assert preview["selection"]["guard"]["qd://memory/"] == {"candidates": 27, "active": 30, "cap": 25, "refuse": True}
    assert preview["selection"]["by_reason"]["deleted_upstream"] == 27 and preview["selection"]["retire"] == 0
    result = quick_desktop.capture()
    assert result["selection"]["retire"] == 0 and result["selection"]["guard"]["qd://memory/"]["refuse"] is True
    assert result["quick_desktop_people"]["processed"] >= 1, "refresh still runs"
    assert [s for s, _ in _rows(test_conn, "qd://memory/1")[2]] == ["superseded", "active"]
    assert _rows(test_conn, "qd://memory/30")[0] == "active"


def test_at_the_cap_retirement_proceeds(test_conn, upstream):
    from src.capture import quick_desktop
    _many(upstream, 30)
    quick_desktop.capture()
    upstream.execute("DELETE FROM memories WHERE id > 5")  # exactly 25 = cap
    result = quick_desktop.capture()
    assert result["selection"]["retire"] == 25 and result["selection"]["guard"]["qd://memory/"]["refuse"] is False


@pytest.mark.parametrize("how", ["argument", "env"])
def test_operator_override_allows_a_mass_retire(test_conn, upstream, monkeypatch, how):
    from src.capture import quick_desktop
    _many(upstream, 30)
    quick_desktop.capture()
    upstream.execute("DELETE FROM memories WHERE id > 3")
    if how == "env":
        monkeypatch.setenv("SECOND_BRAIN_QD_ALLOW_MASS_RETIRE", "1")
        result = quick_desktop.capture()
    else:
        result = quick_desktop.capture(allow_mass_retire=True)
    guard = result["selection"]["guard"]["qd://memory/"]
    assert result["selection"]["retire"] == 27 and guard["refuse"] is False and guard["allowed_by_operator"] is True
    assert _rows(test_conn, "qd://memory/30")[0] == "superseded"


def test_qd_job_passes_the_override_to_the_facts_stage(monkeypatch):
    from src.capture import qd, quick_desktop, qd_eventlog, qd_chats, qd_tags
    seen = {}
    monkeypatch.setattr(quick_desktop, "capture", lambda **kw: seen.update(kw) or {})
    for module, name in ((qd_tags, "enrich_memories"), (qd_eventlog, "ingest_eventlog"), (qd_chats, "capture")):
        monkeypatch.setattr(module, name, lambda **kw: {})
    qd.capture(dry_run=True, allow_mass_retire=True)
    assert seen == {"dry_run": True, "allow_mass_retire": True}
    seen.clear()
    monkeypatch.setattr("sys.argv", ["qd", "--dry-run"])
    qd.main()
    assert seen == {"dry_run": True}, "unset defers to the environment"


def test_a_non_default_threshold_never_retires(test_conn, upstream):
    from src.capture import quick_desktop
    upstream.fact(1)
    upstream.fact(2)
    quick_desktop.capture()
    upstream.execute("DELETE FROM memories WHERE id = 2")
    for threshold in (0.3, 0.7):
        result = quick_desktop.capture(min_confidence=threshold)
        assert _counts(result["selection"])["retirement_skipped"] == "non_default_threshold"
        assert result["selection"]["retire"] == 0 and result["selection"]["by_reason"]["deleted_upstream"] == 1
    assert _rows(test_conn, "qd://memory/2")[0] == "active"


def test_the_legacy_profile_fallback_never_retires_or_reactivates(test_conn, upstream, monkeypatch, caplog):
    from src.capture import quick_desktop
    upstream.fact(1)
    upstream.fact(2)
    upstream.fact(3)
    quick_desktop.capture()
    upstream.execute("DELETE FROM memories WHERE id = 3")
    assert quick_desktop.capture()["selection"]["retire"] == 1
    upstream.fact(3)                                       # would return ...
    upstream.execute("DELETE FROM memories WHERE id = 2")  # ... and fact 2 would retire
    monkeypatch.setattr(quick_desktop, "QD_PROFILE_FALLBACK", "profiles_unreadable")
    with caplog.at_level("WARNING"):
        result = quick_desktop.capture()
    assert _counts(result["selection"]) == {"retire": 0, "reactivate": 0, "profile_fallback": "profiles_unreadable",
                                            "retirement_skipped": "legacy_profile_fallback"}
    assert result["selection"]["held"]["reactivation_skipped"] == 1
    assert "legacy fallback" in caplog.text
    assert _rows(test_conn, "qd://memory/2")[0] == "active"
    assert _rows(test_conn, "qd://memory/3")[:2] == ("superseded", True)


def test_resolve_profile_base_reports_the_legacy_fallback(tmp_path, monkeypatch):
    import json
    from src import qd_profile
    profiles = tmp_path / "profiles.json"
    monkeypatch.setattr(qd_profile, "QD_ROOT", str(tmp_path))
    monkeypatch.setattr(qd_profile, "_PROFILES_JSON", str(profiles))
    assert qd_profile.resolve_profile_base() == (str(tmp_path), "profiles_unreadable")
    profiles.write_text("{not json")
    assert qd_profile.resolve_profile_base() == (str(tmp_path), "profiles_unreadable")
    profiles.write_text(json.dumps({"last_active": "b", "entries": [{"id": "a", "data_path": "profiles/a"}]}))
    assert qd_profile.resolve_profile_base() == (str(tmp_path), "active_profile_not_found")
    profiles.write_text(json.dumps({"last_active": "a", "entries": [{"id": "a", "data_path": "profiles/a"}]}))
    assert qd_profile.resolve_profile_base() == (str(tmp_path / "profiles/a"), None)
    assert qd_profile.profile_base() == str(tmp_path / "profiles/a")
