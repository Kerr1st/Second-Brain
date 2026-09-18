"""Offline panel replay — re-evaluate stored candidates with variant prompts, models or seats.

The consensus panel has accepted almost nothing (Astra seats: 1 ACCEPT in 70
votes). Experiments on the panel must not require a full dream-cycle run
(Explorer + Thinker ≈ 20 min and ≈ $2 before a single verdict). This harness
takes candidates already in ``dream_cycle_candidates``, rebuilds exactly what the
live panel saw (candidate JSON + the cited sources' text), and re-runs chosen
seats with optional overrides. Results go to ``logs/panel_replay/<stamp>.jsonl``
and an acceptance matrix is printed. It never writes to the candidates table.

    AWS_PROFILE=second-brain-jobs .venv/bin/python scripts/panel_replay.py \\
        --run 47fffa94-2b9d-4958-ae17-07912a1cacfc --limit 6 --cap-usd 2 \\
        [--seats skeptic,epistemologist] \\
        [--model-override skeptic=bedrock:global.anthropic.claude-sonnet-5] \\
        [--prompt-variant current|standard] [--effort medium] \\
        [--candidate-file rewritten.json]   # {candidate_id: {title, content}} overrides (scope tests)
        [--dry-run]

See docs/PLAN-panel-acceptance.md (Phase 0/1).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backends.cost import SpendCapExceeded, SpendTracker  # noqa: E402
from src.backends.base import BACKEND_CAPABILITIES  # noqa: E402
from src.backends.resolver import DEFAULT_ADAPTERS, RoleBackend, default_resolver  # noqa: E402
from src.dream_cycle.consensus import tally_consensus  # noqa: E402
from src.dream_cycle.packet import (  # noqa: E402
    EVALUATOR_VERDICT_SCHEMA,
    EVALUATOR_VERDICT_SCHEMA_V2,
    format_sources_for_evaluator,
    load_memories_by_ids,
)
from src.models import CandidateInsight, EvaluatorVerdict  # noqa: E402
from src.prompts import get_evaluator_prompt  # noqa: E402

log = logging.getLogger("panel_replay")

SEATS = ("skeptic", "advocate", "epistemologist", "methodologist")
SEAT_COLUMNS = {"skeptic": "a", "advocate": "b", "epistemologist": "c", "methodologist": "d"}
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "logs", "panel_replay")


# --------------------------------------------------------------------------- #
# Data access (all injectable)                                                 #
# --------------------------------------------------------------------------- #
def fetch_candidates(run_id: str | None, candidate_ids: list[str] | None, limit: int | None) -> list[dict]:
    """Rows: id, run_id, candidate_json (dict), live verdicts a-d."""
    from psycopg2.extras import RealDictCursor
    from src.db import get_connection
    where, params = [], []
    if run_id:
        where.append("run_id = %s"); params.append(run_id)
    if candidate_ids:
        where.append("id = ANY(%s::uuid[])"); params.append(list(candidate_ids))
    if not where:
        raise SystemExit("need --run or --candidate")
    sql = ("SELECT id, run_id, candidate_json, evaluator_a_verdict, evaluator_b_verdict, "
           "evaluator_c_verdict, evaluator_d_verdict, final_verdict FROM dream_cycle_candidates "
           f"WHERE {' AND '.join(where)} ORDER BY created_at")
    if limit:
        sql += " LIMIT %s"; params.append(limit)
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if isinstance(r["candidate_json"], str):
            r["candidate_json"] = json.loads(r["candidate_json"])
        r["id"] = str(r["id"])
    return rows


def candidate_from_row(row: dict, overrides: dict | None = None) -> CandidateInsight:
    cj = dict(row["candidate_json"])
    if overrides:
        cj.update({k: v for k, v in overrides.items() if k in ("title", "content", "confidence", "confidence_reasoning")})
    fields = {f for f in CandidateInsight.__dataclass_fields__}
    return CandidateInsight(**{k: v for k, v in cj.items() if k in fields})


def sources_text_for(candidate: CandidateInsight, loader: Callable = load_memories_by_ids) -> str:
    """Reproduce the live panel's source rendering (cache of cited memories)."""
    cache = {str(m["id"]): m for m in loader(candidate.source_memories)}
    text = f"Source memories: {candidate.source_memories}"
    rendered = format_sources_for_evaluator(cache, candidate.source_memories)
    return text + ("\n" + rendered if rendered else "")


