"""Thinker short memory refs (triage 2026-10-15, issue I3).

On 2026-10-16 the Thinker (Opus 5) cited 1264ac24-b8f6-472c-b5bd-107b835b5e9c — a
corrupted copy of the in-slice 1264ac24-b8f6-472c-b5bd-107b793d836a — and two
otherwise-valid candidates were blocked before judging. Each packet entry now
carries a short ref ("m1".."mN"); the Thinker cites refs and the parser maps them
back to UUIDs. The authorization gate is unchanged: anything that is not an
in-packet ref or an in-packet UUID passes through and is still BLOCKED.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from unittest.mock import MagicMock, patch

import pytest

from src.dream_cycle.packet import assign_refs, build_packet, resolve_candidate_refs, resolve_memory_ref
from src.dream_cycle.storage import CandidateAuthorizationError, authorize_candidate, canonical_uuid
from src.models import CandidateInsight, EvaluatorVerdict, MemorySlice
from src.prompts import get_thinker_prompt

REAL_1016 = "1264ac24-b8f6-472c-b5bd-107b793d836a"
CORRUPT_1016 = "1264ac24-b8f6-472c-b5bd-107b835b5e9c"
IDS = [
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    REAL_1016,
    "44444444-4444-4444-8444-444444444444",
]


def _mem(mid, title="T", **kw):
    return {"id": mid, "title": title, "type": "notes", "content": f"body of {title}",
            "created_at": "2026-10-15T09:00:00+00:00", "tags": [], "project": None, "status": "active", **kw}


def _orch():
    from src.backends.resolver import Resolver, RoleBackend
    from src.dream_cycle import DreamCycleOrchestrator
    orch = DreamCycleOrchestrator()
    orch.resolver = Resolver({
        "explorer": RoleBackend("explorer", "claude_code", "global.anthropic.claude-opus-5", "high"),
        **{role: RoleBackend(role, "bedrock", "global.anthropic.claude-opus-5", "high")
           for role in ("thinker", "skeptic", "advocate", "epistemologist", "methodologist")},
    })
    return orch


def _run_thinker(orch, memories, output, slice_ids=None):
    """Invoke the real invoke_thinker with a stub model; returns (candidates, payload)."""
    captured = {}

    def invoke(system_prompt, user_message, tools=False, **kwargs):
        captured.update(system_prompt=system_prompt, user_message=user_message)
        return {"output": output, "raw": json.dumps(output), "usage": {"inputTokens": 0, "outputTokens": 0}}

    ids = slice_ids or [str(m["id"]) for m in memories]
    slice_ = MemorySlice(name="Recurring accuracy corrections", strategy="pattern_emergence",
                         memory_ids=ids, memory_titles=[m["title"] for m in memories])
    with patch("src.dream_cycle.agents.thinker.load_memories_by_ids", return_value=memories), \
         patch.object(orch, "_capture"):
        orch._invoker_for = lambda role: MagicMock(invoke=invoke)
        candidates = orch.invoke_thinker(slice_)
    return candidates, json.loads(captured["user_message"]), captured["system_prompt"], slice_


def _authorized(orch, slice_):
    """The authorized context exactly as orchestrator.run builds it (orchestrator.py:371-378)."""
    out = {}
    for mid in slice_.memory_ids:
        key = canonical_uuid(mid)
        if key is not None and key in orch._slice_memory_cache:
            out[key] = orch._slice_memory_cache[key]
    return out


def _raw(**kw):
    base = {"title": "Corrections recur [m3]", "type": "insight", "operation": "CREATE",
            "content": "WHAT: x. EVIDENCE: [m1] says a; [m3, m2] say b. claim_scope: m3 only.",
            "schema_operation": "assimilation", "confidence": "medium",
            "source_memories": ["m1", "m3"],
            "relationships": [{"target_id": "m2", "relation_type": "supports", "note": "see [m2]"}]}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- packet
class TestPacketRefs:
    def test_entries_get_sequential_refs_and_map(self):
        entries, ref_map = assign_refs(build_packet([_mem(i) for i in IDS]))
        assert [e["ref"] for e in entries] == ["m1", "m2", "m3", "m4"]
        assert list(entries[0])[0] == "ref"  # first key: the model sees it before the id
        assert [e["id"] for e in entries] == IDS  # full id kept (tool calls, backward compat)
        assert ref_map == {"m1": IDS[0], "m2": IDS[1], "m3": IDS[2], "m4": IDS[3]}

    def test_thinker_payload_carries_refs(self):
        orch = _orch()
        _, payload, prompt, _ = _run_thinker(orch, [_mem(i) for i in IDS], [])
        assert [m["ref"] for m in payload["memory_slice"]["memories"]] == ["m1", "m2", "m3", "m4"]
        assert [m["id"] for m in payload["memory_slice"]["memories"]] == IDS
        assert "## Citing memories by ref" in prompt and "Cite memories by ref" in prompt

    def test_refs_follow_the_active_packet_not_the_slice(self):
        """Non-active memories are excluded from the packet, so refs number the packet entries."""
        orch = _orch()
        mems = [_mem(IDS[0]), _mem(IDS[1], status="superseded"), _mem(IDS[2])]
        cands, payload, _, _ = _run_thinker(orch, mems, [_raw(source_memories=["m2"], relationships=[])])
        assert [(m["ref"], m["id"]) for m in payload["memory_slice"]["memories"]] == [("m1", IDS[0]), ("m2", IDS[2])]
        assert cands[0].source_memories == [IDS[2]]


# --------------------------------------------------------------------------- parsing
class TestResolve:
    REFS = {"m1": IDS[0], "m2": IDS[1], "m3": IDS[2], "m4": IDS[3]}

    def test_m3_maps_in_every_id_field(self):
        out = resolve_candidate_refs(_raw(operation="UPDATE", target_memory_id="m3",
                                          source_memories=["m3"],
                                          relationships=[{"target_id": "m3", "relation_type": "extends"}]),
                                     self.REFS)
        assert out["source_memories"] == [REAL_1016]
        assert out["target_memory_id"] == REAL_1016
        assert out["relationships"][0]["target_id"] == REAL_1016

    def test_prose_refs_are_replaced_and_no_ref_survives(self):
        out = resolve_candidate_refs(_raw(schema_note="extends [m4]", confidence_reasoning="[m1]",
                                          supersedes_reason="[m2]"), self.REFS)
        assert out["title"] == f"Corrections recur [{REAL_1016}]"
        assert f"[{IDS[0]}] says a; [{REAL_1016}, {IDS[1]}] say b" in out["content"]
        assert out["content"].endswith("claim_scope: m3 only.")  # unbracketed prose is never rewritten
        assert out["relationships"][0]["note"] == f"see [{IDS[1]}]"
        assert out["schema_note"] == f"extends [{IDS[3]}]"
        assert (out["confidence_reasoning"], out["supersedes_reason"]) == (f"[{IDS[0]}]", f"[{IDS[1]}]")

    def test_full_in_packet_uuid_still_accepted(self):
        assert resolve_memory_ref(REAL_1016, self.REFS) == REAL_1016
        assert resolve_memory_ref(REAL_1016.upper(), self.REFS) == REAL_1016

    @pytest.mark.parametrize("value", ["m99", "m0", "m", "mm3", "x3", CORRUPT_1016, REAL_1016[:-1],
                                       "55555555-5555-4555-8555-555555555555", "", None, 3])
    def test_anything_else_passes_through_unchanged(self, value):
        assert resolve_memory_ref(value, self.REFS) == value

    def test_tolerated_ref_spellings(self):
        assert [resolve_memory_ref(v, self.REFS) for v in ("M3", " m3 ", "[m3]")] == [REAL_1016] * 3

    def test_unexpected_shapes_left_for_parser_and_gate(self):
        assert resolve_candidate_refs("not a dict", self.REFS) == "not a dict"
        out = resolve_candidate_refs({"source_memories": "m1", "relationships": ["m2"]}, self.REFS)
        assert out == {"source_memories": "m1", "relationships": ["m2"]}

    def test_input_is_not_mutated(self):
        raw = _raw()
        snapshot = json.dumps(raw)
        resolve_candidate_refs(raw, self.REFS)
        assert json.dumps(raw) == snapshot


# --------------------------------------------------------------------------- end-to-end through invoke_thinker
class TestInvokeThinkerMapping:
    def test_parsed_candidate_holds_uuids_in_all_id_fields(self):
        orch = _orch()
        mems = [_mem(i) for i in IDS]
        mems[3] |= {"type": "insight", "metadata": {"dream_cycle": True}}
        cands, payload, _, _ = _run_thinker(orch, mems, [_raw(
            operation="UPDATE", target_memory_id="m4", source_memories=["m3", IDS[0]],
            relationships=[{"target_id": "m2", "relation_type": "supports", "note": "[m2]"}])])
        assert payload["memory_slice"]["memories"][3]["may_update"] is True
        [c] = cands
        assert c.source_memories == [REAL_1016, IDS[0]]
        assert c.target_memory_id == IDS[3]
        assert c.relationships == [{"target_id": IDS[1], "relation_type": "supports", "note": f"[{IDS[1]}]"}]
        stored = json.dumps(asdict(c))  # what store_candidate / the digest serialise
        assert '"m1"' not in stored and '"m2"' not in stored and "[m" not in stored

    @pytest.mark.parametrize("bad, field, msg", [
        ("m99", "source", "must be valid memory UUIDs"),
        (CORRUPT_1016, "source", "outside its supplied context"),
        ("m99", "relationship", "must be valid memory UUIDs"),
        (CORRUPT_1016, "relationship", "Relationship target was not in the supplied context"),
        ("m99", "target", "must be valid memory UUIDs"),
        (CORRUPT_1016, "target", "Mutation target was not in the supplied context"),
    ])
    def test_unknown_ref_and_corrupted_uuid_still_blocked_by_real_gate(self, bad, field, msg):
        orch = _orch()
        raw = _raw(relationships=[])
        if field == "source":
            raw["source_memories"] = ["m1", bad]
        elif field == "relationship":
            raw["relationships"] = [{"target_id": bad, "relation_type": "supports"}]
        else:
            raw.update(operation="UPDATE", target_memory_id=bad)
        cands, _, _, slice_ = _run_thinker(orch, [_mem(i) for i in IDS], [raw])
        [c] = cands
        assert bad in (c.source_memories + [r["target_id"] for r in c.relationships] + [c.target_memory_id])
        with pytest.raises(CandidateAuthorizationError, match=msg):
            authorize_candidate(c, _authorized(orch, slice_))

    def test_ref_from_another_packet_cannot_leak(self):
        """Refs are per packet: m4 is unknown to a 3-memory slice, even if another slice had one."""
        orch = _orch()
        _run_thinker(orch, [_mem(i) for i in IDS], [])
        cands, _, _, slice_ = _run_thinker(orch, [_mem(i) for i in IDS[:3]], [_raw(source_memories=["m4"], relationships=[])])
        assert cands[0].source_memories == ["m4"]
        with pytest.raises(CandidateAuthorizationError, match="must be valid memory UUIDs"):
            authorize_candidate(cands[0], _authorized(orch, slice_))


# --------------------------------------------------------------------------- revision path
class TestRevisionPath:
    """The revision turn (stages/grounding._revise_candidate) never sees the packet or refs:
    it receives the already-mapped candidate (UUIDs) plus format_sources_for_evaluator
    text keyed by UUID, and may only shrink sources / keep original relationship targets."""

    def test_revision_sees_uuids_only_and_keeps_mapped_ids(self):
        orch = _orch()
        cands, _, _, _ = _run_thinker(orch, [_mem(i) for i in IDS], [_raw()])
        [c] = cands
        assert c.source_memories == [IDS[0], REAL_1016]
        captured = {}

        def revise(system_prompt, user_message, tools=False, **kwargs):
            captured["user_message"] = user_message
            # The model echoes one UUID, corrupts another, and invents a ref it never saw.
            return {"output": {"title": "t", "content": "narrower", "confidence": "low",
                               "source_memories": [REAL_1016, CORRUPT_1016, "m1"],
                               "relationships": [{"target_id": IDS[1], "relation_type": "supports", "note": "n"},
                                                 {"target_id": "m2", "relation_type": "supports"}]},
                    "raw": "{}", "usage": {"inputTokens": 0, "outputTokens": 0}}

        orch._invoker_for = lambda role: MagicMock(invoke=revise)
        audit = EvaluatorVerdict(role="skeptic", verdict="REJECT", score=50, blocking_issues=["x"])
        with patch.object(orch, "_capture"):
            revised = orch._revise_candidate(c, audit)
        request = json.loads(captured["user_message"])
        assert request["candidate"]["source_memories"] == [IDS[0], REAL_1016]
        assert request["candidate"]["relationships"][0]["target_id"] == IDS[1]
        assert f"[{REAL_1016}]" in request["cited_sources"]
        assert '"m1"' not in captured["user_message"] and '"m3"' not in captured["user_message"]
        # Only the original UUIDs survive; the corrupted UUID and the stray ref are dropped.
        assert revised.source_memories == [REAL_1016]
        assert revised.relationships == [{"target_id": IDS[1], "relation_type": "supports", "note": "n"}]


# --------------------------------------------------------------------------- prompt
class TestPrompt:
    def test_prompt_tells_thinker_to_cite_refs_in_every_id_field(self):
        p = get_thinker_prompt()
        section = p[p.index("## Citing memories by ref"):p.index("## Depth framework")]
        for field in ("source_memories", "target_memory_id", "relationships[].target_id"):
            assert field in section
        assert "[m3]" in section
        assert '"source_memories": ["m1", "m3"]' in p
        assert '"target_id": "m2"' in p
        assert '"target_memory_id": "ref' in p
        assert '"uuid' not in p


# --------------------------------------------------------------------------- 10-16 replay (real gate, disposable DB)
@pytest.fixture
def slice3(test_schema):
    """Slice-3 shape from 2026-10-16: the correction_episode 1264ac24-…-107b793d836a plus valid peers."""
    from src.db import create_memory, get_connection, get_memory
    peers = [create_memory("research", f"Draft correction {i}", f"Correction body {i}") for i in range(3)]
    tmp = create_memory("correction_episode", "Correction episode 10-15", "Overstatement corrected")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM memories WHERE id = %s", (REAL_1016,))
        cur.execute("UPDATE memories SET id = %s WHERE id = %s", (REAL_1016, tmp))
        conn.commit()
    rows = [dict(get_memory(m)) for m in (peers[0], peers[1], REAL_1016, peers[2])]
    yield rows
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM memories WHERE id = ANY(%s::uuid[])", ([str(r["id"]) for r in rows],))
        conn.commit()


class TestReplay1016:
    def _outputs(self, rows, cite):
        ids = [str(r["id"]) for r in rows]
        return [
            {"title": 'The "source tied to every claim" standard', "type": "insight", "operation": "CREATE",
             "content": "WHAT/EVIDENCE/WHY. claim_scope: two corrections.", "schema_operation": "assimilation",
             "source_memories": [ids[0], cite, ids[1]], "relationships": []},
            {"title": "Not all the draft corrections were overstatement", "type": "insight", "operation": "CREATE",
             "content": "WHAT/EVIDENCE/WHY. claim_scope: three drafts.", "schema_operation": "accommodation",
             "source_memories": [ids[0], ids[3]],
             "relationships": [{"target_id": cite, "relation_type": "contradicts", "note": "n"}]},
        ]

    def test_corrupted_uuid_is_still_blocked(self, slice3):
        orch = _orch()
        cands, _, _, slice_ = _run_thinker(orch, slice3, self._outputs(slice3, CORRUPT_1016))
        msgs = []
        for c in cands:
            with pytest.raises(CandidateAuthorizationError) as exc:
                authorize_candidate(c, _authorized(orch, slice_))
            msgs.append(str(exc.value))
        assert msgs == ["Candidate cites a source outside its supplied context",
                        "Relationship target was not in the supplied context"]

    def test_citing_the_ref_instead_authorizes(self, slice3):
        orch = _orch()
        cands, payload, _, slice_ = _run_thinker(orch, slice3, self._outputs(slice3, "m3"))
        assert (payload["memory_slice"]["memories"][2]["ref"], payload["memory_slice"]["memories"][2]["id"]) == ("m3", REAL_1016)
        authorized = _authorized(orch, slice_)
        for c in cands:
            ids = authorize_candidate(c, authorized)
            assert REAL_1016 in ids and ids <= set(authorized)
        assert REAL_1016 in cands[0].source_memories
        assert cands[1].relationships[0]["target_id"] == REAL_1016
