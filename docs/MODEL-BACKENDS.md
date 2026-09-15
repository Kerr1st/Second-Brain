# Models, providers, and authentication — original implementation

Source-reviewed 2026-09-10. Configured model IDs are recorded below as **configuration values**; this document does not assert current service availability, price, subscription entitlement, or valid credentials.

## Separate the workloads

| Workload | Selection in code | Tool access / auth |
|---|---|---|
| Embeddings for ingestion, writes, search, dedup | `src/embeddings.py`: Bedrock Titan v2, 1024 dimensions | Boto3 credential chain + region; independent of dream-cycle profile |
| Explorer and Thinker | `src/backends/resolver.py`: selected profile's role mapping | Both call `invoke(..., tools=True)` and require live memory tools |
| Four candidate reviewers | Same resolver, one role mapping each | Requests `tools=False`; candidate JSON+source ID text only in original |
| Chat distillation/contradiction detection | `AgentInvoker()` from `src/agent_invoker.py` | Alias to `KiroInvoker`, default model; profile is ignored |
| Weekly activity digest | `AgentInvoker()` | Kiro alias; profile is ignored |
| Express editor | `AgentInvoker()` unless explicitly injected | Kiro alias; deterministic fallback, profile is ignored |

**Bedrock access is required for this original implementation's normal embedding-dependent path even when reasoning uses Kiro or Codex.** A tool-less synthesis comparison can replace that boundary, but it is not a full native run. Conversely, “multiple agents” does not itself require Bedrock: the six roles can be separate CLI invocations using a supported configured provider.

## Implemented adapters and configuration

`DEFAULT_ADAPTERS` registers `kiro`, `claude_code`, `codex`, and `bedrock`. The first three are agentic CLIs that speak MCP and can run any role. `bedrock` (`src/backends/bedrock.py`, on the `DirectApiInvoker` base) is a tool-less, single-shot call to the Bedrock Runtime Converse API; the resolver refuses it for the Explorer, which needs the live tool-loop.

`config/backends.toml` defines three profiles:

| Profile | Roles | Backend / configured model | Effort |
|---|---|---|---|
| `laptop` (default) | all six | Kiro CLI · `claude-opus-4.8` | Thinker=max; other roles=high |
| `mini` | all six | Claude Code · `global.anthropic.claude-opus-4-8` | Thinker=max; other roles=high |
| `bedrock` | Explorer | Claude Code · `global.anthropic.claude-opus-5` | high |
| | Thinker | bedrock · `global.anthropic.claude-fable-5-1` | high |
| | Skeptic, Epistemologist | bedrock · `global.openai.gpt-6-astra` | high |
| | Advocate, Methodologist | bedrock · `global.anthropic.claude-sonnet-5` | high |

`SECOND_BRAIN_PROFILE` selects a profile. `SECOND_BRAIN_BACKENDS_CONFIG` overrides the file. The resolver checks exactly six required roles and known capability names and caches invokers by backend/model. The `bedrock` profile's inference-profile ids were confirmed ACTIVE in the account (us-east-1 and us-west-2) on 2026-09-14; the panel deliberately mixes two model families so the 3-of-4 consensus requires cross-family agreement. The `mini` profile remains a target configuration, not a deployment fact.

### Tool-less Thinker: the context packet

Agentic-CLI Thinkers read memories themselves over MCP and receive only ids and titles. When the Thinker's backend has `supports_mcp=False`, the orchestrator instead delivers the slice contents in the prompt (`src/dream_cycle/packet.py`: per-memory cap 4,000 chars, packet cap 120,000 chars, invalid ids skipped, DB failure degrades to titles only) and invokes with `tools=False`. Those contents are cached for the run, and every evaluator prompt appends the cited sources' text after the existing `Source memories: [...]` line — so the Skeptic can check factual grounding against text rather than UUIDs. On tool-capable profiles nothing changes.

### Bedrock request rules (live-verified 2026-09-14)

- Claude 5-series (Fable 5.1, Opus 5, Sonnet 5): `additionalModelRequestFields = {"thinking": {"type": "adaptive"}, "output_config": {"effort": <low|medium|high|max>}}`. `thinking.type=enabled`/`budget_tokens` is rejected. Fable 5.1 also rejects forced tool choice, so structured output uses `toolChoice.auto` and validates the reply.
- Claude 4.x keeps the extended-thinking `budget_tokens` mapping.
- OpenAI on Bedrock (`openai.gpt-6-astra` etc.) is served by plain `converse`; depth is `{"reasoning": {"effort": ...}}` (`reasoning_effort` is rejected). Both vendors may emit a `reasoningContent` block, which is ignored.
- Thinking is billed as output and counts against `maxTokens`. The default budget is 32,768 (`BEDROCK_MAX_TOKENS`); a `max_tokens` stop is raised as an error, never parsed as a complete answer.

### Spend guardrails

Every call's real `usage` is priced from the list-price table in `src/backends/cost.py` (unknown models are billed at the ceiling) and accumulated per run. `SECOND_BRAIN_RUN_COST_CAP_USD` (default 5.00) is a hard stop: the run aborts, the run record is closed, and no verdict is fabricated — the exception is not a `RuntimeError`, so the evaluator retry loop cannot swallow it. `SECOND_BRAIN_KILL_SWITCH=1` refuses to start a run. Estimate from the June verified-run token counts: ≈ $1.60/run for the `bedrock` profile before thinking tokens.

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

The HTTP capture API has bearer authentication; the stdio memory server has no per-tool authorization layer. Its security boundary is the local client and environment, not a guarantee that every attached agent is safe to trust. The capture token was rotated on 2026-09-14 and moved out of the tracked launchd plist into the gitignored, mode-0600 `.env` at the repo root, which `scripts/jobs/capture_api.sh` exports before starting the server (and refuses to start without). `tests/test_repo_hygiene.py` fails if a secret-looking value reappears in any tracked plist. The retired value remains in git history and should be treated as public; it no longer authenticates anything.