# --------------------------------------------------------------------------- #
# Replay                                                                       #
# --------------------------------------------------------------------------- #
def schema_for(variant: str) -> dict:
    return EVALUATOR_VERDICT_SCHEMA_V2 if variant == "standard" else EVALUATOR_VERDICT_SCHEMA


def _require_direct_api(seat: str, spec: RoleBackend) -> None:
    """Replay only runs on Direct-API backends: they accept the verdict schema, and
    they honour the requested model. An agentic CLI seat (Kiro) would silently route
    the corpus to whatever the server picks — on 2026-09-17 that was Fable."""
    caps = BACKEND_CAPABILITIES.get(spec.backend)
    if caps is None or caps.supports_mcp:
        raise SystemExit(
            f"seat {seat!r} resolves to backend {spec.backend!r} ({spec.model}), which is not a Direct-API "
            f"backend. Use --profile controlled or --model-override {seat}=bedrock:<model>.")


def parse_overrides(specs: list[str]) -> dict[str, RoleBackend]:
    """--model-override skeptic=bedrock:global.anthropic.claude-sonnet-5[:effort]"""
    out = {}
    for spec in specs or []:
        role, _, rest = spec.partition("=")
        parts = rest.split(":")
        if role not in SEATS or len(parts) < 2:
            raise SystemExit(f"bad --model-override {spec!r}; expected role=backend:model[:effort]")
        out[role] = RoleBackend(role=role, backend=parts[0], model=parts[1], effort=parts[2] if len(parts) > 2 else "high")
    return out


def replay(rows: list[dict], seats: list[str], resolver, tracker: SpendTracker, *,
           prompt_fn: Callable = get_evaluator_prompt, sources_fn: Callable = sources_text_for,
           overrides: dict[str, RoleBackend] | None = None, effort: str | None = None,
           candidate_overrides: dict[str, dict] | None = None, variant: str = "current",
           dry_run: bool = False, sink: Callable[[dict], None] | None = None) -> list[dict]:
    """Run the chosen seats over the candidates. Returns one record per (candidate, seat)."""
    overrides = overrides or {}
    records: list[dict] = []
    for row in rows:
        cand = candidate_from_row(row, (candidate_overrides or {}).get(row["id"]))
        candidate_json = json.dumps(asdict(cand), default=str)
        sources = sources_fn(cand)
        for seat in seats:
            spec = overrides.get(seat) or resolver.spec_for(seat)
            _require_direct_api(seat, spec)
            eff = effort or spec.effort
            prompt = prompt_fn(role=seat, candidate_json=candidate_json, source_memories_content=sources)
            rec = {"candidate_id": row["id"], "run_id": str(row["run_id"]), "title": cand.title[:120],
                   "seat": seat, "backend": spec.backend, "model": spec.model, "effort": eff,
                   "variant": variant, "live_verdict": row.get(f"evaluator_{SEAT_COLUMNS[seat]}_verdict"),
                   "prompt_chars": len(prompt)}
            if dry_run:
                rec.update({"verdict": None, "reasoning": "", "cost_usd": 0.0})
                records.append(rec); (sink or (lambda r: None))(rec)
                continue
            invoker = (resolver.invoker_for(seat) if seat not in overrides
                       else DEFAULT_ADAPTERS[spec.backend](model=spec.model))
            t0 = time.time()
            try:
                result = invoker.invoke(
                    system_prompt=prompt,
                    user_message=(f"Please evaluate this candidate insight as the {seat}. "
                                  "Return your verdict as JSON with 'verdict' and 'reasoning' fields."),
                    effort=eff, stage=f"replay:{seat}", schema=schema_for(variant))
            except Exception as e:  # noqa: BLE001 — one bad call must not sink the experiment
                rec.update({"verdict": "ERROR", "reasoning": f"{type(e).__name__}: {e}"[:400], "cost_usd": 0.0,
                            "latency_s": round(time.time() - t0, 1)})
                records.append(rec); (sink or (lambda r: None))(rec)
                continue
            output = result.get("output") or {}
            if isinstance(output, list):
                output = output[0] if output else {}
            cost = tracker.record(spec.model, result.get("usage")) if isinstance(result.get("usage"), dict) else 0.0
            rec.update({"verdict": output.get("verdict", "REJECT"), "reasoning": (output.get("reasoning") or "")[:4000],
                        "score": output.get("score"), "blocking_issues": output.get("blocking_issues"),
                        "advisory_issues": output.get("advisory_issues"),
                        "cost_usd": round(cost, 4), "latency_s": round(time.time() - t0, 1)})
            records.append(rec); (sink or (lambda r: None))(rec)
    return records


