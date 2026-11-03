"""Skim probe — run the skim prompt over a few documents and report aggregates only.

Phase 1 of docs/PLAN-skim-stage.md: before the skim runs inside the dream cycle,
measure on real arrivals whether the chosen model (Amazon Nova 2 Lite, forced tool
choice, reasoning off, temperature 0, Flex tier) returns schema-valid records whose
quotes are verbatim. Each document is fetched, skimmed once, validated
(``src.skim.schema``), checked for verbatim quotes (``src.skim.faithfulness``) and
metered through ``SpendTracker`` under ``--cap-usd``. One JSON line per document
goes to ``logs/skim_probe/<stamp>.jsonl`` (local; contains document-derived text).

STDOUT carries aggregates only — never a title, summary, quote or document text.

    AWS_PROFILE=second-brain-jobs .venv/bin/python scripts/skim_probe.py \\
        --since 2026-09-17 --limit 25 --cap-usd 0.25
    ... --ids <uuid> [<uuid> ...]
    ... --model google.gemma-3-12b-it --service-tier default
    ... --dry-run                       # build requests, no model calls, no spend
    ... --strict                        # Phase 1 behaviour: reject over-long fields

Reads ``memories.content`` only at the user's request (the runner's store,
``src/skim/store.py``, is the other reader — inside a dream-cycle run).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Callable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backends.bedrock import SERVICE_TIERS, BedrockInvoker  # noqa: E402
from src.backends.cost import SpendCapExceeded, SpendTracker, UsageUnavailable, invoke_metered, price_for
from src.backends.base import assert_corpus_route
from src.backends.resolver import RoleBackend, SKIM_ROLE  # noqa: E402
from src.prompts.skim import PROMPT_VERSION, SKIM_SYSTEM_PROMPT, build_skim_request  # noqa: E402
from src.skim.faithfulness import check_record  # noqa: E402
from src.skim.schema import SKIM_SCHEMA, SkimValidationError, validate_record  # noqa: E402

log = logging.getLogger("skim_probe")

DEFAULT_MODEL = "us.amazon.nova-2-lite-v1:0"
DEFAULT_CAP_USD = 0.25
DEFAULT_MAX_TOKENS = 4096
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "logs", "skim_probe")


# --------------------------------------------------------------------------- #
# Data access — the one place that reads memories.content                      #
# --------------------------------------------------------------------------- #
def fetch_documents(ids: list[str] | None, since: str | None, limit: int | None) -> list[dict]:
    """Parent-level, non-superseded memories: id, title, source_type, created_at, content."""
    from psycopg2.extras import RealDictCursor
    from src.db import get_connection
    where = ["parent_id IS NULL", "status IS DISTINCT FROM 'superseded'"]
    params: list = []
    if ids:
        where.append("id = ANY(%s::uuid[])"); params.append(list(ids))
    if since:
        where.append("created_at >= %s"); params.append(since)
    if not ids and not since and not limit:
        raise SystemExit("need --ids, --since or --limit")
    sql = ("SELECT id, title, source_type, created_at, content FROM memories "
           f"WHERE {' AND '.join(where)} ORDER BY created_at DESC")
    if limit:
        sql += " LIMIT %s"; params.append(limit)
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["id"] = str(r["id"])
    return rows


# --------------------------------------------------------------------------- #
# Skim one document                                                            #
# --------------------------------------------------------------------------- #
def make_invoker(model: str, *, service_tier: str | None, temperature: float | None,
                 max_tokens: int) -> BedrockInvoker:
    assert_corpus_route("bedrock", model, SKIM_ROLE)
    return BedrockInvoker(model=model, service_tier=service_tier, temperature=temperature,
                          max_tokens=max_tokens)


def skim_document(doc: dict, invoker, model: str, tracker: SpendTracker, *, effort: str = "low",
                  timeout: int = 120, run_id: str | None = None, dry_run: bool = False,
                  coerce: bool = True) -> dict:
    """Skim one document. Never raises except :class:`SpendCapExceeded` (a hard stop).

    ``coerce`` is passed to :func:`validate_record`: by default over-long fields
    are trimmed and listed under ``coercions``; ``--strict`` keeps Phase 1's
    reject-on-overrun behaviour so the raw overrun rate can still be measured.
    """
    spec = RoleBackend(SKIM_ROLE, "bedrock", model, effort)
    assert_corpus_route(spec.backend, spec.model, spec.role)
    content = doc.get("content") or ""
    request = build_skim_request(content, doc.get("source_type"), doc.get("title"), doc.get("created_at"))
    rec: dict = {
        "memory_id": doc["id"], "channel": doc.get("source_type") or "unknown",
        "created_at": str(doc.get("created_at") or ""), "model": model, "prompt_version": PROMPT_VERSION,
        "doc_chars": len(content), "prompt_chars": len(SKIM_SYSTEM_PROMPT) + len(request),
        "dry_run": dry_run, "error": None, "valid": None, "validation_error": None, "record": None,
        "coercions": [], "faithfulness": None, "latency_s": None, "input_tokens": None, "output_tokens": None,
        "service_tier_served": None, "cost_usd": 0.0,
    }
    if dry_run:
        return rec
    t0 = time.monotonic()
    before = tracker.total_usd
    try:
        result = invoke_metered(invoker, spec, tracker, SKIM_SYSTEM_PROMPT, request, effort=effort, timeout=timeout,
                                stage="skim_probe", run_id=run_id, schema=SKIM_SCHEMA)
    except SpendCapExceeded:
        raise
    except Exception as e:
        if tracker.blocked_reason:
            raise UsageUnavailable(tracker.blocked_reason) from e
        rec.update({"error": type(e).__name__, "cost_usd": round(tracker.total_usd - before, 6),
                    "latency_s": round(time.monotonic() - t0, 2)})
        return rec
    rec["latency_s"] = round(time.monotonic() - t0, 2)
    usage = result.get("usage")
    if isinstance(usage, dict):
        rec["input_tokens"] = usage.get("inputTokens", usage.get("input_tokens"))
        rec["output_tokens"] = usage.get("outputTokens", usage.get("output_tokens"))
        rec["service_tier_served"] = usage.get("serviceTier")
        rec["cost_usd"] = round(tracker.total_usd - before, 6)   # may raise SpendCapExceeded
    output = result.get("output")
    if isinstance(output, list):
        output = output[0] if output else None
    rec["raw_output"] = output
    try:
        record = validate_record(output, coerce=coerce)
    except SkimValidationError as e:
        rec.update({"valid": False, "validation_error": str(e)[:400]})
        return rec
    rec.update({"valid": True, "record": record.to_dict(), "coercions": list(record.coercions),
                "faithfulness": check_record(record, content)})
    return rec


def run_probe(docs: list[dict], invoker, model: str, tracker: SpendTracker, *, effort: str = "low",
              timeout: int = 120, run_id: str | None = None, dry_run: bool = False,
              coerce: bool = True, sink: Callable[[dict], None] | None = None) -> list[dict]:
    """Skim every document in order; stop (re-raise) when the spend cap trips."""
    records: list[dict] = []
    emit = sink or (lambda r: None)
    for doc in docs:
        rec = skim_document(doc, invoker, model, tracker, effort=effort, timeout=timeout,
                            run_id=run_id, dry_run=dry_run, coerce=coerce)
        records.append(rec); emit(rec)
        if not dry_run:
            log.info("%s %-16s %s  %5.1fs  $%.5f%s", rec["memory_id"][:8], rec["channel"][:16],
                     "ERROR" if rec["error"] else ("valid" if rec["valid"] else "INVALID"),
                     rec["latency_s"] or 0.0, rec["cost_usd"],
                     f"  coerced×{len(rec['coercions'])}" if rec.get("coercions") else "")
    return records


# --------------------------------------------------------------------------- #
# Aggregates (the only thing that reaches stdout)                              #
# --------------------------------------------------------------------------- #
def _pct(num: int, den: int) -> float | None:
    return None if den == 0 else round(100.0 * num / den, 1)


_INDEX_RE = re.compile(r"\[\d+\]")
_NUMBER_RE = re.compile(r"\b\d+\b")


def reason_key(validation_error: str) -> str:
    """Collapse a validation message to a countable class: field path plus the
    kind of failure, with indices/numbers generalised and any model-produced
    value (after ', got') dropped — safe to print as an aggregate."""
    msg = validation_error.split(", got")[0].split("; the limit")[0]
    msg = _INDEX_RE.sub("[]", msg)
    return _NUMBER_RE.sub("N", msg).strip()


def aggregate(records: list[dict]) -> dict:
    attempted = [r for r in records if not r.get("dry_run")]
    errors = [r for r in attempted if r.get("error")]
    valid = [r for r in attempted if r.get("valid")]
    invalid = [r for r in attempted if r.get("valid") is False]
    latencies = [r["latency_s"] for r in attempted if isinstance(r.get("latency_s"), (int, float))]
    quotes_emitted = sum(r["faithfulness"]["quotes_emitted"] for r in valid)
    quotes_verbatim = sum(r["faithfulness"]["quotes_verbatim"] for r in valid)
    match_paths: Counter = Counter()
    for r in valid:
        match_paths.update(r["faithfulness"].get("match_paths") or {})
    coerced = [r for r in valid if r.get("coercions")]
    coercion_classes = Counter(c for r in coerced for c in r["coercions"])
    with_items = [r for r in valid if r["record"]["decisions"] or r["record"]["open_questions"]]
    per_channel: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        c = per_channel[r["channel"]]
        c["docs"] += 1
        if r.get("dry_run"):
            continue
        if r.get("error"):
            c["errors"] += 1
        elif r.get("valid"):
            c["valid"] += 1
            c["quotes_fabricated"] += r["faithfulness"]["quotes_fabricated"]
        else:
            c["invalid"] += 1
    return {
        "docs": len(records),
        "dry_run_docs": len(records) - len(attempted),
        "calls": len(attempted),
        "errors": len(errors),
        "json_valid": len(valid),
        "json_valid_pct": _pct(len(valid), len(attempted)),
        "invalid_reasons": dict(Counter(reason_key(r["validation_error"] or "") for r in invalid)),
        "coerced_records": len(coerced),
        "coerced_pct": _pct(len(coerced), len(valid)),
        "coercion_classes": dict(coercion_classes),
        "with_decision_or_question_pct": _pct(len(with_items), len(valid)),
        "quotes_emitted": quotes_emitted,
        "quotes_verbatim": quotes_verbatim,
        "quote_verbatim_pct": _pct(quotes_verbatim, quotes_emitted),
        "quote_match_paths": dict(match_paths),
        "fabricated_quotes": quotes_emitted - quotes_verbatim,
        "records_with_fabrication": sum(1 for r in valid if not r["faithfulness"]["all_verbatim"]),
        "empty_summaries": sum(1 for r in valid if not r["record"]["summary"].strip()),
        "latency_mean_s": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "latency_max_s": max(latencies) if latencies else None,
        "input_tokens": sum(r["input_tokens"] or 0 for r in attempted),
        "output_tokens": sum(r["output_tokens"] or 0 for r in attempted),
        "total_cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in attempted), 6),
        "service_tiers_served": dict(Counter(r["service_tier_served"] or "n/a" for r in attempted)),
        "per_channel": {ch: dict(c) for ch, c in sorted(per_channel.items())},
    }


def print_aggregates(agg: dict, *, model: str, out_path: str | None) -> None:
    def pct(v):
        return "n/a" if v is None else f"{v:.1f}%"
    print(f"\nskim probe — {model} — prompt {PROMPT_VERSION}")
    print(f"  docs {agg['docs']}  calls {agg['calls']}  errors {agg['errors']}"
          + (f"  dry-run {agg['dry_run_docs']}" if agg["dry_run_docs"] else ""))
    print(f"  JSON-valid            {agg['json_valid']}/{agg['calls']}  ({pct(agg['json_valid_pct'])})")
    for reason, n in sorted(agg["invalid_reasons"].items(), key=lambda kv: -kv[1]):
        print(f"    invalid: {n:3d} × {reason}")
    print(f"  coerced records       {agg['coerced_records']}/{agg['json_valid']}  ({pct(agg['coerced_pct'])})")
    for cls, n in sorted(agg["coercion_classes"].items(), key=lambda kv: -kv[1]):
        print(f"    coerced: {n:3d} × {cls}")
    print(f"  ≥1 decision/question  {pct(agg['with_decision_or_question_pct'])} of valid records")
    print(f"  quotes verbatim       {agg['quotes_verbatim']}/{agg['quotes_emitted']}  ({pct(agg['quote_verbatim_pct'])})"
          f"  fabricated {agg['fabricated_quotes']} in {agg['records_with_fabrication']} record(s)"
          f"  paths {agg['quote_match_paths']}")
    print(f"  empty summaries       {agg['empty_summaries']}")
    print(f"  latency mean/max      {agg['latency_mean_s']} / {agg['latency_max_s']} s")
    print(f"  tokens in/out         {agg['input_tokens']} / {agg['output_tokens']}")
    print(f"  total cost            ${agg['total_cost_usd']:.5f}  tiers served {agg['service_tiers_served']}")
    print("  per channel:")
    for ch, c in agg["per_channel"].items():
        print(f"    {ch:20s} docs {c.get('docs', 0):3d}  valid {c.get('valid', 0):3d}  invalid {c.get('invalid', 0):3d}"
              f"  errors {c.get('errors', 0):3d}  fabricated quotes {c.get('quotes_fabricated', 0):3d}")
    if out_path:
        print(f"records: {out_path}")


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sel = ap.add_argument_group("document selection")
    sel.add_argument("--ids", nargs="+", metavar="ID", help="memory ids to skim")
    sel.add_argument("--since", metavar="ISO", help="arrivals with created_at >= this timestamp")
    sel.add_argument("--limit", type=int, metavar="N", help="at most N documents (newest first)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--service-tier", choices=sorted(SERVICE_TIERS) + ["none"], default="flex",
                    help="Converse serviceTier.type; 'none' sends no tier (default: flex)")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--effort", default="low", help="low = extended reasoning off on Nova 2 (default)")
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--cap-usd", type=float, default=DEFAULT_CAP_USD)
    ap.add_argument("--profile", help="AWS named profile for the Bedrock client (sets AWS_PROFILE)")
    ap.add_argument("--strict", action="store_true",
                    help="reject over-long summary/quote/topics instead of trimming them (Phase 1 behaviour)")
    ap.add_argument("--dry-run", action="store_true", help="build the requests; no model calls, no spend")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    if not price_for(args.model).known:
        raise SystemExit(f"{args.model!r} has no entry in src/backends/cost.py _PRICE_TABLE; "
                         "add it before any live call (plan constraint 2).")
    tier = None if args.service_tier == "none" else args.service_tier
    invoker = None if args.dry_run else make_invoker(
        args.model, service_tier=tier, temperature=args.temperature, max_tokens=args.max_tokens)
    tracker = SpendTracker(cap_usd=args.cap_usd)

    docs = fetch_documents(args.ids, args.since, args.limit)
    log.info("%d document(s); model=%s tier=%s temperature=%s effort=%s cap $%.2f%s", len(docs), args.model,
             tier, args.temperature, args.effort, args.cap_usd, "  (dry run)" if args.dry_run else "")

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(OUT_DIR, f"{stamp}.jsonl")
    records: list[dict] = []
    with open(out_path, "a", encoding="utf-8") as fh:
        def sink(rec: dict) -> None:
            fh.write(json.dumps(rec, default=str) + "\n"); fh.flush()
            records.append(rec)
        try:
            run_probe(docs, invoker, args.model, tracker, effort=args.effort, timeout=args.timeout,
                      run_id=f"skim-probe-{stamp}", dry_run=args.dry_run, coerce=not args.strict, sink=sink)
        except SpendCapExceeded as e:
            log.error("stopped: %s", e)
    print_aggregates(aggregate(records), model=args.model, out_path=out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
