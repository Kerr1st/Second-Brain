# Model Backends — pluggable execution paths (agentic CLIs + direct APIs)

> **Status: IMPLEMENTED (uncommitted).** Built + wired into the orchestrator; full suite green (685). Date: 2026-06-15 (status updated 2026-06-16).
>
> **Adapter status (2026-06-16, spec `model-backend-adapters`).** The two
> agentic-CLI adapters are now **implemented and registered**:
> - **`ClaudeCodeInvoker`** (`src/backends/claude_code.py`, `claude -p`) — built
>   on the shared `AgenticCliInvoker` base, registered in the resolver as
>   `claude_code`. **Live-verified** (tool-less path) on the local enterprise-managed build.
> - **`CodexInvoker`** (`src/backends/codex.py`, `codex exec`) — built on the
>   same base, registered as `codex`. Verified by **mocked-subprocess unit
>   tests** plus a **manual smoke-test checklist** (Codex is not installed
>   anywhere today). Checklist location: **`docs/MODEL-BACKENDS-VERIFICATION.md`
>   Part B** — run it on a Codex-equipped box.
>
> Full suite **green (814 passed)** on the `laptop` profile (default, all-Kiro —
> zero behavior change; the ~688 pre-feature baseline is unchanged) and the
> adapter/resolver/probe suites pass under the `claude_code` (`mini`) profile
> against the mocked adapters. The embedding path (Bedrock Titan, `src/embeddings.py`)
> is **untouched** (Req 20.3). Per-environment verification is recorded in
> **`docs/MODEL-BACKENDS-VERIFICATION.md`**.
>
> > **Agentic Claude Code path (tools=True) — FIXED (spec `claude-code-stream-json-probe-fix`).**
> > The tool-less Claude Code path (evaluators / Express) was always
> > **live-verified and working**. The **agentic** path (Explorer / Thinker,
> > `tools=True`) is now **fixed**: previously `--output-format json` did not
> > expose the tool-use/tool-result transcript the `MCP_Startup_Probe` scans, so
> > an agentic Claude Code call raised a **false-positive `RuntimeError`** even
> > when MCP tools attached and were used successfully. The adapter now emits
> > **`--output-format stream-json --verbose`**, extracts `.result`/`is_error`/
> > `usage` from the terminal `{"type":"result"}` event of the JSONL stream, and
> > feeds the stream events to `detect_tool_result` so the probe confirms the
> > real `tool_result`. The compounding **Finding 2** defect (the safety-tuned
> > model refused the injection-styled probe instruction) is also fixed:
> > `MCPStartupProbe.instruction()` was reworded to a transparent
> > natural-language request (the `PROBE_NAME`/`TOOL`/`QUERY` tokens are
> > retained). See `docs/MODEL-BACKENDS-VERIFICATION.md` **Finding 1** and
> > **Finding 2**. The Codex adapter and the embedding path are unaffected.
>
> **rev. 2026-06-15c — incorporates doc review.** Generalized from the original two-implementation
> sketch (`KiroInvoker` + `BedrockInvoker`) to a **backend-family abstraction** so each dream-cycle
> agent role can run on a chosen model via any of several execution paths — the existing **Kiro
> CLI**, **Claude Code**, or **Codex** (agentic CLIs that speak MCP), or a **direct provider API**
> (Bedrock). Driver: **deployment portability** (run the brain on whatever a machine has). Realizes
> `docs/FABLE5-THINKER-PLAN.md` architecture #1 + #3.
> **Review deltas (2026-06-15):** (a) corrected the Mini "unattended-SSO" claim — `awsAuthRefresh`
> is *mostly*-unattended, not hands-off; (b) reconciled the Mini cost posture (evaluators → cheap
> model, not Opus-on-Bedrock); (c) escalated the Explorer port from "any agentic CLI" to a
> validation-gated, **Mini-critical-path** risk; (d) build is **demand-driven, coupled to the first
> real machine** — not adapters-up-front; (e) added failure-mode parity, a metrics real-vs-estimate
> field, and a portability-vs-experiment-integrity split.
>
> **rev. 2026-06-15d — MVP scope: one model per instance, built as per-machine profiles.** The
> selectable unit is a **named profile** in committed `config/backends.toml`, picked by
> `SECOND_BRAIN_PROFILE` (unset => the `laptop` default that reproduces today) — *not* a per-role
> routing matrix. For the MVP each profile sets **one backend + one model** uniformly across roles
> (only effort varies per role, as today); per-role model selection and evaluator-panel **diversity
> are explicitly deferred** (see *Deferred to a later feature*). The whole-instance backend must be
> **agentic** ({Kiro, Claude Code, Codex}); a tool-less Direct-API backend (Bedrock) **cannot run a
> whole instance** (the Explorer needs live tools) and stays a per-stage-only option — enforced by the
> `assert_backend_supports_role` guard (today it fires when the Explorer is resolved; the resolver
> should add an eager check at construction so a Direct-API instance profile fails fast). The
> `Invoker` interface, capability registry, and the per-role profile *structure* are unchanged — only
> the policy is "one model per instance," keeping the door open for per-role diversity later.