def replay_grounded(rows: list[dict], seats: list[str], orch, *, loader: Callable = load_memories_by_ids,
                    sink: Callable[[dict], None] | None = None) -> list[dict]:
    """Run candidates through the real pre-panel path — grounding audit + one revision
    (orchestrator._ground_candidate) — then the chosen seats (orchestrator.invoke_evaluator).

    Uses the orchestrator's own spend tracker; set ``orch._spend`` to a capped
    SpendTracker before calling. Records carry the revised title and the audit's
    findings so acceptance can be compared with the un-grounded replay.
    """
    records: list[dict] = []
    emit = sink or (lambda r: None)
    for row in rows:
        original = candidate_from_row(row)
        for m in loader(original.source_memories):
            orch._slice_memory_cache[str(m["id"])] = m
        cand = orch._ground_candidate(original)
        revision = cand.revision or {}
        base = {"candidate_id": row["id"], "run_id": str(row["run_id"]), "title": cand.title[:120],
                "original_title": original.title[:120], "revised": cand is not original,
                "audit_score": revision.get("audit_score"),
                "audit_blocking": len(revision.get("blocking_issues") or []),
                "audit_advisory": len(revision.get("advisory_issues") or []), "variant": "grounded"}
        if not cand.content.strip():
            for seat in seats:
                rec = {**base, "seat": seat, "verdict": "REJECT", "reasoning": "nothing supportable after revision",
                       "score": 0, "cost_usd": 0.0, "live_verdict": row.get(f"evaluator_{SEAT_COLUMNS[seat]}_verdict")}
                records.append(rec); emit(rec)
            continue
        for seat in seats:
            spec = orch.resolver.spec_for(seat)
            before = orch._spend.total_usd
            t0 = time.time()
            try:
                v = orch.invoke_evaluator(cand, seat)
                rec = {**base, "seat": seat, "backend": spec.backend, "model": spec.model, "verdict": v.verdict,
                       "score": v.score, "blocking_issues": v.blocking_issues, "advisory_issues": v.advisory_issues,
                       "reasoning": v.reasoning[:4000]}
            except SpendCapExceeded:
                raise
            except Exception as e:  # noqa: BLE001
                rec = {**base, "seat": seat, "backend": spec.backend, "model": spec.model, "verdict": "ERROR",
                       "reasoning": f"{type(e).__name__}: {e}"[:400]}
            rec.update({"cost_usd": round(orch._spend.total_usd - before, 4), "latency_s": round(time.time() - t0, 1),
                        "live_verdict": row.get(f"evaluator_{SEAT_COLUMNS[seat]}_verdict")})
            records.append(rec); emit(rec)
    return records


def matrix(records: list[dict], seats: list[str]) -> dict:
    """Acceptance per seat, plus the 3-of-4 outcome when all four seats were replayed."""
    by_cand: dict[str, dict[str, str]] = {}
    for r in records:
        by_cand.setdefault(r["candidate_id"], {})[r["seat"]] = r["verdict"]
    per_seat = {s: Counter(v.get(s) for v in by_cand.values()) for s in seats}
    outcomes = Counter()
    if set(seats) >= set(SEATS):
        for cid, votes in by_cand.items():
            if all(votes.get(s) in ("ACCEPT", "REJECT") for s in SEATS):
                outcomes[tally_consensus([EvaluatorVerdict(role=s, verdict=votes[s]) for s in SEATS])] += 1
    scores = {s: [r["score"] for r in records if r["seat"] == s and isinstance(r.get("score"), int)] for s in seats}
    blocking = {s: sum(1 for r in records if r["seat"] == s and r.get("blocking_issues")) for s in seats}
    return {"candidates": len(by_cand), "per_seat": {s: dict(c) for s, c in per_seat.items()},
            "mean_score": {s: round(sum(v) / len(v), 1) for s, v in scores.items() if v},
            "blocking_count": {s: n for s, n in blocking.items() if scores.get(s)},
            "consensus": dict(outcomes), "total_cost_usd": round(sum(r.get("cost_usd", 0) for r in records), 4)}


