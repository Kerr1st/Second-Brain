# Models, providers, and authentication — original implementation

Source-reviewed 2026-09-10; skim-stage additions (the `skim` role, adapter options, Nova 2 Lite / Gemma 3 12B) 2026-09-20. Configured model IDs are recorded below as **configuration values**; this document does not assert current service availability, price, subscription entitlement, or valid credentials.

## Separate the workloads

| Workload | Selection in code | Tool access / auth |
|---|---|---|
| Embeddings for ingestion, writes, search, dedup | `src/embeddings.py`: Bedrock Titan v2, 1024 dimensions | Boto3 credential chain + region; independent of dream-cycle profile |
| Explorer and Thinker | `src/backends/resolver.py`: selected profile's role mapping | Both call `invoke(..., tools=True)` and require live memory tools |
| Four candidate reviewers | Same resolver, one role mapping each | Requests `tools=False`; candidate JSON+source ID text only in original |
| Skim stage (Step 3.5: one structured record per arriving document) | Same resolver, role `skim` — Bedrock-only in every profile | `tools=False` with a JSON schema; receives full document text, so it may never resolve to an agentic CLI; see `docs/SKIM-STAGE.md` |
| Chat distillation/contradiction detection | `AgentInvoker()` from `src/agent_invoker.py` | Alias to `KiroInvoker`, default model; profile is ignored |
| Weekly activity digest | `AgentInvoker()` | Kiro alias; profile is ignored |
| Express editor | `AgentInvoker()` unless explicitly injected | Kiro alias; deterministic fallback, profile is ignored |

**Bedrock access is required for this original implementation's normal embedding-dependent path even when reasoning uses Kiro or Codex.** A tool-less synthesis comparison can replace that boundary, but it is not a full native run. Conversely, “multiple agents” does not itself require Bedrock: the six roles can be separate CLI invocations using a supported configured provider.

## Implemented adapters and configuration

`DEFAULT_ADAPTERS` registers `kiro`, `claude_code`, `codex`, and `bedrock`. The first three are agentic CLIs that speak MCP and can run any agent role. `bedrock` (`src/backends/bedrock.py`, on the `DirectApiInvoker` base) is a tool-less, single-shot call to the Bedrock Runtime Converse API; the resolver refuses it for the Explorer, which needs the live tool-loop, and conversely it is the only backend the resolver accepts for the `skim` role.

