# Models, routing, spend and data boundaries

`config/backends.toml` is the configuration authority; `src/backends/resolver.py` validates every profile at load; `src/backends/base.py` holds the capability matrix and the routing rule; `src/backends/cost.py` holds pricing and the spend tracker. [The generated inventory](generated/runtime-inventory.md) records the adapter, profile and role counts and the default profile. Model ids below are configuration values; this document asserts no service availability, price, entitlement or credential validity. Live probes are recorded per release in the private notes ([RELEASES.md](RELEASES.md)).

## Separate the workloads

| Workload | Selection in code | Tool access |
|---|---|---|
| Embeddings for ingestion, writes, search, dedup, rechunk | `src/embeddings.py`: Bedrock Titan Text Embeddings V2, 1024 dimensions, input bounded (8,192 tokens / 50,000 characters per AWS; oversize is a reported truncation or an explicit failure, never a silent slice) | boto3 credential chain; independent of the reasoning profile |
| Explorer | role `explorer`, Claude Code | `tools=True`: `--mcp-config` with `python -m src.mcp_server`, `--strict-mcp-config`; the full interactive registry, not `src.mcp_readonly` ([INTERFACES.md](INTERFACES.md)) |
| Thinker | role `thinker`, direct Bedrock | `tools=False`; receives the slice contents inline (`src/dream_cycle/packet.py`: 4,000 chars per memory, 120,000 per packet) |
| Grounding audit | the `skeptic` seat's backend (`GROUNDING_AUDIT_ROLE`) | `tools=False`; one graded call per candidate before the vote, skipped past the configured spend share |
| Four seats: skeptic, advocate, epistemologist, methodologist | one role mapping each, direct Bedrock | `tools=False`; candidate JSON plus the cited sources' text |
| Skim (Step 3.5) | role `skim`, Bedrock only in every profile | `tools=False` with a JSON schema; receives full document text ([SKIM-STAGE.md](SKIM-STAGE.md)) |
| Chat distillation | role `distill` (`scripts/distill_sessions.py`, `redistill_capped.py`) | tool-less, metered; model refusals mark the session `do_not_send`; the `distill` launchd job is disabled by the operator |
| Codex semantic pass | role `codex_semantic` (`src/capture/codex.py`) | tool-less, one run-wide threshold, frozen batches |
| Express editor (`memory_brief(use_llm=True)`, `scripts/brief.py`, `express_push.py`) | role `editor` | tool-less, `SECOND_BRAIN_EDITOR_CAP_USD` (default $0.25) |
| Weekly digest | `scripts/weekly_digest.py` through the same resolver and `invoke_metered` | tool-less |

Bedrock access is required for the normal embedding-dependent path even when no discovery model runs. The former `AgentInvoker` compatibility alias is gone; every consumer resolves a role.

## Routing rule

`controlled` is the default profile (`default_profile`; `SECOND_BRAIN_PROFILE` selects another, `SECOND_BRAIN_BACKENDS_CONFIG` another file). Every role in `CORPUS_ROLES` (the six agent roles plus `skim`, `distill`, `codex_semantic`, `editor`) is refused on the `kiro` backend and on any model whose id contains `fable`, at config load, at `resolve_stage` and again inside `invoke_metered` (`assert_corpus_route`), so an explicit or programmatic override cannot bypass it. Spellings of the fable id are pinned by `tests/test_fable_denial_variants.py`. The four Bedrock-only roles are additionally refused on every MCP-capable backend because they receive full document text. A CLI that reports it did not honour `--model` (`failed to set model`) raises `CorpusRoutingError` for a corpus stage after the call's usage is billed; the recorded model is then the requested one and the run does not continue on an unknown model. Why: in September 2026 kiro-cli ignored `--model` and served `claude-fable-5.1`, which Kiro labels unfit for PII, to the Explorer and Thinker. A locally executing CLI is not local inference.

`DEFAULT_ADAPTERS` registers `kiro`, `claude_code`, `codex` (agentic CLIs that speak MCP) and `bedrock` (`src/backends/bedrock.py` on the `DirectApiInvoker` base: one tool-less Converse call). The resolver refuses `bedrock` for the Explorer (it needs the tool loop) and refuses `tools=True` on it. The Kiro and Codex adapters remain registered for non-corpus use and deliberate future selection; neither is used by any configured profile.

## Profiles and roles

Four profiles, ten roles each. `laptop` and `mini` are compatibility profiles on Claude Code (Opus 4.8 inference profiles); `bedrock` and `controlled` run the Thinker and the panel on direct Bedrock.

| Profile `controlled` role | Backend · model | Effort · options |
|---|---|---|
| explorer | claude_code · `global.anthropic.claude-sonnet-5` | high |
| thinker | bedrock · `global.anthropic.claude-opus-5` | high (32,768 output budget) |
| skeptic, epistemologist | bedrock · `global.openai.gpt-6-astra` | high · `max_tokens = 8192` |
| advocate, methodologist | bedrock · `global.anthropic.claude-sonnet-5` | high · `max_tokens = 20480` |
| skim, editor | bedrock · `us.amazon.nova-2-lite-v1:0` | low · `temperature = 0.0, service_tier = "flex", max_tokens = 4096` |
| distill | bedrock · `global.anthropic.claude-sonnet-5` | medium · `max_tokens = 8192` |
| codex_semantic | bedrock · `global.anthropic.claude-opus-5` | high · `max_tokens = 8192` |

Two model families sit on the panel on purpose: with the three-of-four rule no insight is accepted unless both families vote for it. `options` is an inline table of adapter constructor arguments accepted only by `bedrock` (`ADAPTER_OPTIONS`: `temperature`, `service_tier`, `max_tokens`); an option on another backend or an unknown key is rejected at load; roles that share backend and model but not options get separate invokers. The resolver requires exactly the ten roles; a profile missing one is rejected.

