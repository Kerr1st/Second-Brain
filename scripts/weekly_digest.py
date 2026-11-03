#!/usr/bin/env python3
"""Weekly digest (Refactor P3): aggregate the last N days of captured memory and
distill ONE `synthesis` memory answering "what am I working on / learning".

This is the proactive half of the system's purpose — it answers questions that
point retrieval cannot (e.g. "what am I spending most of my time on"). Idempotent
via source_url digest://{ISO-year}-W{week}.

Backend (rewritten 2026-09-23). This job used to construct ``KiroInvoker`` directly
with ``model="claude-sonnet-5"``. That was wrong twice over:

  1. Policy. kiro-cli 2.22 ignores ``--model`` (see config/backends.toml
     [profiles.controlled] and ``AgenticCliInvoker._note_model_honored``), so its
     server-side "Auto" routing served ``claude-fable-5.1`` — labelled "NOT FOR
     CUSTOMER DATA, ITAR OR PII" — while this job summarises the user's own
     corpus. The standing rule (docs/PLAN-panel-acceptance.md §3, pinned by
     tests/test_resolver.py) is that the corpus never runs on Kiro/Fable.
  2. It failed anyway: ``TimeoutError: Agent subprocess timed out after 600s``
     (logs/weekly-digest-*.log), so there was no digest after ~2026-09-16.

The digest now resolves its model through the active profile exactly like the rest
of the system (``default_resolver()`` -> ``spec_for``/``invoker_for``) and refuses
to run on an MCP-capable/agentic backend. See :data:`DIGEST_ROLE` for why it
borrows the ``skim`` seat, and ``digest_cap_usd`` for the spend guard.

Usage: .venv/bin/python scripts/weekly_digest.py [--days N] [--dry-run]
"""

import argparse
import datetime
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import get_connection, create_memory, get_processed_source_urls
from src.embeddings import generate_embedding
from src.backends.cost import SpendCapExceeded, SpendTracker, cap_from_env, invoke_metered
from src.backends.resolver import SKIM_ROLE, default_resolver, resolve_stage

#: The digest uses the faithful-summarisation seat. Shared profile validation
#: and resolve_stage enforce direct Bedrock and reject forbidden model overrides;
#: invoke_metered applies one digest-specific spend threshold.
DIGEST_ROLE = SKIM_ROLE

#: Optional model override (same backend as the resolved role — the backend, and
#: therefore the Kiro/Fable prohibition, is never overridable). Unset by default:
#: the model comes from the profile.
DIGEST_MODEL = os.environ.get("DIGEST_MODEL") or None

#: Timeout for one direct Bedrock Converse call. The old 600s was sized for
#: kiro-cli's ~35s startup plus an agentic tool loop; a single tool-less call with
#: a ~2-5k-token prompt answers in seconds. The headroom here is for the Flex
#: service tier, which is best-effort and may queue. A timeout is a hard failure
#: (non-zero exit), never a silent skip.
DIGEST_TIMEOUT = int(os.environ.get("DIGEST_TIMEOUT", "180"))

#: The digest's own spend cap, in USD. It is a single call on a cheap model
#: (~$0.002), so $0.25 is ~100x headroom while still stopping a runaway — e.g. a
#: DIGEST_MODEL override onto an unpriced model, which cost.py bills at the
#: $10/$50 ceiling. Override with SECOND_BRAIN_DIGEST_CAP_USD. Deliberately tiny
#: next to the nightly run cap ($9, scripts/jobs/dream_cycle_scheduled.sh) so the
#: digest can never eat the day's budget.
DEFAULT_DIGEST_CAP_USD = 0.25
DIGEST_CAP_ENV = "SECOND_BRAIN_DIGEST_CAP_USD"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

SOURCE_TYPE = "weekly_digest"
SYSTEM_PROMPT = (
    "You are a personal-knowledge analyst. You are given aggregated statistics about "
    "the user's captured activity over a recent window (top topic tags, active "
    "projects, channel activity, and titles of decisions/insights they recorded). "
    "Do NOT reply conversationally. Write a concise digest answering: what is the user "
    "spending time on, what did they decide, and what are they learning. Output ONLY a "
    'JSON object (no prose, no code fence): {"title":"<=80 chars","summary":"3-6 '
    'sentences, concrete, naming the actual projects/themes/decisions, written to '
    "directly answer 'what am I working on and learning'\"}. Use only the provided "
    "data; do not invent."
)


class DigestBackendError(RuntimeError):
    """The digest cannot run on the backend the active profile resolves to."""


def digest_cap_usd():
    return cap_from_env(DIGEST_CAP_ENV, DEFAULT_DIGEST_CAP_USD)


def resolve_digest_backend(resolver=None, model_override=None):
    """Resolve the digest through shared routing, including override validation."""
    resolver = resolver or default_resolver()
    try:
        return resolve_stage(DIGEST_ROLE, resolver=resolver, model_override=model_override)
    except ValueError as exc:
        raise DigestBackendError(
            f"Refusing weekly digest backend {resolver.spec_for(DIGEST_ROLE).backend!r}/model: " + str(exc)
            + ". Use SECOND_BRAIN_PROFILE=controlled; Kiro/Fable are forbidden."
        ) from exc


