# Quality Baseline — Sacred Core (2026-06-06)

A point-in-time quality assessment of the three "sacred core" capabilities, captured as the
**gate for the streamlining review**. North Star: *accomplish everything we do now, but simpler,
to best demonstrate "memory that thinks."* This doc records the quality evidence that gate rests on.

## TL;DR
The two hardest-to-fake capabilities — **synthesis** and **retrieval** — hold up to quality
scrutiny, and **delivery** (Express) makes them legible in one sitting. **Quality gate: PASSED.**

## 1. Retrieval (substrate) — strong recall, soft precision
- HNSW **recall@10 = 0.93** (index healthy; alert threshold 0.85). `scripts/eval/recall_check.py`
- Golden queries (68): **hit@10 90%, hit@5 76%, hit@1 37%, MRR 0.53.** `python -m scripts.eval.run_evaluation --tier golden`
- 7/68 misses cluster on one memory (`890e87c3`).
- Read: the right memory is almost always *surfaced*, often not at rank 1. Notably `890e87c3`
  (the vector blind spot) still reaches the user through the **relationship graph** (it appears
  as a live contradiction in the brief), so the substrate has more than one path to a memory.

## 2. Synthesis (dream cycle) — validated on claude-opus-4.8; the differentiator
- **First successful 4.8 run: `7c712903` — 9 generated / 8 accepted / 1 rejected** (2026-06-06).
- Content is deep, evidence-grounded, cross-domain. Flagship accepted insight: *the user's
  backup-then-prune habit is an unarticulated application of Bezos's two-way-door reversibility
  doctrine* (`cross_project_collision`, 4 source memories, WHAT/EVIDENCE structure). Also: AWS-adoption
  corpus → Anthropic DNB CSM job description 1:1 mapping; a unified "defer-until-context-is-maximal"
  decision heuristic spanning engineering and professional domains.
- **Panel discrimination — the central pre-4.8 question, now ANSWERED.** Pre-4.8 looked like a
  rubber stamp (36 ACCEPTED / 2 REJECTED ≈ 95% accept; dissent on only 6/38 ≈ 16%). On 4.8:
  **dissent on 6 of 9 candidates** (only 3 unanimous), the skeptic dissented on 6/9, and the **one
  rejection was a meaningful 3-1** that caught an *overclaim* on factual grounding. The 4-judge BFT
  is genuinely discriminating, not rubber-stamping.

## 3. Delivery (Express) — high-signal, legible
- Live brief generated 2026-06-06 ~13:18 via `scripts/brief.py`: **led** with a brand-new 4.8
  insight, ranked 5 items (3 insights + 2 contradictions), wrote its own headlines, cited
  provenance ("Drawn from …"), and rendered the feedback footer.
- Read: the delivery half (missing in the evolution analysis) now lands; all three capabilities
  compose into a single legible artifact — the North Star demonstration in one screen.

## Reliability fixes shipped today to enable the 4.8 read
- `199d5ee` — decode agent output leniently (`errors="replace"`); fixed the `UnicodeDecodeError`
  (stray byte `0xdc`) that aborted the first 4.8 run with 0 candidates.
- `c10d1ae` — recover the JSON payload from 4.8 tool-using transcripts via a largest-balanced-span
  scanner (the Explorer emitted valid JSON buried in tool-call/marker/prose noise). Shared by
  Explorer, Thinker, and all 4 evaluators.

## Honest soft spots (inputs to streamlining, not blockers)
- Retrieval **rank-1 precision (37%)** and the one blind spot (`890e87c3`).
- Synthesis quality rests on **one 4.8 run (n=9 candidates)** — strong signal, small sample.
- The pipeline's **output-contract was brittle** — it took two fixes today just to run on 4.8.
- Express **email push not activated**, and not exercised in run `7c712903` (ran
  `dream_cycle_run.py` directly, not the scheduled wrapper that chains `express_push`).

## Reproduce
| Capability | Command / source |
|---|---|
| Retrieval index health | `scripts/eval/recall_check.py --verbose` |
| Retrieval quality | `python -m scripts.eval.run_evaluation --tier golden` |
| Synthesis | `dream_cycle_runs` / `dream_cycle_candidates` for run `7c712903` |
| Delivery | `scripts/brief.py` |