def print_matrix(m: dict) -> None:
    print(f"\n{m['candidates']} candidates, ${m['total_cost_usd']:.2f}")
    for seat, counts in m["per_seat"].items():
        acc = counts.get("ACCEPT", 0); tot = sum(counts.values())
        extra = f"  mean score {m['mean_score'][seat]}  blocking on {m['blocking_count'].get(seat, 0)}" if seat in m.get("mean_score", {}) else ""
        print(f"  {seat:15s} ACCEPT {acc}/{tot}" + (f"  (errors {counts['ERROR']})" if counts.get("ERROR") else "") + extra)
    if m["consensus"]:
        print(f"  3-of-4: {m['consensus']}")


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run"); ap.add_argument("--candidate", action="append")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--seats", default=",".join(SEATS))
    ap.add_argument("--model-override", action="append", default=[])
    ap.add_argument("--effort", choices=("low", "medium", "high", "max"))
    ap.add_argument("--prompt-variant", default="current", choices=("current", "standard"))
    ap.add_argument("--candidate-file", help="JSON {candidate_id: {title, content, ...}} overrides")
    ap.add_argument("--cap-usd", type=float, default=2.0)
    ap.add_argument("--profile", help="backend profile (default: SECOND_BRAIN_PROFILE or config default)")
    ap.add_argument("--ground", action="store_true",
                    help="run the real pre-panel grounding audit + revision (orchestrator) before the seats")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")

    seats = [s.strip() for s in args.seats.split(",") if s.strip()]
    if any(s not in SEATS for s in seats):
        raise SystemExit(f"--seats must be from {SEATS}")
    if args.profile:
        os.environ["SECOND_BRAIN_PROFILE"] = args.profile
    resolver = default_resolver()
    tracker = SpendTracker(cap_usd=args.cap_usd)
    prompt_fn = get_evaluator_prompt
    if args.prompt_variant != "current":
        from src.prompts import panel as _panel
        prompt_fn = getattr(_panel, f"get_evaluator_prompt_{args.prompt_variant}", None)
        if prompt_fn is None:
            raise SystemExit(f"prompt variant {args.prompt_variant!r} not implemented in src/prompts/panel.py")
    cand_over = json.load(open(args.candidate_file)) if args.candidate_file else None

    rows = fetch_candidates(args.run, args.candidate, args.limit)
    log.info("%d candidate(s); seats=%s; variant=%s; cap $%.2f", len(rows), seats, args.prompt_variant, args.cap_usd)

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(OUT_DIR, f"{stamp}-{'grounded' if args.ground else args.prompt_variant}.jsonl")
    with open(out_path, "a", encoding="utf-8") as fh:
        def sink(rec: dict) -> None:
            fh.write(json.dumps(rec, default=str) + "\n"); fh.flush()
            if rec.get("verdict"):
                log.info("%-14s %-7s $%.3f  score=%-4s %s%s", rec["seat"], rec["verdict"], rec.get("cost_usd", 0),
                         rec.get("score"), "(revised) " if rec.get("revised") else "", rec["title"][:60])
        try:
            if args.ground:
                from src.dream_cycle.orchestrator import DreamCycleOrchestrator
                orch = DreamCycleOrchestrator()
                orch._spend = tracker
                orch._current_run_id = f"replay-{stamp}"
                for seat in seats:
                    _require_direct_api(seat, orch.resolver.spec_for(seat))
                records = replay_grounded(rows, seats, orch, sink=sink)
            else:
                records = replay(rows, seats, resolver, tracker, prompt_fn=prompt_fn,
                                 overrides=parse_overrides(args.model_override), effort=args.effort,
                                 candidate_overrides=cand_over, variant=args.prompt_variant,
                                 dry_run=args.dry_run, sink=sink)
        except SpendCapExceeded as e:
            log.error("stopped: %s", e); records = []
    if records:
        print_matrix(matrix(records, seats))
    print(f"records: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
