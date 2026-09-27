# Models, routing and data boundaries

`config/backends.toml` is the configuration authority. `src/backends/resolver.py` validates every profile before invocation. [The generated inventory](generated/runtime-inventory.md) records current roles and adapter counts without inferring service availability.

## Enforced contracts

The default profile is `controlled`. Corpus roles may not use Kiro or a model identifier containing `fable`, including overrides. This rule applies to capture semantics, distillation, skim, editor, grounding audit and discovery/review roles. A locally executing CLI does not establish local inference or zero cost. Kiro model-selection behavior previously violated that assumption; the retired compatibility invoker cannot be used to bypass routing.

The controlled Explorer uses Claude Code with an explicit read-only MCP server and disabled built-in tools, bare mode and no session persistence. Bare mode prevents ambient project instructions, memory, hooks and plugins from expanding the authorized input. Returned model identifiers are checked against the configured corpus model; disagreement records available usage and fails closed. Its PostgreSQL session is read-only. The Thinker, four panel seats, grounding auditor, skim, distiller, editor and Codex semantic pass use configured Bedrock roles. A separate role means a separate invocation/contract, not an independent physical service. Changing the provider requires a documented endpoint, actual model, authentication route, permitted data scope and measured usage.

The Codex adapter remains available for deliberate future selection. It is not evidence that Codex capture inference runs locally. Direct Bedrock requests are tool-less, with vendor-specific request shaping and strict output validation. Claude Code extracts the terminal stream-JSON result and verifies startup tool results, not merely server registration. The installed ASBX wrapper applies its model allowlist to Claude model names, then maps them to Bedrock inference profiles. The adapter converts recognized Claude Bedrock identifiers for the CLI argument only and retains the original requested model in provenance. A synthetic September 26 probe exposed silent fallback when given a raw profile identifier; canonical Sonnet naming selected Sonnet correctly. Served-model checking remains mandatory.

## Separate embeddings

`src/embeddings.py` uses Bedrock Titan Text Embeddings V2 with 1024 dimensions independently of the reasoning profile. Ingestion, semantic search and derived-memory creation can therefore send text to Bedrock even when no discovery model runs. Oversize input is rejected explicitly, never silently sliced. Chunking must prepare inputs within bounds.

AWS documents a maximum of 8,192 tokens or 50,000 characters. Character counts cannot guarantee the token count for every language, so provider length errors remain explicit failures. [AWS model documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/titan-embedding-models.html)

## Spend and failure accounting

All reasoning invocations go through `invoke_metered` with a `SpendTracker`. Response-bearing parse, truncation and refusal failures are billed before retry decisions. Usage must be finite and nonnegative; missing usage after a potentially paid call marks spend incomplete and prevents another call. Successful parsing does not prove correct facts.

The library default and scheduled override are listed in the generated inventory. These are **post-response stop thresholds**, not hard monetary ceilings: an in-flight response can overshoot. Embedding spend is separate and must be included in maintenance/evaluation estimates. `SECOND_BRAIN_KILL_SWITCH=1` prevents a discovery run. CLI calls lacking reliable usage cannot be treated as free. Claude reports client-side estimated cost, which may differ from the provider invoice; these ledgers are reported or model-priced spend, not invoice reconciliation. [Claude headless and bare mode](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode)

Skim has a sub-budget inside the same run total. Codex semantic capture shares one threshold across bounded new-turn batches, caches validated results before storage retries, and distinguishes contract failures from infrastructure failures. New arrivals do not reset a failed batch's retry budget.

## Qualification before private use

Verify DB/schema, credentials, the exact embedding model, tool-less reasoning, then a real read-only Explorer tool result using synthetic content. STS success alone does not prove model entitlement. Profiles and earlier dated probes are not live verification.

Private evaluation uses a frozen record/provider/cost manifest. Local MCP transport, cloud backups, embedding requests and model prompts are different data boundaries. Preserve the user's authorization scope for each. Do not send the live corpus under a historical 35-record authorization.

## Dependencies

`requirements.lock` is the resolved development/runtime dependency set. MCP stays on maintained 1.x until a deliberate major-version migration; the SDK's security policy supports current maintenance releases, not arbitrary older versions. [MCP security policy](https://github.com/modelcontextprotocol/python-sdk/security)

Upgrade yt-dlp and its supported curl transport together; verify real parsing/provider compatibility separately from the offline suite. The September 26 isolated environment was audited with pip-audit, with the exact result retained in the execution evidence. [yt-dlp releases](https://github.com/yt-dlp/yt-dlp/releases)