## TL;DR

- **One `Invoker` interface, two backend *families*:**
  - **Agentic CLI backends** — shell to a coding-agent CLI that runs non-interactively, **speaks
    MCP**, selects a model, returns parseable output. Adapters: **`KiroInvoker`** (today; `kiro-cli`
    → Amazon Q, **$0 metered**), **`ClaudeCodeInvoker`** (`claude -p`), **`CodexInvoker`**
    (`codex exec`).
  - **Direct API backends** — one **tool-less, single-shot** call. Adapter: **`BedrockInvoker`**
    (`bedrock-runtime` `InvokeModel`, metered). `AnthropicInvoker`/`OpenAIInvoker` possible later.
- **MVP selection: one backend + one model per instance** — a per-machine config toggle, validated
  by the backend **registry** of capability flags (`supports_mcp`, `metered`, `structured_output`,
  `reports_usage`). Internally the role→backend map still exists but **defaults every role to the one
  configured backend**; per-role routing/diversity is deferred (see *Deferred to a later feature*).
- **Deterministic Python orchestrator unchanged** — only per-call *execution* becomes pluggable.
- **Default = today:** every role → `{backend: kiro, model: claude-opus-4.8}`. **Zero behavior
  change** until the map is edited.
- **Hard constraint (reframed + caveated):** the **Explorer requires an *agentic* (MCP-capable)
  backend** (Kiro / Claude Code / Codex), never a tool-less Direct-API backend (= deferred **B1**).
  **But MCP-capability is necessary, not sufficient** — porting the Explorer to a *new* CLI is a
  validation-gated change (see *Explorer port*), not a drop-in.

## Why we're doing this

1. **Deployment portability (primary).** Run on machines without Kiro — a Claude-sub box (Claude
   Code), an OpenAI box (Codex), an offline box (Codex `--oss` local), or an AWS-only box (Claude
   Code on Bedrock, or direct Bedrock). The role→backend map is the **per-machine deployment knob**.
2. **Model diversity for the evaluator panel — DEFERRED (future feature).** Same-model evaluators have
   correlated errors (PoLL; "Correlated Errors Undermine LLM Evaluation Panels"), so diversity *could*
   strengthen the ≥3/4 vote — but it needs per-role routing, so it is **out of MVP scope** and tracked
   under *Deferred to a later feature*. No regression: the panel is 4× one model today and stays so.
3. **Enable models one backend can't carry** (Fable 5; GPT‑5.x) on specific roles.
4. **Per-role experimentation** — config + replay harness, not a code change. (Hold the backend fixed
   when experimenting — see *Portability ≠ experiment integrity*.)
5. **Real cost + reproducibility** — Direct-API and structured CLI modes give real `usage`.
6. **Cost control by design** — keep the call-volume hog (evaluators) on a cheap/free path.

## What stays fixed (locked tenets — unchanged)

- **Deterministic Python orchestration** (star topology; no agentic controller).
- **4-evaluator independent BFT consensus (≥3/4) is the quality gate.** No model grades its own
  output. (Model diversity *may* strengthen it — a **deferred, measured A/B, not an assumed win**.)
- **The Explorer stays agentic** (now: on any MCP-capable CLI backend). B1 remains deferred.
- **Default-preserving** — nothing changes until the role→backend map is edited.

## The agents and their backend eligibility

| Agent | Live tools (MCP)? | Today runs via | Eligible backends |
|---|---|---|---|
| **Explorer** | **Yes** (agentic search) | Kiro → Amazon Q | Any agentic CLI — **but each new CLI is a validation-gated port, not a drop-in** (crown-jewel risk). Never Direct-API (= B1). |
| **Thinker** | Yes today; **No with the packet** | Kiro → Amazon Q | Any agentic CLI; **+ Direct-API** once fed a pre-fetched packet |
| **Evaluators ×4** | **No** (already tool-less) | Kiro → Amazon Q | **All** — any agentic CLI *or* Direct-API (drop-in) |
| *(Express)* **Editor** | No | Kiro → Amazon Q | All (delivery layer) |

The split falls out of **tool use**: only the Explorer needs a live tool-loop. Tool-less stages port
freely; the Explorer's *capability* eligibility is broad but its *behavioral* equivalence per CLI is
not assumed.

## Backend capability matrix (verified 2026-06-15 against official CLI docs)

