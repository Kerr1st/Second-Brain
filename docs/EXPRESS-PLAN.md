# Express — Plan (closing the loop)

*Draft for iteration, 2026-06-06. Builds on `SECOND-BRAIN-PURPOSE-AND-EVOLUTION.md`: Express is
the unfinished top of the ladder (Storage → Retrieval → Synthesis → **Partnership**).*

## Status (2026-06-06) — built

- ✅ **P1 — on-demand `brief`** (`a16632c`): `src/express.py` (compose + LLM editor + render) and `scripts/brief.py`. Verified live; high-quality headlines.
- ✅ **P2 — proactive Gmail push** (`d715b90`): `should_push()` (high bar) + `render_email()` + `send_email()`, gated CLI `scripts/express_push.py`, chained after the noon dream cycle. *Activation pending: user sets `EXPRESS_EMAIL_TO/FROM` + `GMAIL_APP_PASSWORD`; until then it composes-but-skips benignly.*
- ✅ **P3 (slice) — `memory_brief` MCP tool** (`109249b`): the in-context surface; an agent can volunteer the briefing at session start.
- ✅ **P3 — the feedback loop** (`ef0d730`): `brief --useful/--less/--mute/--unmute/--prefs` shape what the briefing surfaces (item / kind / topic; gradient boost→soft-down→hard-hide), stored in `express_feedback` (migration 010), applied as hard filters + soft re-rank. **Delivery-only** for now (does not yet feed the dream cycle's synthesis); that remains an optional future extension.

Full suite: 637 passing.

## The gap, precisely

The system already produces synthesis in **three** places — and **none of them reach you**:

| Producer | What it makes | Where it goes | Reaches you? |
|---|---|---|---|
| Dream cycle | Accepted insights + a digest | `logs/dream-cycle-digest-*.md` (a file) | ✗ |
| Weekly digest | One `synthesis` memory | A **title-only** macOS notification | ✗ (barely) |
| `session_start` run | Capped dream-cycle candidates | Stored as memories; no surface | ✗ |

The thinking happens. Nobody hears it. **Express is the delivery layer on top of synthesis we already generate** — not a new synthesizer.

## What Express *is* for this project

The Partnership rung: the system **pushes the right knowledge to you, at the right time, in a form you consume, and learns what's worth telling you.** It completes the cognitive-science model the project is built on:

> encode (capture) → consolidate (dream cycle) → **express at the moment of need** ← the missing third act

Each Express form maps to a finding the project already honors:
- **Resurfacing** ("remember this?") = spacing/testing effect + desirable difficulty (Bjork) — surfacing high-value, long-unretrieved memories at intervals. This is spaced repetition *for your own knowledge.*
- **Proactive briefing** = encoding-specificity (Tulving & Thomson) — surface it when the context matches, instead of waiting to be asked.
- **Feedback on what's surfaced** = closes the loop, reusing the dream cycle's existing rejection-feedback machinery.

## What it expresses (all from existing data — no new stores)

1. **"What I figured out."** Recent accepted dream-cycle insights — especially cross-project ones. *(memories tagged `dream-cycle`.)*
2. **"Where you're contradicting yourself."** Active `contradicts` relationships. *(memory_relationships.)*
3. **"Remember this?"** Resurfaced high-value memories not accessed in 30+ days. *(the desirable-difficulty signal, already used by the Explorer.)*
4. **"What you're working on / learning."** The latest weekly digest. *(already built.)*
5. **"Open threads."** Active `question` memories — flagged if newly answerable.

## Surfaces (how/where) — phased, reusing channels we already have

- **P1 — MVP: on-demand `brief` command.** Composes the briefing and prints clean Markdown to the terminal. Zero new infrastructure, instantly demonstrable, and it lives where you already work (the CLI). *This is the anchor — it makes the dream cycle's insights reach you on demand.*
- **P2 — Proactive push.** Deliver the same briefing each morning (right after the overnight dream cycle) to a channel you actually check. The "it pings me with an insight" moment — the real Partnership surface.
- **P3 — In-context + feedback.** An MCP `memory_brief` tool so the agent volunteers the briefing at session start (finally wiring the dormant `should_run_briefing` scaffolding to *deliver*), plus a one-tap "useful / not" path that feeds the existing dream-cycle feedback loop so **Express learns what's worth telling you.**

## Build (minimal, surgical — the project's ethos)

- **New, small:** `src/express.py` (the briefing composer — queries existing tables, with one optional LLM "editor" pass for the partner voice) + `scripts/brief.py` (the CLI).
- **P2:** a delivery function + one morning LaunchAgent (reuse `job_wrapper.sh` + `scheduling/`).
- **P3:** one MCP tool (`memory_brief`) + wire `session_start` to deliver + a feedback flag (reuse dream-cycle feedback infra).
- **Reuse, don't reinvent:** `weekly_digest` aggregation, dream-cycle accepted memories, `contradicts` links, the resurfacing signal, `AgentInvoker`, the notification/Slack channels, and the `should_run_briefing` frequency cap.

## Locked decisions (2026-06-06)

1. **Scope:** v1 = **P1 (on-demand `brief`) + P2 (proactive Gmail push)**.
2. **Channel:** email via **Gmail** (SMTP + App Password), **to self**.
3. **Format / voice:** **headline + detail-below**. A light LLM "editor" pass writes the punchy headlines and ranks the items; the detail beneath each is the underlying synthesis + source-memory titles. Email = simple HTML (scannable headlines up top, detail beneath) with a plain-text fallback — no fragile collapsible HTML.
4. **Marquee:** **cross-project synthesis is the signature**, but the editor auto-ranks and leads with the day's single strongest item, so the marquee is automatic (fresh each run).
5. **Timing:** the P2 email is **chained to fire right after the noon dream cycle** completes (freshest synthesis; the dream cycle is daily-noon now, not overnight).
6. **Push bar (rare + high-signal):** the email sends **only when the just-finished dream cycle produced a new cross-project synthesis OR a detected contradiction**. Everything else (routine insights, resurfacing, weekly digest) stays in the on-demand `brief`. *Initial bar — tune as we watch it in action.*
7. **Quiet days:** no email. The on-demand `brief` is the always-available pull surface.
8. **Privacy:** to self only, lean footprint (headlines + light detail, not raw memories). Accepted trade (local-first store → Gmail; low risk, self-to-self).
9. **Config:** `EXPRESS_EMAIL_TO` / `EXPRESS_EMAIL_FROM` / `GMAIL_APP_PASSWORD` via env vars / gitignored config — nothing hardcoded. The **actual address is kept out of committed files** (origin is GitHub); a one-line change updates it.
10. **Defaults:** ≤5 items (lead + up to 4); resurfacing won't repeat an item within ~14 days; "new insights" = the just-completed dream cycle's accepted items, with contradictions/resurfacing on a rolling window.

## Build sequence (start here)

**Reuse, don't reinvent.** Sources already in place: dream-cycle accepted memories (tagged `dream-cycle`), `contradicts` edges (`memory_relationships`), the resurfacing signal (high-value + `last_accessed_at` > 30d), the `weekly_digest` synthesis memory, active `question` memories, `AgentInvoker` (model `claude-opus-4.8`), the `should_run_briefing` cadence, and `scheduling/` + `job_wrapper.sh`.

**P1 — on-demand `brief` (the anchor):**
1. `src/express.py` → `compose_briefing()`: query the five content sources (apply the ≤5 cap, resurfacing de-dup, recency windows); return structured items.
2. LLM editor pass via `AgentInvoker`: rank items, write one headline each, pick the lead. Detail = existing synthesis + source titles. JSON out (parsed by the now-`strict=False` parser).
3. `scripts/brief.py` → CLI: compose → render Markdown to stdout. Tests for the composer (mock LLM + DB).

**P2 — proactive Gmail push (high bar):**
4. `src/express.py` → `should_push()` (true iff latest dream-cycle run produced a cross-project synthesis or a contradiction); `render_email()` (HTML headlines+detail + plaintext fallback); `send_email()` (smtplib + STARTTLS to smtp.gmail.com:587, creds from env/config).
5. `scripts/express_push.py` → CLI: if `should_push()`, compose + send; else log "nothing worth sending" and exit 0.
6. Chain after the noon dream cycle (extend the dream-cycle job to run the push after `dream_cycle_run.py`, or a separate ~12:45 LaunchAgent via `job_wrapper.sh`). Tests for `should_push` + render + a mocked send.

**Verify each phase:** compile + tests; `brief` prints a real briefing; prove the email composes via an echo/no-send path before wiring live creds.

## Why this is the right next build

- It's the **highest-leverage, lowest-code** move: the expensive part (synthesis) already runs; Express is a thin delivery layer that finally makes it *land*.
- It's the most **demonstrable** capability we have. The 30-second demo: *"My second brain doesn't wait to be asked. Each morning it tells me the non-obvious connection it found overnight, flags where I'm reversing a past decision, and resurfaces something valuable I'd forgotten — and it learns what's worth telling me."* That is "memory that thinks **with** you," shown live.

## Success criteria

- The dream cycle's accepted insights reach you **without you querying.**
- A briefing is consumable in **< 30 seconds** and names concrete projects/decisions/insights (no fluff).
- (P3) Express **measurably learns**: the useful-rate of surfaced items trends up via feedback.

## Non-goals

- No web UI or app. No new datastore. Express **delivers** synthesis; it does not **re-think** it (no second synthesis engine).

## Activation runbook — launchd credential wiring (2026-06-13)

P2 is built and verified-composing; the only thing gating live delivery is that the three Gmail
env vars (decision #9) aren't present in launchd's minimal environment, so `email_configured()`
returns False and `express_push.py` composes-but-skips benignly. **No code change is needed — this
is pure secret wiring.** Effort ~30 min, no metered cost, independent of Fable.

**Mechanism (confirmed in code):** `scripts/express_push.py` is chained after the noon cycle in
`scripts/jobs/dream_cycle_scheduled.sh` (`express_push.py || true`) → `should_push()` gate →
compose/edit/render → `email_configured()` → `send_email()` (Gmail SMTP STARTTLS on :587, login
with `EXPRESS_EMAIL_FROM` + `GMAIL_APP_PASSWORD`). Env constants live in `src/express.py`
(`ENV_TO/ENV_FROM/ENV_PASSWORD`).

**Approach — gitignored env file sourced by the wrapper** (keeps the secret out of git *and* the
plist):

1. Secret file *outside the repo*, created by the user so the app password never enters chat/logs:
   `~/.config/second-brain/express.env`, `chmod 600`:
   ```sh
   export EXPRESS_EMAIL_TO="you@gmail.com"
   export EXPRESS_EMAIL_FROM="you@gmail.com"
   export GMAIL_APP_PASSWORD="................"   # 16-char Google App Password, not the login pw
   ```
2. One guarded source line in `dream_cycle_scheduled.sh`, just before the `express_push.py` call
   (references only the *path* — safe to commit + push both remotes):
   ```sh
   [ -f "$HOME/.config/second-brain/express.env" ] && set -a && . "$HOME/.config/second-brain/express.env" && set +a
   ```
   Sourced here (not in `job_wrapper.sh`) to scope the creds to the one job that needs them.

**Why not plist `EnvironmentVariables`:** it would force the secret into the installed plist and
risk it diverging into / leaking through the committed `scheduling/` copy.

**Verification (sends real mail — do with the user present, explicit go):**
1. `express_push.py --dry-run` → proves compose+render, no send.
2. Inspect `should_push()` on the latest run; if it trips (cross-project/contradiction),
   `express_push.py --force` sends a real email; otherwise do a one-off direct `send_email()` test
   so delivery is confirmed regardless of the gate.
3. Confirm inbox delivery; optional `launchctl kickstart` to prove the scheduled path end-to-end.

**Caveat:** `should_push` is intentionally selective (decision #6) — email fires only on days with
a new cross-project synthesis or contradiction. Selective by design, not a misconfiguration.