`config/backends.toml` defines five profiles (`laptop`, `mini`, `bedrock`, `hybrid`, `controlled`; the last two are described in the file's own comments). The three original profiles, plus the `skim` role that every profile shares:

| Profile | Roles | Backend / configured model | Effort |
|---|---|---|---|
| `laptop` (default) | six agent roles | Kiro CLI · `claude-opus-4.8` | Thinker=max; other roles=high |
| `mini` | six agent roles | Claude Code · `global.anthropic.claude-opus-4-8` | Thinker=max; other roles=high |
| `bedrock` | Explorer | Claude Code · `global.anthropic.claude-opus-5` | high |
| | Thinker | bedrock · `global.anthropic.claude-fable-5-1` | high |
| | Skeptic, Epistemologist | bedrock · `global.openai.gpt-6-astra` | high |
| | Advocate, Methodologist | bedrock · `global.anthropic.claude-sonnet-5` | high |
| every profile | Skim | bedrock · `us.amazon.nova-2-lite-v1:0` · `options = { temperature = 0.0, service_tier = "flex", max_tokens = 4096 }` | low (= Nova 2 extended reasoning off) |

`SECOND_BRAIN_PROFILE` selects a profile. `SECOND_BRAIN_BACKENDS_CONFIG` overrides the file. The resolver checks exactly seven required roles (the six agent roles plus `skim`) and known capability names and caches invokers by backend/model/options. The `bedrock` profile's inference-profile ids were confirmed ACTIVE in the account (us-east-1 and us-west-2) on 2026-09-14; the panel deliberately mixes two model families so the 3-of-4 consensus requires cross-family agreement. The `mini` profile remains a target configuration, not a deployment fact.

### Adapter options and the `skim` role

A role may carry an inline table of adapter constructor arguments: `options = { temperature = 0.0, service_tier = "flex", max_tokens = 4096 }`. Only the `bedrock` adapter accepts options (`ADAPTER_OPTIONS` in `src/backends/resolver.py`): `temperature` (Converse `inferenceConfig.temperature`, 0.0–1.0), `service_tier` (`default` | `flex` | `priority` | `reserved`, Converse `serviceTier.type`) and `max_tokens` (`inferenceConfig.maxTokens`, default 32768). An option on another backend, or an unknown key, is rejected at config load; roles that share backend and model but not options get separate invokers.

The `skim` role (docs: `docs/SKIM-STAGE.md`; model selection: `docs/RESEARCH-skim-model.md`) reads every parent-level arrival in a run's window with a cheap, faithful model and is the same in all five profiles: Amazon Nova 2 Lite on the direct Bedrock API, Flex tier, temperature 0, extended reasoning off, 4096 output tokens. The fallback model is Google Gemma 3 12B IT (`google.gemma-3-12b-it`, structured output via `outputConfig.textFormat`; doc-verified, not live). Because the skim sends full document text to its model, it is Bedrock-only by rule: `assert_backend_supports_role` rejects it on any MCP-capable backend (Kiro, Claude Code, Codex) when the profile is loaded and again when the role is resolved. A profile without the role is rejected at load; the runtime off-switch is `SECOND_BRAIN_SKIM_DISABLED=1` (see `docs/SKIM-STAGE.md`, "Disabling the skim").

### Tool-less Thinker: the context packet

Agentic-CLI Thinkers read memories themselves over MCP and receive only ids and titles. When the Thinker's backend has `supports_mcp=False`, the orchestrator instead delivers the slice contents in the prompt (`src/dream_cycle/packet.py`: per-memory cap 4,000 chars, packet cap 120,000 chars, invalid ids skipped, DB failure degrades to titles only) and invokes with `tools=False`. Those contents are cached for the run, and every evaluator prompt appends the cited sources' text after the existing `Source memories: [...]` line — so the Skeptic can check factual grounding against text rather than UUIDs. On tool-capable profiles nothing changes.

### Panel semantics (since 2026-09-18, `docs/PLAN-panel-acceptance.md`)

Evaluators judge against an explicit Standard — (a) grounded in the cited sources, (b) stated at a scope the sources support, (c) non-obvious and useful, (d) not already in the corpus — and return a graded verdict: `ACCEPT`/`REJECT`, a 0–100 score, and `blocking_issues` / `advisory_issues` lists (`EVALUATOR_VERDICT_SCHEMA`, persisted per seat in `dream_cycle_candidates.evaluator_details`, migration 013). Before the panel votes, the orchestrator runs one grounding audit of each candidate by the Skeptic seat (`_ground_candidate`) and, when it reports blocking issues, one tool-less revision on the Thinker backend (`_revise_candidate`: sources may only shrink, provenance kept in `candidate_json.revision`); the panel then votes on the revision. The audit is skipped once the run has spent 80% of its cap. Consensus stays 3-of-4 across the two model families; there is no REVISE verdict or second panel pass. Live: acceptance went from 1 of 70 candidates to 4 of 6 (run `3a1263a6`) and 6 of 8 (run `05171b48`, with the skim stage), at ~$0.66 per candidate judged.

### Bedrock request rules (live-verified 2026-09-14)

- Claude 5-series (Fable 5.1, Opus 5, Sonnet 5): `additionalModelRequestFields = {"thinking": {"type": "adaptive"}, "output_config": {"effort": <low|medium|high|max>}}`. `thinking.type=enabled`/`budget_tokens` is rejected. Fable 5.1 also rejects forced tool choice, so structured output uses `toolChoice.auto` and validates the reply.
- Claude 4.x keeps the extended-thinking `budget_tokens` mapping.
- OpenAI on Bedrock (`openai.gpt-6-astra` etc.) is served by plain `converse`; depth is `{"reasoning": {"effort": ...}}` (`reasoning_effort` is rejected). Both vendors may emit a `reasoningContent` block, which is ignored.
- Thinking is billed as output and counts against `maxTokens`. The default budget is 32,768 (`BEDROCK_MAX_TOKENS`); a `max_tokens` stop is raised as an error, never parsed as a complete answer.
- Amazon Nova 2 Lite (`us.amazon.nova-2-lite-v1:0`, the skim; live-verified 2026-09-20): forced tool choice `toolChoice: {"tool": {"name": "structured_output"}}` is accepted (Nova and Claude 3 only) and served by constrained decoding; extended reasoning is `additionalModelRequestFields.reasoningConfig` — `{"type": "disabled"}` for effort `low` (and no effort), `{"type": "enabled", "maxReasoningEffort": ...}` for `medium`/`high`; `serviceTier: {"type": "flex"}` is accepted and the response echoes the served tier, which the adapter copies into `usage.serviceTier` for pricing.
- Google Gemma 3 12B IT (`google.gemma-3-12b-it`, skim fallback; doc-verified only): no forced tool choice, so structured output uses the JSON-schema `outputConfig.textFormat` instead. Bedrock's structured-output grammar accepts no `maxLength` / `maximum` keywords (Nova ignores them), so the skim's length limits are enforced by its validator, not the schema.

### Spend guardrails

Every call's real `usage` is priced from the list-price table in `src/backends/cost.py` (unknown models are billed at the ceiling) and accumulated per run. `SECOND_BRAIN_RUN_COST_CAP_USD` (default 5.00) is a hard stop: the run aborts, the run record is closed, and no verdict is fabricated — the exception is not a `RuntimeError`, so the evaluator retry loop cannot swallow it. `SECOND_BRAIN_KILL_SWITCH=1` refuses to start a run. Estimate from the June verified-run token counts: ≈ $1.60/run for the `bedrock` profile before thinking tokens.

The skim has its own sub-cap inside the run cap: `SECOND_BRAIN_SKIM_CAP_USD` (default `SKIM_SPEND_CAP_USD` = $1.00, never more than 15% of the run cap); the runner stops cleanly at the sub-cap and the records already stored still feed the Explorer. Its calls are metered through the same `SpendTracker`, so they count toward the run cap. Skim-model prices (us-east-1 geo/in-region, pricing-page data of 2026-09-17; the Flex rate applies only when the response says the call was served on Flex):

| Model | Standard $/M in / out | Flex $/M in / out |
|---|---|---|
| `us.amazon.nova-2-lite-v1:0` | 0.33 / 2.75 | 0.165 / 1.375 |
| `google.gemma-3-12b-it` | 0.09 / 0.29 | 0.045 / 0.145 (derived: 50% of standard) |

Measured on the first live run (`05171b48`, 2026-09-20): 164 documents for $0.19, about $0.001 per document. Add a `_PRICE_TABLE` entry before pointing the role at any other model — an unknown id is priced at $10/$50 and trips the sub-cap within ~25 documents.

## Invocation lifecycle and failure handling

`AgenticCliInvoker` builds temporary prompt/config inputs, spawns a subprocess, maps timeouts/nonzero exits, extracts/parses JSON, writes per-call metrics/debug output, and cleans temporary configuration. Kiro tool calls use `--require-mcp-startup` and retry exit 3 up to two additional attempts. Claude uses stream-JSON with tool-result probes; Codex supports final-message-file or JSON extraction and a required MCP startup configuration.

Prompt/CLI formats and model aliases depend on installed versions. Earlier live probes are preserved in [the historical verification record](archive/2026-09-10/docs/MODEL-BACKENDS-VERIFICATION.md); they are not current provider verification. Native role prompts are in `src/prompts/{explorer,thinker,panel}.py`; transport differences are in `src/backends/`.

Kiro's tool-enabled config permits `tools=["*"]` and invokes `--trust-all-tools`. The server includes writes as well as reads. The original architecture does **not** enforce a read-only discovery capability solely through role prompts. Isolated evaluation must constrain tools/state explicitly. For Codex, `tools=False` only selects a read-only shell sandbox; it does not clear ambient MCP configuration or prove that all tools are unavailable. Treat reviewer calls as requesting no tools, and verify actual adapter/CLI restrictions before claiming isolation.

Metrics are JSONL in `logs/llm_metrics` (or `KIRO_LLM_METRICS_DIR`). Kiro returns no real token usage, only character-based estimates; other adapters extract available provider counts. Latency, successful parse, and token counts do not measure source accuracy, novelty, or business value. The per-run spend cap and kill switch (see "Spend guardrails" above) are enforced at runtime; they price only calls that report real usage, so Kiro calls contribute nothing to the total.

## Authentication preflight

Check each dependency independently, from the same shell/service account/environment that will run it:

1. **DB:** connection and actual schema/index compatibility. A reachable server does not prove migration completeness.
2. **Embedding provider:** region/profile/identity, then a bounded synthetic embedding request to the exact model. STS success alone does not prove Bedrock model permission or region availability. Never print credential contents.
3. **Reasoning provider:** selected CLI installation/version/authentication, then a bounded synthetic tool-less JSON response.
4. **Tool-using roles:** confirm an actual read-only MCP tool result against the isolated DB, not just a server registration. Kiro/Claude/Codex may differ in transport and inherited configuration.
5. **Other Kiro stages:** test distillation/digest/editor independently of the dream-cycle profile.
6. **Delivery/backup:** Gmail and rclone authentication are separate from model access.

AWS authentication may use SSO or another credential source; fixed token-lifetime promises in older docs are not universal. Kiro login is separate from the Python embedding client. The Claude Code `mini` intent uses Bedrock, but actual Claude credential routing is outside the TOML and must be verified on the target host.

## Data boundaries

MCP stdio is a local transport. It does **not** keep data local once a remote reasoning model or embedding service consumes memory content. Embedding inputs, role prompts/evidence, Gmail output, and Google Drive backup material cross different boundaries. Source capture may also use authenticated browser cookies. Record actual destinations and approved source scope for an evaluation; do not silently broaden a previously approved frozen-record transfer to the live corpus.

The skim stage adds one more destination and deliberately no more: the full text of every arriving parent-level document goes to the `skim` role's Bedrock model inside the user's AWS account (`us.` geo inference profile) and nowhere else — the role cannot be configured onto Kiro, Claude Code or Codex. The probe's `logs/skim_probe/*.jsonl` contains document-derived text and is gitignored. Details in `docs/SKIM-STAGE.md`, "Privacy".

The HTTP capture API has bearer authentication; the stdio memory server has no per-tool authorization layer. Its security boundary is the local client and environment, not a guarantee that every attached agent is safe to trust. The capture token was rotated on 2026-09-14 and moved out of the tracked launchd plist into the gitignored, mode-0600 `.env` at the repo root, which `scripts/jobs/capture_api.sh` exports before starting the server (and refuses to start without). `tests/test_repo_hygiene.py` fails if a secret-looking value reappears in any tracked plist. The retired value remains in git history and should be treated as public; it no longer authenticates anything.