| | **Kiro** (`kiro-cli chat --no-interactive`) | **Claude Code** (`claude -p`) | **Codex** (`codex exec`) | **Bedrock direct** (boto3 `InvokeModel`) |
|---|---|---|---|---|
| Family | Agentic CLI | Agentic CLI | Agentic CLI | Direct API |
| MCP / live tools | ✅ agent-config `mcpServers` | ✅ `--mcp-config <json>` (+`--strict-mcp-config`) | ✅ `config.toml [mcp_servers.*]` (or `-c`) | ❌ tool-less by design |
| System prompt | agent-config `prompt` | `--system-prompt[-file]` (replace) / `--append-system-prompt` | `developer_instructions`/`instructions`/`model_instructions_file` (or prepend) | inline |
| Model select | `--model <id>` | `--model opus\|sonnet\|haiku\|fable\|<id>` | `-m <id>` / `model_provider` | inference-profile ID |
| Effort / thinking | `--effort low…max` | `--effort low\|medium\|high\|xhigh\|max` | `model_reasoning_effort none…xhigh` (`-c`) | `output_config.effort` (Fable) |
| Structured output | ❌ (scrape) | ✅ `--json-schema` | ✅ `--output-schema` | request/response JSON |
| Final-text extraction | stdout (scrape) | `--output-format stream-json --verbose` → terminal `{"type":"result"}` event `.result` | `-o <file>` or `--json` events | response body |
| Real token `usage` | ❌ (char/4 estimate) | ✅ envelope (`usage`,`total_cost_usd`) | ✅ `--json` events | ✅ response |
| Fail-loud if MCP didn't attach | `--require-mcp-startup` (exit 3) — **strong** | envelope `is_error`/tool-use check — **weaker** | `mcp_servers.<n>.required=true` — **strong** | n/a |
| Cost cap knob | — | `--max-budget-usd`, `--max-turns` | per-call `usage` + budget | per-call `usage` + budget |
| Auth / metering | Amazon Q login — **$0 metered** | Claude sub (flat) / API (`ANTHROPIC_API_KEY`) / **Bedrock** / Vertex / Mantle | ChatGPT (flat) / `OPENAI_API_KEY` / custom / `--oss` local ($0) | AWS creds — **metered** |
| Sandbox caveat | — | permission modes; `--bare` faster scripted start | **sandboxes by default** — needs `--sandbox workspace-write` + `network_access=true` so the MCP server reaches Postgres/Bedrock | — |

> **Build-time verification still required.** These are from the official references (Claude Code CLI
> reference + Bedrock page; Codex `exec`/config reference). Re-confirm each flag against the target
> machine's actual `--help`/version when an adapter is built — versions drift, and the local `claude`
> here is an **enterprise-managed build** whose auth/metering may differ from public Claude Code.

## Architecture

### 1. The `Invoker` interface (common contract)

```
class Invoker(Protocol):
    def invoke(
        self,
        system_prompt: str,
        user_message: str,
        *,
        tools: bool = False,             # was `mcp_config: str|None` — a Kiro-ism (truthy=attach).
                                         # Direct-API backends require tools=False.
        effort: str | None = None,
        timeout: int = 300,
        stage: str | None = None,
        run_id: str | None = None,
    ) -> dict:        # {"output": parsed, "raw": str, "usage": dict|None, "usage_source": "real"|"estimate"}
        ...
```

Two interface fixes the multi-backend view forces (do them in the refactor, not later):
- **`mcp_config: str|None` → `tools: bool`.** Today any truthy `mcp_config` just attaches the
  second-brain server; the value is ignored. That's a Kiro-ism; make the boolean explicit.
- **Return `usage_source`.** Kiro = `estimate` (char/4); Claude Code / Codex / Bedrock = `real`. Without
  this, the cost budget and the Fable A/B silently compare estimates against measurements.

`AgentInvoker.parse_json_output` (recovers the largest balanced JSON payload from prose/ANSI/tool
transcripts) is **backend-agnostic** and stays the common fallback even where a backend offers
schema-validated output.

### 2. Adapters (per-backend differences, isolated)

A thin base (`AgenticCliInvoker`) owns the shared mechanics already in `AgentInvoker` — subprocess
spawn, timeout, lenient decode, metrics, cleanup — and **one contract every adapter MUST honor:**

> **Failure-mode parity (hard requirement).** Every adapter maps its CLI's failures onto the *same*
> exceptions: infra failure → `TimeoutError`/`RuntimeError`; never a silent empty/`REJECT`. This is
> what the evaluator retry/abort integrity fix relies on (a crashed evaluator must not become a fake
> REJECT). Per backend: Claude Code `is_error` envelope + nonzero exit; Codex exit 1; Bedrock
> throttling/`ValidationException`/timeout. Tested per adapter.

Each adapter overrides only: command construction, system-prompt delivery, MCP config, final-text
extraction, the effort flag, and **failure-mode mapping**.