def aggregate(days):
    """Pull the recent-activity signal from the memories table (no KG; that is P4)."""
    iv = "%s days" % int(days)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT t, count(*) FROM memories m, unnest(m.tags) t "
                        "WHERE m.created_at >= now() - %s::interval GROUP BY t "
                        "ORDER BY 2 DESC LIMIT 25", (iv,))
            tags = cur.fetchall()
            cur.execute("SELECT project, count(*) FROM memories WHERE created_at >= "
                        "now() - %s::interval AND project IS NOT NULL AND project <> '' "
                        "AND project !~ '^[0-9a-f-]{36}$' GROUP BY 1 ORDER BY 2 DESC "
                        "LIMIT 12", (iv,))
            projects = cur.fetchall()
            cur.execute("SELECT source_type, count(*) FROM memories WHERE created_at >= "
                        "now() - %s::interval GROUP BY 1 ORDER BY 2 DESC", (iv,))
            sources = cur.fetchall()
            cur.execute("SELECT title FROM memories WHERE created_at >= now() - %s::interval "
                        "AND source_type = 'distilled_chat' AND title IS NOT NULL "
                        "ORDER BY created_at DESC LIMIT 40", (iv,))
            decisions = [r[0] for r in cur.fetchall()]
    return tags, projects, sources, decisions


def build_user_message(days, tags, projects, sources, decisions):
    def fmt(rows):
        return ", ".join(f"{name} ({n})" for name, n in rows) or "(none)"
    parts = [
        f"Window: last {days} days.",
        f"Top topic tags: {fmt(tags)}",
        f"Active projects: {fmt(projects)}",
        f"Channel activity: {fmt(sources)}",
        "Recent decisions/insights recorded:",
    ]
    parts += [f"- {t}" for t in decisions] or ["- (none)"]
    return "\n".join(parts)


def run(days=7, dry_run=False, resolver=None, tracker=None):
    y, w, _ = datetime.date.today().isocalendar()
    url = f"digest://{y}-W{w:02d}"
    if url in get_processed_source_urls(SOURCE_TYPE):
        log.info("Digest %s already exists, skipping", url)
        return None
    tags, projects, sources, decisions = aggregate(days)
    if not sources:
        log.info("No activity in window, nothing to digest")
        return None

    invoker, spec = resolve_digest_backend(resolver, model_override=DIGEST_MODEL)
    if tracker is None:
        tracker = SpendTracker(cap_usd=digest_cap_usd())
    log.info("Digest backend: profile role %r -> %s / %s (effort=%s), timeout %ds, cap $%.2f",
             DIGEST_ROLE, spec.backend, spec.model, spec.effort or "low",
             DIGEST_TIMEOUT, tracker.cap_usd)

    res = invoke_metered(
        invoker, spec, tracker, SYSTEM_PROMPT, build_user_message(days, tags, projects, sources, decisions),
        tools=False, timeout=DIGEST_TIMEOUT, effort=spec.effort or "low",
        stage="weekly_digest")
    log.info("Digest running cost $%.4f of $%.2f stop threshold",
             tracker.total_usd, tracker.cap_usd)

    out = res["output"] if isinstance(res["output"], dict) else {}
    summary = (out.get("summary") or "").strip()
    title = (out.get("title") or f"Weekly digest {url}").strip()[:200]
    if not summary:
        raise ValueError("Digest model returned no summary")
    # Anchor to the question it answers so it is retrievable for meta-queries
    # like "what am I spending my time on" (the summary alone is object-level).
    body = (f"What I've been spending my time working on and learning "
            f"(last {days}-day digest):\n\n{summary}")

    if dry_run:
        log.info("[DRY] %s\nTITLE: %s\n%s", url, title, body)
        return body

    create_memory(
        type="synthesis", title=title, content=body,
        embedding=generate_embedding(body), tags=["digest", "weekly"],
        source_url=url, source_type=SOURCE_TYPE, confidence=0.85,
        mem_class="semantic", metadata={"days": days, "week": url},
    )
    log.info("Created digest memory %s", url)
    try:  # best-effort proactive ping (the "here's your week" surface)
        import subprocess
        subprocess.run(["osascript", "-e",
                        'display notification "%s" with title "Weekly digest"'
                        % title.replace('"', "'")[:180]], timeout=10, check=False)
    except Exception:
        pass
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true",
                    help="Metered model preview; does not store or deliver the digest.")
    args = ap.parse_args()
    # Every failure mode below is loud and non-zero: the job is monitored by
    # com.second-brain.liveness, and a silent skip is how the digest went missing
    # for a week in the first place.
    try:
        run(days=args.days, dry_run=args.dry_run)
    except DigestBackendError as exc:
        log.error("Weekly digest refused: %s", exc)
        return 2
    except SpendCapExceeded as exc:
        log.error("Weekly digest aborted on its spend cap: %s", exc)
        return 3
    except TimeoutError as exc:
        log.error("Weekly digest timed out after %ds: %s", DIGEST_TIMEOUT, exc)
        return 4
    except (ValueError, RuntimeError, OSError) as exc:
        log.error("Weekly digest failed (%s)", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