### Bedrock request rules (live-verified 2026-09-14 and 2026-09-20; configuration, not a service guarantee)

- Claude 5-series: `additionalModelRequestFields = {"thinking": {"type": "adaptive"}, "output_config": {"effort": …}}`; Claude 4.x keeps the extended-thinking `budget_tokens` mapping.
- OpenAI on Bedrock (`openai.gpt-6-astra`): plain `converse`; depth is `{"reasoning": {"effort": …}}`; a `reasoningContent` block is ignored.
- Thinking is billed as output and counts against `maxTokens` (`BEDROCK_MAX_TOKENS`, default 32,768; per-seat `max_tokens` overrides). A `max_tokens` stop is a loud error, never a silently truncated answer.
- Amazon Nova 2 Lite: forced `toolChoice` accepted (constrained decoding); `reasoningConfig` disabled for effort `low`; `serviceTier: flex` accepted and the served tier echoed into `usage.serviceTier` for pricing.
- Claude Code: `claude -p … --model <id> --output-format stream-json --verbose`; the adapter extracts the terminal result and verifies startup tool results, not just server registration; `--strict-mcp-config` with or without `--mcp-config` selects tools on or off, and `--tools ""` disables built-ins for tool-less calls.

## Spend and failure accounting

Every reasoning call goes through `invoke_metered` with a `SpendTracker`. Usage is priced from the list-price table in `src/backends/cost.py` (unknown models at the ceiling; Flex rates only when the response says Flex) and accumulated per run. `SECOND_BRAIN_RUN_COST_CAP_USD` (library default **$5.00**; the scheduled dream cycle sets **$9**) is a post-response stop threshold: the run aborts, the run row is closed, no verdict is fabricated, and the exception is not a `RuntimeError` so the seat retry loop cannot swallow it. An in-flight response can overshoot. `SECOND_BRAIN_KILL_SWITCH=1` refuses to start a run. Embedding spend is outside these trackers and must be included in maintenance and evaluation estimates separately.

Failed calls: a response-bearing failure (refusal, malformed JSON, `max_tokens` truncation, model not honoured) is billed from real usage before the error propagates. A call that fails before any response exists (throttle, timeout, 5xx, DNS, a CLI that dies) is charged a **conservative reservation** — `ceil(input_chars / 3)` input tokens plus the adapter's output budget at list price, or the stage's budget estimate if larger — recorded as `SpendTracker.reserved_usd` / `reserved_calls` and `stats.spend_reserved_usd`, so the bounded retry and skip rules (seat ≤ 3 attempts, Explorer crash → arrivals-only slice, Thinker failure → slice skipped, skim failure → that document) apply unchanged and a run that keeps failing stops on the cap. Only two things block the tracker permanently (`stats.spend_unknown = true`): a routing-guarantee failure and a successful response without finite, complete usage. Per-seat output budgets bound the worst-case reservation (Astra 8,192, Sonnet-5 20,480 tokens; ≥ 2× the largest observed real output; `tests/test_evaluator_reservation_bound.py`).

Bedrock input counts are reconciled before pricing (`reconcile_input_usage`): `totalTokens − outputTokens` raises `inputTokens` when the provider's total says more input was consumed than the reported input and cache counts, and an effective input below one token per 12 prompt characters is replaced by `ceil(chars / 3)` and flagged `inputTokensEstimated` (still `usage_source="real"`, so charged, not refused). Motivation: in September 2026, 307 of 309 Astra calls reported two input tokens for 16–27k-character prompts.

The skim has its own sub-cap inside the run cap (`SKIM_SPEND_CAP_USD` $1.00, env `SECOND_BRAIN_SKIM_CAP_USD`, at most 15 % of the run cap). The Codex semantic pass shares one threshold across its frozen batches and caches validated output before storage retries. Metrics are JSONL lines in `logs/llm_metrics/` (`KIRO_LLM_METRICS_DIR`) with `usage_source`, real token and cache counts, `model_honored`, reconciliation fields and the stage; Kiro reports only character estimates. Latency, parse success and token counts do not measure accuracy, novelty or value. Client-reported cost is not invoice reconciliation.

## Qualification before private use

Check each dependency independently, from the same account and environment that will run it: database reachability and schema ledger; the exact embedding model with a bounded synthetic request (STS success is not model entitlement); the selected reasoning provider with a bounded synthetic tool-less call; for the Explorer, a real tool result against an isolated database, not just server registration; delivery and backup authentication separately. Profiles and dated probes are not live verification. A private evaluation uses a frozen record/provider/cost manifest and never widens an earlier authorization ([VALIDATION.md](VALIDATION.md)).

## Data boundaries

MCP stdio is a local transport; it does not keep data local once a remote model or embedding service consumes it. Embedding inputs, role prompts and evidence, Gmail output and the encrypted Google Drive bundle cross different boundaries. The skim adds exactly one destination: full document text to the `skim` role's Bedrock model inside the user's AWS account over the `us.` geo inference profile; the role cannot be configured onto an agentic CLI. Source capture may use authenticated browser cookies. The HTTP capture API has bearer authentication; the stdio memory server has no per-tool authorization — its boundary is the local client and environment. The capture token lives only in the gitignored `.env`; `tests/test_repo_hygiene.py` keeps secret-looking literals out of tracked plists, scripts and config.

## Dependencies

`requirements.lock` is the resolved dependency set (installed on the Mac in R1b). MCP stays on maintained 1.x; upgrade yt-dlp and its curl transport together and verify real parsing separately from the offline suite.