- **`KiroInvoker`** (today's `AgentInvoker`, unchanged). `kiro-cli chat --no-interactive --agent
  <name> --model <id>` (`--trust-all-tools --require-mcp-startup` with tools; `--effort`). `usage=None`.
- **`ClaudeCodeInvoker`.** Tool-less: `claude -p "<msg>" --system-prompt-file <role>.txt
  --strict-mcp-config --tools "" --model <id> --effort high --output-format stream-json --verbose`.
  With tools: add `--mcp-config <sb>.json --permission-mode bypassPermissions`. Read the terminal
  `{"type":"result"}` event of the JSONL stream for `.result` → `parse_json_output`, `is_error`, and
  `usage`/`total_cost_usd`; bound with `--max-budget-usd`. Fail-loud = inspect `is_error` + confirm
  a real `tool_result` in the stream events via the `MCP_Startup_Probe` (no exact
  `--require-mcp-startup` analog — weaker).
- **`CodexInvoker`.** Tool-less: `codex exec "<msg>" -m <id> -c model_reasoning_effort="high"
  -c developer_instructions="<role>" --sandbox read-only --output-last-message <out>.txt`. With tools:
  config `[mcp_servers.second_brain] command=<py> args=["-m","src.mcp_server"] cwd=<repo>
  required=true`, `--full-auto --sandbox workspace-write` + `sandbox_workspace_write.network_access
  =true`. `required=true` = the clean `--require-mcp-startup` analog.
- **`BedrockInvoker`** (Direct API; from the original plan). `InvokeModel` with an inference-profile
  ID (cross-region `us.`/`global.` prefix; IAM `bedrock:InvokeModel`+`GetInferenceProfile`). Asserts
  `tools=False`. Anthropic Messages (Claude/Fable) + OpenAI Responses (GPT on Mantle); Fable
  constraints (no `temperature`; thinking block; `refusal`); inline packet; `usage` capture.

### 3. Backend selection — per-machine profiles (one model per instance)

A named **profile** in committed `config/backends.toml` gives each role its `(backend, model, effort)`;
`SECOND_BRAIN_PROFILE` picks the active profile (unset => `default_profile`, the `laptop` profile that
reproduces today exactly). For the MVP each profile sets **one backend + one model** uniformly across
roles (only effort varies per role); the per-role structure stays, so per-role diversity is later a
config-only change (see *Deferred to a later feature*).

```
# config/backends.toml (committed) — laptop reproduces today; mini = Claude Code on Bedrock.
default_profile = "laptop"

[profiles.laptop]                         # all-Kiro / Opus 4.8 ($0 metered)
explorer = { backend = "kiro", model = "claude-opus-4.8", effort = "high" }
thinker  = { backend = "kiro", model = "claude-opus-4.8", effort = "max"  }
# skeptic / advocate / epistemologist / methodologist = kiro / opus / high

[profiles.mini]                           # Claude Code on Bedrock (implemented + validated)
explorer = { backend = "claude_code", model = "global.anthropic.claude-opus-4-8", effort = "high" }
# ... one backend + one model across all roles (Bedrock inference-profile id the CLI accepts)
```

`Resolver.invoker_for(role)` looks up the role's spec, runs `assert_backend_supports_role` (the
Explorer-needs-tools guard), then returns a cached invoker per `(backend, model)` — so an all-Kiro
profile shares one `KiroInvoker` across roles, exactly like today; an unimplemented backend raises
`NotImplementedError`. Effort is carried **per role in the profile** (`RoleBackend.effort`) and passed
to each `invoke()`. Credentials are never in the TOML — they stay out-of-band/gitignored.

> **Guard timing:** the guard fires *when the Explorer is resolved* (lazy). The resolver should also
> validate the active profile's Explorer backend **at construction**, so a Direct-API instance profile
> fails fast at config load rather than mid-run.

### 4. Orchestrator change (small, contained)

`self.invoker = AgentInvoker()` → `self._invoker_for(role)`. Sequence, prompts, parsing, consensus
tally (incl. the new evaluator retry/abort logic), storage, digest **untouched**.

## Deployment portability ≠ experiment integrity (keep these separate)

Two distinct uses of this abstraction that must not be conflated:

- **Portability:** run the brain on whatever a machine has. Mixing backends across roles is fine.
- **Experiment integrity:** for the Fable A/B and any panel-diversity study, **hold the backend
  fixed** and vary only the model. Each CLI is its own unpinned harness (the kiro-drift concern ×3);
  a panel mixing Skeptic-on-Claude-Code vs Advocate-on-Codex confounds *model* with *harness*. Anchor
  every experiment to one reference backend (Kiro/Opus today); use direct-Bedrock for the cleanest
  model-only comparison. Portability is a deployment property, not an experimental control.

## Deployment profiles

Set per machine from what's installed + authenticated there.

| Machine | What it has | Recommended map |
|---|---|---|
| **This laptop** (enterprise-managed) | `kiro-cli` + Amazon Q | All-Kiro / Opus 4.8 — **today's default, $0** |
| **Mac Mini** (always-on) | AWS creds; install Claude Code (enterprise-managed `claude` present here) | **Whole instance on Claude Code → Bedrock, one model** (Opus-class). Per-role cost tiering (cheap evaluators) is deferred. See *Mini notes*. |
| **Box with a Claude Max sub** | `claude` (flat) | Claude Code everywhere — flat cost; the **instance model** can be `fable` if desired |
| **Box with ChatGPT/OpenAI** | `codex` | Codex everywhere; or **Codex `--oss`** local for offline/$0 (one model for the whole instance) |
| **AWS creds only, no agent CLI** | boto3 | Bedrock runs tool-less stages only; the Explorer needs a *validated* agentic-CLI port — so Bedrock is **not** a whole-instance backend |
| **Diverse panel** (experiment) | — | **Deferred** — needs per-role routing (see *Deferred to a later feature*). |

### Mini notes (corrected from the earlier draft)

- **AWS Bedrock creds are required on the Mini regardless of agent backend** — embeddings
  (`src/embeddings.py`, Titan) always call Bedrock directly. This *favors* Claude Code on Bedrock:
  one credential set serves both embeddings and the agent calls.
- **Unattended credentials (the corrected story).** `CLAUDE_CODE_USE_BEDROCK=1` + `awsAuthRefresh`
  gives **mostly-unattended** operation: token refresh is silent *within the SSO session window*
  (days–weeks if the session is configured for it), then a human must re-auth (`aws sso login` is
  browser/device-code; Claude Code's own docs warn of SSO auth-loops). **Truly hands-off needs a
  dedicated Bedrock-invoke-only IAM principal** (rotated keys) or the **long-term Bedrock API key**
  (`AWS_BEARER_TOKEN_BEDROCK` — simplest, but it creates an IAM user and AWS recommends it "only for
  exploration"; short-term keys are ≤12h). Pin `ANTHROPIC_DEFAULT_OPUS_MODEL='us.anthropic.claude-opus-4-8'`.
  Pick per security tolerance: IAM principal (robust) ▸ long-term key (simple, weaker) ▸ SSO+refresh (periodic re-auth).
- **The Explorer port is the Mini's critical path.** No Kiro on the Mini ⇒ the full cycle requires
  porting the Explorer to a non-Kiro agentic CLI — the **riskiest** change here. Gate it on a
  slice-quality validation (below), not "it speaks MCP." Until validated, an interim option is to run
  the Explorer where Kiro exists and the rest on the Mini.
- **Cost under one model (MVP).** The whole instance — Explorer + Thinker + all ~30 evaluator
  calls/run — runs on the one configured model. On Opus-via-Bedrock the evaluator volume alone is
  ≈ **$0.73/run → ~$22/mo** (FABLE5 verified figures: 32 calls, ~49.5K in / ~19.1K out; verify current
  Bedrock pricing), plus the Explorer/Thinker calls. **Accepted for the MVP.** Per-role cost tiering
  (cheap evaluators, Opus only on synthesis) is the first payoff of the deferred per-role feature if
  the bill matters.

## Guardrails & cost controls (generalized)

- **Explorer → agentic backend only** (resolver rejects non-MCP); **and a new CLI requires the
  slice-quality validation before production use.**
- **Tool-less assertion** for Direct-API; explicit no-tools for tool-less CLI stages.
- **MCP fail-loud, per backend** (Kiro `--require-mcp-startup`; Codex `required=true`; Claude Code
  envelope check). Preserves the Jun-8–12 silent-outage fix.
- **cwd discipline carries over** — every agentic backend spawns `python -m src.mcp_server` with
  **cwd=repo** (Kiro agent `cwd`, Claude `--mcp-config` server entry, Codex `mcp_servers.*.cwd`).
- **Failure-mode parity** (above) — infra failure never becomes a fake verdict, on any backend.
- **Per-run cost budget** from real `usage` (now on three backends), with auto-downgrade + kill
  switch (revert map to all-Kiro / $0).
- **Effort pinned per role**; **`usage_source` recorded** so estimates and measurements aren't mixed.
- **Judge quality > judge cost.** Diversity de-correlates errors **only among capable judges**. Free
  is fine via Kiro's *strong* non-Anthropic catalog (DeepSeek/GLM/Qwen) or a flat-rate sub; **small
  local OSS models as evaluators are a false economy** — they add noise to the gate, not signal.

## Deferred to a later feature (explicitly out of MVP scope)

These need **per-role routing** and are intentionally postponed; the `Invoker` interface + the
internal role→backend map keep the door open, so they are additive, not a redesign:

- **Per-role model selection** (e.g. cheap evaluators + Opus synthesis) — the first cost lever for a
  metered instance like the Mini.
- **Evaluator-panel model diversity** — different models/labs per seat to de-correlate the ≥3/4 vote.
  A measured A/B (hold the backend fixed), not an assumed win; no regression vs today's same-model panel.
- **Additional adapters** — ~~`CodexInvoker`~~ (now implemented — see *Adapter status* above),
  direct `OpenAIInvoker`/`AnthropicInvoker`, and
  `BedrockInvoker` (Fable Phase 1b) — added when a deployment profile actually needs them.

## Rollout (demand-driven — coupled to the first real machine)

The abstraction is cheap and right; **over-building it before a concrete deployment is the risk.**
The refactor also touches the just-stabilized `orchestrator.py`/`agent_invoker.py` (the cwd outage
tests missed, plus the in-flight evaluator-integrity fix), so it earns its risk only alongside a real
second backend.

- **Step 0 — evaluator-integrity fix committed** (`e686284`); full suite green (**683**, incl. the
  backend-contract scaffold) for a clean base.
- **Step 1 — paper-validate the `Invoker` interface** against all four real command surfaces
  (already gathered) so the Kiro refactor doesn't bake in Kiro-isms (the `tools: bool` + `usage_source`
  fixes above).
- **Step 2 — couple the abstraction to the Mini.** When the Mini is actually stood up, ship
  {interface + registry + role→backend map + `KiroInvoker` refactor + **`ClaudeCodeInvoker`**} as one
  validated unit. Two implementations prove the interface; the Mini is the reason to touch the
  conductor. Gate: full suite green with the default (all-Kiro) map = zero behavior change.
- **Step 3 — validate the Explorer port** (the Mini critical path): stratified/crown-jewel replay
  (reuse the Fable 1a harness) confirming Claude Code produces comparable slices before it's the
  scheduled Explorer.
- **Defer `CodexInvoker`** until a box needs it (not installed anywhere today), and **`BedrockInvoker`**
  to its own metered step (Fable Phase 1b; gated by the data-sharing decision).

> **Open decision:** ship the abstraction now (decoupled) vs. couple it to the Mini (Step 2). I lean
> **couple** — minimal/surgical cuts both ways: don't build adapters speculatively *and* don't
> refactor the freshly-stabilized conductor speculatively. Your call.

## Testing & verification

- **Default-preserving:** existing suite passes unchanged with the all-Kiro map.
- **Per-adapter unit tests** (mock subprocess/`boto3`): command construction, system-prompt delivery,
  MCP-config emission, tool-less enforcement, final-text extraction, `usage`+`usage_source` capture,
  and **failure-mode parity** (each CLI's failures → the right exception, never a fake verdict).
- **Resolver/guard tests:** `(backend, model)` → invoker; Explorer + non-MCP → guarded error.
- **Cross-backend parse:** reuse the `parse_json_output` property tests per backend envelope.
- **Per-backend prompt validation:** the dense Thinker 12-field schema + strict-JSON evaluator
  prompts were tuned on Opus; confirm GPT‑5/DeepSeek/Qwen/local honor them (structured-output flags
  help only on some backends). Format-robust ≠ instruction-following-robust.
- **Explorer slice-quality validation** before any new-CLI Explorer goes live.

## Risks / open items

- **Explorer port (highest).** "MCP-capable" ≠ behaviorally equivalent; different harness/tool-call
  conventions. It's the crown-jewel stage and the Mini's critical path. Validate slice quality.
- **Failure-mode parity** must be implemented per adapter or the evaluator-integrity guarantee breaks
  silently on that backend.
- **Metrics confound** — record `usage_source`; never compare estimate vs real across backends.
- **Experiment integrity** — hold the backend fixed for A/Bs; portability mixing is for deployment.
- **Judge quality** — weak local models degrade the BFT gate; keep evaluators on capable models.
- **System-prompt delivery** — Kiro `prompt` / Claude `--system-prompt-file` / Codex
  `developer_instructions`(or prepend). Native where available; prepend as universal fallback; test it.
- **Prompt portability** — per-backend prompt validation (above).
- **Codex sandboxing** can block the MCP server's DB/Bedrock calls — require `workspace-write` +
  `network_access=true`; assert it.
- **Secrets per profile (unattended)** — each headless box needs a creds provisioning/rotation story;
  the **dedicated Bedrock-invoke-only IAM principal** is the robust one (vs the AWS-discouraged
  long-term key). Embeddings need Bedrock creds regardless.
- **enterprise-managed `claude`** — the locally-installed build is enterprise-managed; verify its auth/metering on the
  Mini before relying on the Claude-Code-on-Bedrock profile.
- **Response-shape normalization** (Anthropic Messages vs OpenAI Responses) — net-new in `BedrockInvoker`.
- **Data-sharing / governance** — Bedrock under AWS governance; Fable under `provider_data_share`; a
  third-party SaaS sub sends memory content off-AWS. **Gate before real memory content reaches a
  metered/off-AWS backend** (Fable plan); tag-filtered "sensitive stays on Kiro/Bedrock" carve-out.

## Relationship to other docs

- **`docs/FABLE5-THINKER-PLAN.md`** — realizes architecture #1 (Bedrock backend) + #3 (route a role).
  Fable is now one `(backend, model)` choice — via `ClaudeCodeInvoker --model fable` (incl. Bedrock)
  or `BedrockInvoker` (`global.anthropic.claude-fable-5`). 1a/1b, the diverse panel, and the
  Opus-via-Bedrock control arm are *consumers* — and must stay on a fixed reference backend.
- **`docs/DREAM-CYCLE-MCP-DECOUPLING.md`** — B1 stays deferred; the cwd / fail-loud-on-MCP lessons
  carry to every adapter.
- **the private host-migration runbook** — the migration that *consumes* this refactor; owns the DB move,
  capture locality, interactive MCP, backups, and cutover/rollback. The role→backend map is the
  per-machine knob; Claude Code on Bedrock is the recommended Mini profile but **reduces, not
  eliminates**, the unattended-creds problem, and the **Explorer port is the critical path**.
- **`docs/EXPRESS-PLAN.md`** — the Express editor is tool-less; any backend; not a priority.

---

## Implementation plan & session decisions — 2026-06-15 (build kickoff)

> Appended to the design proposal above. This section is the **living plan** for implementing the
> backend refactor — **the execution seam only**. **Scope:** this doc owns *how the dream cycle runs
> on a non-Kiro backend*. The **migration** of the Second Brain to the Mac Mini (DB move, capture
> locality, interactive MCP, the other jobs, backups, cutover/rollback) lives in
> the private host-migration runbook, which *consumes* this refactor as a prerequisite. The Mini is the
> motivating first consumer, not the subject of this doc.

### Decision — the Mini is the motivating first consumer (migration specified separately)

- The first real deployment that needs a non-Kiro backend is the **Mac Mini** (no Kiro/Amazon Q
  there). The migration itself — DB continuity, capture locality, cutover/rollback, the other jobs —
  is owned by the private host-migration runbook, not here.
- **The seam this doc owns:** even a "same data, same model" move crosses **Kiro →
  Claude-Code-on-Bedrock**, a different execution path (system-prompt delivery, sampling, tool-loop).
  The move is a backend change regardless of model — which is *why* this refactor is the migration's
  prerequisite.

### Decision — build the abstraction for all backends; implement adapters in priority order

- The `Invoker` interface + capability registry + resolver + role→backend map are designed to support
  **all** targets: **OpenAI, Anthropic, Kiro CLI, Claude Code, Codex, Amazon Bedrock**.
- Adapters are **implemented in priority order**, not all at once: **KiroInvoker** (today) →
  **ClaudeCodeInvoker-on-Bedrock** (the Mini's path) → Codex / OpenAI-direct / Anthropic-direct /
  Bedrock-direct as a deployment profile actually needs them. The rest slot in as drop-in adapters
  with no redesign. (Keeps the architecture open without writing speculative code.)

### Decision — default-preserving at every step

- Until the role→backend map is edited, every role stays **all-Kiro / Opus 4.8**, and the existing
  **685-test suite must pass unchanged** after the refactor. The live cycle cannot regress while the
  abstraction lands.

### Decision — parity is the MVP; diversity is deferred

1. **Swap with parity** — validate the chosen backend (Claude-Code-on-Bedrock) running the **same one
   model (Opus)** before anything else: identical slices through Kiro-Opus vs ClaudeCode-Bedrock-Opus,
   decisions matching within tolerance on the replay harness. This is the whole MVP **and** the
   migration's acceptance gate (tolerance + fallback in the private host-migration runbook).
2. **Panel model diversity is a deferred, separate feature** (it needs per-role routing) — and when it
   is taken up, never move host+backend *and* change panel composition in one step, or quality changes
   can't be attributed.
- **The replay harness is the common keystone** for migration-parity, the (deferred) diversity A/B,
  *and* Fable (it is the Fable-plan "task 0 = slice-composition persistence"). Build it once; it serves
  all three.

### Config mechanism (built 2026-06-15 — committed per-machine profiles)

- **Committed `config/backends.toml`** holds named profiles (`laptop` reproduces today's all-Kiro/Opus
  exactly; `mini` = claude_code on Bedrock, adapter pending). `SECOND_BRAIN_PROFILE` selects the active
  profile (unset => `default_profile = "laptop"`), so a machine sets one env var — no code change.
  **Credentials stay out-of-band/gitignored**, never in the TOML. For the MVP each profile is uniform
  in backend+model ("one model per instance"); the per-role structure is retained for later diversity.

### Build sequence (bounded, de-risked)

1. **[done]** Ground the refactor (read-only) — findings below.
2. `Invoker` interface + capability registry (`supports_mcp`, `metered`, `structured_output`,
   `reports_usage`) + invariants.
3. Refactor `AgentInvoker → KiroInvoker` behind the interface (behavior-identical) + resolver +
   committed `config/backends.toml` profiles (`laptop` default) selected by `SECOND_BRAIN_PROFILE`.
4. Wire orchestrator to `_invoker_for(role)` + record `(backend, model, effort)` provenance per
   run/candidate.
5. Verify default-preserving (full suite green, all-Kiro map).
6. `ClaudeCodeInvoker` (Bedrock) + fail-loud/startup-probe/usage + mocked-subprocess unit tests.
7. Slice-composition persistence + parity harness (Kiro-Opus vs ClaudeCode-Bedrock-Opus on identical
   slices).
8. Empirical backend smoke-test (needs Claude Code present). *(The Mini cutover runbook lives in
   the private host-migration runbook.)*

**Laptop vs Mini build split (practical constraint):** this laptop has Kiro, not Claude Code. Steps
2–7 (interface, registry, resolver, KiroInvoker refactor, orchestrator wiring, provenance,
slice-persistence, ClaudeCodeInvoker *code* + mocked tests) are **buildable here**. The **empirical**
verification (tools attach, fail-loud works, parity vs Kiro) must run **where Claude Code lives** — the
Mini or a Claude-Code install. So: build with mocks here, verify on the box that has it.

### Grounding findings (step 1 — read of `src/agent_invoker.py` + orchestrator wiring)

- **Model is set at construction** (`AgentInvoker(model=...)`); the orchestrator uses **one shared
  invoker for all roles** (`self.invoker = AgentInvoker()`). → per-role models = the **resolver/cache
  pattern** (one cached invoker per `(backend, model)`); **effort stays per-call** (already an
  `invoke()` arg).
- `invoke()` returns `{output, raw}` today — **`usage` is the one new return field** (Kiro `None`;
  Claude Code / Bedrock populate it).
- The **outage-class hardening lives inside `AgentInvoker`**: the cwd fix (`_SECOND_BRAIN_MCP`
  `cwd=repo`), `--require-mcp-startup`, the MCP-startup retry, the Phase-0 metrics. These move into
  `KiroInvoker` intact and are **re-expressed per adapter** — fail-loud-on-MCP is now a per-backend
  obligation.
- `mcp_config` is effectively a **tool-access flag** today (truthy → attach the hardcoded second-brain
  MCP); each adapter renders that MCP in its own format.

### Design considerations carried forward (from the build-kickoff review)

- **Decision provenance** — record `(backend, model, effort)` on the candidate/run record. A
  now-machine-dependent gate must stay auditable across the cutover (laptop-Opus era → Mini era).
  Model+effort are already in the per-call metrics JSONL but **not** on the decision record; step 4
  fixes that.
- **Per-model JSON parse-rate feeds the new retry/abort path** (commit e686284): a seat that fails to
  emit clean JSON N% of the time is effectively a *crashing evaluator* N% → more aborted runs. Measure
  parse-success on the replay sample before trusting a model in a seat; set `--json-schema` /
  `--output-schema` where the backend supports it.
- **Diversity is a measured A/B, not an assumed win.** PoLL / "correlated errors undermine LLM panels"
  supports diverse panels beating a single judge *at comparable model quality* — it does **not** promise
  four cheaper diverse models beat four Opus. First A/B = "does *this* diverse config hold accept-rate +
  crown-jewel quality vs all-Opus on the replay sample?"
- **Smoke-test the capability matrix empirically** before trusting the exact CLI flags (the outage
  class: `--require-mcp-startup`, Codex `required=true`, Claude envelope `is_error`, `awsAuthRefresh`).
  Verify-before-trust.
- **System-prompt delivery is a consensus-correctness risk**, not just formatting: the evaluators'
  independence *is* their system prompts. The "prepend to user message" fallback is the weak one.
  Assert role identity survives **per backend** (extend `test_panel_prompts.py` /
  `test_prompt_contracts.py`); prefer native delivery.
- **Sharp invariants:** `metered ⇒ reports_usage` (never run a metered seat you can't meter; Kiro
  reports none but is $0). Codex sandbox-blocks-network is the **same class** as the cwd outage → the
  fail-loud check needs a **startup probe** (one trivial tool call), not just process-attach. Headless
  CLI auth beyond Bedrock (a Claude/ChatGPT *subscription* on a headless box has its own token-expiry
  problem) — fine because the Mini profile is Claude-Code-*on-Bedrock* (AWS creds + `awsAuthRefresh`).

### Open items

- **Confirm the config mechanism** (above) before the KiroInvoker refactor.
- **Effort-level semantics differ across backends** ("high" on Claude ≠ Kiro ≠ Codex) — affects
  production parity, not just experiments; ties to provenance.
- Diversity A/B + remaining adapters (Codex / OpenAI-direct / Anthropic-direct / Bedrock-direct) +
  Fable Phase 1b = follow-on, after the Mini lands on parity.
