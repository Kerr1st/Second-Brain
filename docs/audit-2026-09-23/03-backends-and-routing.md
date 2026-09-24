## review_backends

=== Context Gathering Summary ===
FOUND: Full profile×role matrix in config/backends.toml (5 profiles, 7 roles); Resolver resolution order, validation, and invoker caching; the skim Bedrock-only enforcement chain across base.py/resolver.py/orchestrator.py; BedrockInvoker request construction, structured-output modes, effort mapping, usage extraction and exception mapping; cost.py price tables, Flex tier handling, SpendTracker cap enforcement; both family bases (AgenticCliInvoker, DirectApiInvoker) and their duplicated mechanics; three off-profile model-selection paths (src/express.py, src/capture/codex.py, scripts/weekly_digest.py + scripts/panel_replay.py); the cron-job profile pinning; the codex_local missing-profile bug; dead seams (InvokeModel, Gemma, Claude 4.x budgets, unreachable CodexInvoker); the trivial uncommitted kiro.py import change.
NOT FOUND: Actual vendor list prices for any configured model (no network calls made); current kiro-cli --model behaviour (cannot execute the CLI); embeddings dimension/region verification (only the model-id default at src/embeddings.py:12 was read — file was out of scope); the live SECOND_BRAIN_PROFILE in the user's shell/launchd state; runtime behaviour of any adapter (no tests run); docs/ and .kiro/specs/ referenced as the source of truth for the capability matrix and every "Req N" citation (out of bounds by instruction); full contents of tests/test_backends.py and other role-matrix tests (sampled via grep only).
====================================

=== CONTEXT GATHERING COMPLETE ===

ANSWER:
# Model-backend / routing layer review — /Users/kerrfir/second-brain @ 6b8c2d2

Note on working tree: `src/backends/kiro.py` has one uncommitted edit, and it is cosmetic — it moves `_write_metrics_line` from `from src.backends.agentic_cli import (...)` to `from src.backends.base import (...)` (kiro.py:30-36). Both modules expose the same object (`agentic_cli.py:41-45` re-exports it from `base`), so behaviour at HEAD and in the working tree is identical. Everything below describes the working tree.

---

## 1. THE ACTUAL PROFILE × ROLE MATRIX

Resolution order, as the code actually runs it:

1. `load_profiles()` reads `SECOND_BRAIN_BACKENDS_CONFIG` or `config/backends.toml` (resolver.py:148-151). There is no per-role env override anywhere — **the only env knob that changes routing is `SECOND_BRAIN_PROFILE`** (resolver.py:176-179).
2. `default_resolver()` picks `SECOND_BRAIN_PROFILE`, else `data.get("default_profile", "laptop")` (resolver.py:152, 229-231). Config declares `default_profile = "laptop"` (backends.toml:33), so config and code default agree — but see finding C-1: the code's hardcoded fallback is also `"laptop"`, which is the all-Kiro profile.
3. `_validate_profile()` requires exactly the 7 roles, rejects unknown backends, validates `options` against `ADAPTER_OPTIONS`, and calls `assert_backend_supports_role` for every role (resolver.py:71-107).
4. `Resolver.__init__` re-checks only `explorer` and `skim` eagerly (resolver.py:191-197); `invoker_for()` re-checks per role lazily (resolver.py:210).
5. Invokers are cached on `(backend, model, options_key())` (resolver.py:211-221), and constructed as `adapter_cls(model=spec.model, **options)`.

### The table (backend / model / effort)

| role | laptop (default) | mini | bedrock | hybrid | controlled (all cron jobs) |
|---|---|---|---|---|---|
| explorer | **kiro** / claude-opus-4.8 / high | claude_code / global.anthropic.claude-opus-4-8 / high | claude_code / claude-opus-5 / high | **kiro** / claude-opus-5 / high | claude_code / claude-sonnet-5 / high |
| thinker | **kiro** / claude-opus-4.8 / max | claude_code / claude-opus-4-8 / max | bedrock / **claude-fable-5-1** / high | **kiro** / claude-opus-5 / max | bedrock / claude-opus-5 / high |
| skeptic | **kiro** / claude-opus-4.8 / high | claude_code / claude-opus-4-8 / high | bedrock / gpt-6-astra / high | bedrock / gpt-6-astra / high | bedrock / gpt-6-astra / high |
| advocate | **kiro** / claude-opus-4.8 / high | claude_code / claude-opus-4-8 / high | bedrock / claude-sonnet-5 / high | **kiro** / claude-sonnet-5 / high | bedrock / claude-sonnet-5 / high |
| epistemologist | **kiro** / claude-opus-4.8 / high | claude_code / claude-opus-4-8 / high | bedrock / gpt-6-astra / high | bedrock / gpt-6-astra / high | bedrock / gpt-6-astra / high |
| methodologist | **kiro** / claude-opus-4.8 / high | claude_code / claude-opus-4-8 / high | bedrock / claude-sonnet-5 / high | **kiro** / claude-sonnet-5 / high | bedrock / claude-sonnet-5 / high |
| skim | bedrock / us.amazon.nova-2-lite-v1:0 / low / `{temp 0.0, flex, max_tokens 4096}` | identical | identical | identical | identical |

Evidence: backends.toml:33-126 (laptop 36-42, mini 53-59, bedrock 76-82, hybrid 94-100, controlled 120-126).

The skim row is genuinely invariant and genuinely pinned: identical literal in all five profiles (backends.toml:42, 59, 82, 100, 126), enforced structurally by `assert_backend_supports_role` rejecting any `supports_mcp=True` backend for `role == "skim"` (base.py:200-205), checked at config load (resolver.py:104), at Resolver construction (resolver.py:195-197), at every resolution (resolver.py:210), and again defensively before the stage runs (orchestrator.py:897-901). `temperature=0.0` reaches `inferenceConfig.temperature` (bedrock.py:341-343) and forced `toolChoice.tool` is applied because Nova hits the `forced_tool` branch (bedrock.py:191-199, 268-290) when `src/skim/runner.py:201` passes `schema=SKIM_SCHEMA`. This part of the system matches the stated rule exactly.

### Cells where corpus content can reach Kiro or Fable 5.1

**M-1 (CRITICAL) — the default profile routes the whole corpus through Kiro.** `laptop` puts all six agent roles on `kiro` (backends.toml:36-41), and it is both the config default and the code's hardcoded fallback (backends.toml:33, resolver.py:152). Every cron job overrides to `controlled` (`scripts/jobs/dream_cycle_scheduled.sh:32`, `codex_capture.sh:38`, `weekly_digest.sh:36`), so scheduled runs are safe — but any interactive/manual invocation without `SECOND_BRAIN_PROFILE` set gets all-Kiro. The Explorer and Thinker read slice contents; that is corpus content on Kiro. Fix: change `default_profile` to `controlled` and change the `data.get("default_profile", "laptop")` fallback to match; keep `laptop` as an explicitly-selected legacy profile only.

**M-2 (CRITICAL) — `hybrid` routes explorer/thinker/advocate/methodologist to Kiro** (backends.toml:94, 95, 97, 99). Its own comment (backends.toml:89-92) argues only against Kiro's *Fable* model, not against Kiro; but the `controlled` comment (backends.toml:105-114) records that kiro-cli 2.22 ignores `--model` and its server-side "Auto" routing served Fable 5.1 anyway. Under that finding, `hybrid`'s `model = "claude-opus-5"` is a *request*, not a fact — `AgenticCliInvoker._note_model_honored` (agentic_cli.py:228-243) only logs a warning and sets a metrics flag when the CLI says `failed to set model`; nothing aborts, and provenance still records the requested model. So `hybrid` can put the corpus on Fable 5.1 *and* on Kiro simultaneously. Fix: delete `hybrid`, or gate it behind an explicit opt-in env flag, or make `model_honored == False` a hard `RuntimeError` for corpus-bearing roles.

**M-3 (HIGH) — `bedrock` profile puts the Thinker on Fable 5.1** (backends.toml:77). The Thinker receives the slice packet inline (per the `controlled` comment, backends.toml:115-117), so this is corpus text to the model the user says must never see it. `tests/test_resolver.py:63` actively *asserts* this (`assert p["thinker"].model == "global.anthropic.claude-fable-5-1"`), so the prohibition is pinned backwards in the test suite. Fix: switch to `claude-opus-5` (making `bedrock` a superset of `controlled` for the Thinker) and invert the test to a prohibition; or add a global "no fable for corpus roles" assertion in `_validate_profile`.

**M-4 (HIGH) — nothing in the code enforces either rule.** `assert_backend_supports_role` (base.py:169-206) encodes exactly two rules: explorer-needs-MCP and skim-must-not-have-MCP. There is no check that a corpus-bearing role is off Kiro, and no check that any role avoids Fable. The only enforcement is per-profile assertions in `tests/test_resolver.py:79-100` (`test_hybrid_profile_meters_only_the_astra_seats`, `test_controlled_profile_avoids_kiro_and_fable`), which cover only the two profiles they name and cannot catch a new profile or an operator's `SECOND_BRAIN_PROFILE=laptop`. Fix: add `CORPUS_ROLES` and a `DENIED_MODEL_SUBSTRINGS = {"fable"}` / `DENIED_BACKENDS_FOR_CORPUS = {"kiro"}` check inside `_validate_profile`, so it fails at config load in every profile.

### Roles whose backend is decided somewhere other than the profile table

**M-5 (CRITICAL) — the Express editor bypasses the resolver entirely and hardcodes Kiro.** `src/express.py:481-483` does `from src.agent_invoker import AgentInvoker; invoker = AgentInvoker()`. `AgentInvoker` is `KiroInvoker` (agent_invoker.py:19-20) and a bare constructor takes `DEFAULT_MODEL = "claude-opus-4.8"` (kiro.py:43, agentic_cli.py:83). The editor's user message is built from briefing items (`_editor_user_message(items, ...)`, express.py:485) — i.e. corpus-derived titles and content. This is a corpus→Kiro path that no profile can change, that ignores `SECOND_BRAIN_PROFILE` completely, and that is never metered. The `except Exception` at express.py:496 means it also fails silently to a deterministic fallback, so the violation is invisible in logs beyond one WARNING. Fix: resolve through `default_resolver().invoker_for(SKIM_ROLE)` the way `scripts/weekly_digest.py` already does (see below), and pass a `SpendTracker`.

**M-6 (HIGH) — codex chat-capture borrows the `thinker` role for raw chat turns.** `src/capture/codex.py:190-212` (`_BackendSemanticPass.run`) resolves `spec_for("thinker")` / `invoker_for("thinker")` and sends `_turn_dict(turn)` payloads — raw chat-session content. Under `laptop`/`hybrid` that is Kiro; under `bedrock` that is Fable 5.1. The cron job pins `controlled` (`scripts/jobs/codex_capture.sh:38`, `scheduling/com.second-brain.codex-capture.plist`), so the scheduled path is safe, but the library function is not. Fix: give this workload its own Bedrock-pinned role (or reuse `SKIM_ROLE`), and add the same `assert_backend_supports_role`-style guard.

**M-7 (MEDIUM) — weekly digest borrows the `skim` role, deliberately.** `scripts/weekly_digest.py:60` sets `DIGEST_ROLE = SKIM_ROLE` precisely so the Bedrock-only rule applies transitively, and re-checks `caps.supports_mcp` before running (weekly_digest.py:139-146). This is the *correct* pattern and the one Express and codex-capture should copy. But it also introduces `DIGEST_MODEL` (weekly_digest.py:65), an env override that changes the model while keeping the role's backend — so the digest's model is decided outside the profile table, and an unpriced override is billed at the $10/$50 ceiling (its own docstring, weekly_digest.py:74-80, acknowledges this).

**M-8 (MEDIUM) — panel replay can override any evaluator seat from the command line.** `scripts/panel_replay.py:121-129` parses `--model-override role=backend:model[:effort]` into a `RoleBackend` and constructs the adapter directly via `DEFAULT_ADAPTERS[spec.backend](model=spec.model)` (panel_replay.py:157-158), bypassing `Resolver.invoker_for` and therefore bypassing `assert_backend_supports_role`. A `_require_direct_api` check (panel_replay.py:111-117) blocks MCP-capable backends, which incidentally blocks Kiro — but nothing blocks `--model-override thinker=bedrock:global.anthropic.claude-fable-5-1`.

---

## 2. CONTRADICTIONS

**C-1 (HIGH) — config default and code fallback both point at the profile the rules forbid.** `default_profile = "laptop"` (backends.toml:33) and `data.get("default_profile", "laptop")` (resolver.py:152). Meanwhile every shipped cron job exports `controlled` and `scripts/jobs/weekly_digest.sh:10` calls `controlled` "the only profile whose every seat is" acceptable. The declared default contradicts operational reality. Fix: make `controlled` the default in both places.

**C-2 (HIGH) — `SECOND_BRAIN_PROFILE=codex_local` is set by a shipped job but no such profile exists.** `scripts/jobs/codex_capture_canary.sh:25` and `scheduling/com.second-brain.codex-canary.plist` both set `codex_local`; `config/backends.toml` defines only laptop/mini/bedrock/hybrid/controlled. `default_resolver()` raises `KeyError` for an unknown profile (resolver.py:232-236). `scripts/jobs/codex_capture.sh:12` even documents "`codex_local` is not in..." — so this is known and unfixed. The canary job cannot resolve a backend. Fix: add the profile or change the canary to `controlled`.

**C-3 (MEDIUM) — the `codex` adapter is registered but unreachable.** `DEFAULT_ADAPTERS["codex"] = CodexInvoker` (resolver.py:45) and `BACKEND_CAPABILITIES["codex"]` exists (base.py:159-161), but no profile in `config/backends.toml` uses `backend = "codex"`, and the one CLI path that could inject it (`panel_replay --model-override`) rejects MCP-capable backends (panel_replay.py:111-117). 620 lines of adapter (`src/backends/codex.py`) plus its usage-event parsing are reachable only from tests. Related: the `codex_local` profile that would have used it does not exist (C-2). Fix: decide — either add the profile or move the adapter behind an explicit opt-in and stop advertising it in `DEFAULT_ADAPTERS`.

**C-4 (MEDIUM) — `claude-opus-4.8` is hardcoded in code and also declared in config.** `kiro.py:43 DEFAULT_MODEL = "claude-opus-4.8"`, re-exported through `agent_invoker.py:18,29`, and independently declared for all six laptop roles (backends.toml:36-41). Changing the config leaves the Express path (M-5) on the hardcoded value. `tests/test_resolver.py:44` also hardcodes it. Fix: delete `KiroInvoker.DEFAULT_MODEL` and require an explicit model, as `BedrockInvoker` already does (bedrock.py:157-161) and `CodexInvoker` does (codex.py:175-176).

**C-5 (MEDIUM) — two price lookups for the same call, one tier-aware and one not.** `SpendTracker.record` prices the call with `estimate_cost_usd(model, usage)`, which reads `usage["serviceTier"]` and can select `_FLEX_PRICE_TABLE` (cost.py:172-176, 108-118), then immediately calls `price_for(model)` *without* the tier (cost.py:214) to decide whether to emit the unknown-model warning. So the warning text can report `$10/$50` for a call that was actually priced at the Flex Nova rate. Cosmetic today (both agree on `known`), but it is a second, divergent price lookup for one call. Fix: `price = price_for(model, tier)` computed once and passed into `estimate_cost_usd`.

**C-6 (LOW) — the `_PRICE_TABLE` ordering comment describes an entry that does not exist.** cost.py:41-42 says specific substrings "must precede more general ones (e.g. `fable-5-1` before `fable-5`)", but the table contains only `("claude-fable-5", 10.0, 50.0)` (cost.py:45). There is no `fable-5-1` row; the 5.1-specific behaviour lives elsewhere, as an inline `frac = 0.025 if "fable-5-1" in m` (cost.py:113). So Fable 5.1's cache-read discount is expressed in a different mechanism than the comment implies. Fix: either add the row or correct the comment to point at the `frac` logic.

**C-7 (MEDIUM) — `ADAPTER_OPTIONS` whitelists three of `BedrockInvoker`'s eight constructor options, and the other five are unreachable from config.** `ADAPTER_OPTIONS["bedrock"] = {"temperature", "service_tier", "max_tokens"}` (resolver.py:51-53), while the constructor accepts `region`, `client`, `max_retries`, `system_prompt_delivery`, `use_invoke_model` as well (bedrock.py:140-152). `_validate_options` rejects anything outside the whitelist (resolver.py:110-121), so `use_invoke_model` and `system_prompt_delivery` cannot be set by any profile. See D-2.

**C-8 (LOW) — the "compatibility alias" indirection still does real work, but in the wrong direction.** `src/agent_invoker.py` is documented as a migration shim (agent_invoker.py:1-11) and tells new code not to use it. It still has a live non-test consumer: `src/express.py:481`. So the alias is not dead — it is the vehicle for the M-5 rule violation. Separately, `kiro.py:44-58` keeps a *duplicate* `METRICS_DIR` and `_record_call_metrics` alongside the canonical ones in `base.py:35-53`, purely so tests and launchd can monkeypatch `src.backends.kiro.METRICS_DIR`; `agentic_cli.py:41-45` keeps a *third* re-export for the same reason. Three names for one directory. Fix: retire the shim by fixing express.py, then collapse the metrics-dir indirection to one module-level accessor that reads the env at call time.

---

## 3. INACCURACIES (docstrings / comments / log strings)

**I-1 (MEDIUM) — backends.toml's header describes a model and a cost posture that no longer exist.** Lines 6-9 say "The `laptop` profile reproduces today's behavior exactly (all-Kiro / Opus 4.8, $0 metered)" and "The `bedrock` profile runs the Thinker and all four evaluators on the direct Bedrock Converse API". The header never mentions `hybrid` or `controlled` (the profile every job actually uses), and it presents all-Kiro as "today's behavior" when the `controlled` comment 70 lines later (105-114) documents that Kiro must not be used for this corpus at all. The file contradicts itself top-to-bottom.

**I-2 (MEDIUM) — `resolver.py`'s module docstring states the default profile "reproduces today's all-Kiro behavior"** (resolver.py:4-6) and that "an all-Kiro profile yields a single shared KiroInvoker across all roles — identical to today" (resolver.py:9-11). "Today" is the pre-`controlled` era; as written the docstring recommends the configuration the user's rules prohibit.

**I-3 (LOW) — `base.py` claims adapters are unimplemented that are implemented.** base.py:15-16 says "Concrete adapters (`KiroInvoker` and, later, `ClaudeCodeInvoker` etc.)", base.py:19-20 says "Nothing here changes runtime behavior", and base.py:155-157 says "Declaring a backend here does NOT imply its adapter is implemented yet; adapters are added in priority order (Kiro, then Claude Code)". All four adapters exist and are registered (resolver.py:42-47). The `base.py:20` claim is also false now that `assert_backend_supports_role` is load-bearing for the skim rule.

**I-4 (LOW) — `direct_api.py` names two adapters that do not exist.** It refers to "later `OpenAIInvoker`/`AnthropicInvoker`" and "a future `OpenAIInvoker` or `AnthropicInvoker`" (direct_api.py:20-21, 30-32). Only `BedrockInvoker` subclasses it. OpenAI models are reached *through* Bedrock (`global.openai.gpt-6-astra`), so a direct-OpenAI adapter may never be wanted; the docstring plans for a shape the architecture has moved away from.

**I-5 (MEDIUM) — `bedrock.py`'s Fable-5.1 rules are load-bearing documentation for a model the rules forbid.** bedrock.py:21-22 and 191-199, 253-259 document that "Fable 5.1 rejects forced tool choice … so structured output uses `toolChoice.auto`". That is why `_structured_output_mode()` returns `auto_tool` for *every* non-Nova, non-Gemma model — including Opus 5 and Sonnet 5, which are the models actually configured (backends.toml:120-125). So the weakest structured-output mode is applied to all Claude/GPT seats to accommodate a model that should not be in use. The comment is accurate about Fable but misleading about why the current models are configured the way they are.

**I-6 (LOW) — `cost.py` docstring says prices are "as published on 2026-09-14"** (cost.py:22-24) while the Nova/Gemma rows are dated 2026-09-20 (cost.py:57-63) and the Flex table 2026-09-20 (cost.py:65-70). One date claim covering three vintages.

**I-7 (LOW) — `mcp_probe.py` says both CLI adapters "reuse this helper from their `_run_probe` hook"** (mcp_probe.py:27-30). True for `claude_code.py:77` and `codex.py:80`, but `KiroInvoker` does not use it at all — it relies on `--require-mcp-startup` and the exit-3 retry (kiro.py:180-183, 219-238). Since Kiro is the only backend running the Explorer in `laptop`/`hybrid`, the "worst silent failure" the probe was written to prevent (mcp_probe.py:3-10) is unguarded in exactly those profiles.

**I-8 (LOW) — `cost.py` docstring says "On direct Bedrock (and Claude Code on Bedrock) every dream-cycle call is metered"** (cost.py:3-5). Not every call: the Express editor (M-5) and any Kiro seat spend without metering, and `SpendTracker.record` early-returns before incrementing `self.calls` when cost is 0 (cost.py:210-212) — see G-2.

---

## 4. DEAD CODE

**D-1 (MEDIUM) — `CodexInvoker` is unreachable in production.** See C-3. `src/backends/codex.py` (620 lines) plus `_usage_from_events` / `_usage_from_event` / `_is_usage_dict` (codex.py:428-475) have no live caller.

**D-2 (MEDIUM) — `BedrockInvoker`'s InvokeModel fallback seam is unreachable.** `use_invoke_model` gates `_invoke_model_fallback` (bedrock.py:151, 316-317, 428-473) and a dedicated branch in `_extract_raw` (bedrock.py:492-502) and `_extract_usage` (bedrock.py:566-583). It cannot be set from config (C-7) and no code passes it. Same for `system_prompt_delivery="prepend"` (bedrock.py:147-149, 324-331) — the `prepend` branch is never taken. Fix: delete, or add to `ADAPTER_OPTIONS` if a real model needs it.

**D-3 (MEDIUM) — the Gemma structured-output path is dead.** `_is_gemma()` (bedrock.py:188), the `json_schema` / `outputConfig.textFormat` branch (bedrock.py:267-283), and the `gemma-3-12b` rows in both price tables (cost.py:63, 72) exist for a model that appears in no profile. The code even admits it: "not live-verified; Phase 1 only spends on Nova" (bedrock.py:56-57).

**D-4 (MEDIUM) — the Claude 4.x thinking-budget path is dead on Bedrock.** `_LEGACY_THINKING_BUDGETS` (bedrock.py:82) and the `generation < 5` branch (bedrock.py:409-415) apply only to a Claude 4.x model *on the bedrock backend*. No profile does that: `claude-opus-4-8` appears only under `claude_code` (backends.toml:53-58) and `claude-opus-4.8` only under `kiro` (36-41). The `("claude-opus-4", 5.0, 25.0)` and `("claude-sonnet-4", 3.0, 15.0)` price rows (cost.py:49-50) are likewise only reachable via Claude Code's `total_cost_usd`, which short-circuits the table entirely (cost.py:160-162).

**D-5 (LOW) — `BackendCapabilities.structured_output` is never read.** Declared (base.py:135-137) and set for all four backends (base.py:147-166), but `grep` finds no consumer. `reports_usage` is read only by the invariant in `__post_init__` (base.py:139-146). Only `metered` and `supports_mcp` are consumed (orchestrator.py:179, 899; panel_replay.py:114; weekly_digest.py:139; tests). Fix: drop `structured_output`, or use it to decide whether to pass a schema.

**D-6 (LOW) — the generic retry machinery in `AgenticCliInvoker` has exactly one user.** `_attempts_allowed` / `_initial_backoff` / `_should_retry` default to no-retry (agentic_cli.py:394-411) and are overridden only by `KiroInvoker` for the exit-3 MCP case (kiro.py:220-238). Claude Code and Codex never retry. Combined with D-1 that is one live consumer for a three-hook protocol in the base.

**D-7 (LOW) — `src/backends/__init__.py` exports a curated surface nobody imports.** Every consumer imports from the submodules directly (`from src.backends.base import ...`, `from src.backends.resolver import ...`). The `__all__` at `__init__.py:17-23` is unused indirection.

**D-8 (LOW) — `if TYPE_CHECKING: pass` in bedrock.py:70-71** is a no-op left from a removed import.

**D-9 (LOW) — `Invoker` is `@runtime_checkable` (base.py:79) solely for tests.** The only `isinstance(x, Invoker)` calls are in `tests/test_claude_code.py:170`, `tests/test_codex.py:94`, `tests/test_backends.py:79-85`. No production code does structural checking. Harmless, but note that `runtime_checkable` Protocol checks only verify method *names*, so those tests prove very little.

---

## 5. DUPLICATION — what the base should own vs what it owns today

The two family bases (`AgenticCliInvoker`, `DirectApiInvoker`) are genuinely good factorings within their family, but they duplicate each other, and `base.py` owns almost nothing of the shared template.

**P-1 (HIGH) — the metrics dict and the metrics lifecycle are implemented twice.** `AgenticCliInvoker._init_metrics` (agentic_cli.py:280-315) and `DirectApiInvoker._init_metrics` (direct_api.py:175-207) build near-identical dicts with **divergent key names for the same quantity**: the CLI family writes `latency_s` (agentic_cli.py:190) while the direct family writes `duration_s` (direct_api.py:169). Both also write `usage_source`, `success`, `error`, `input_chars`, `output_chars`, `model`, `stage`, `run_id`. Any cross-backend latency analysis over `logs/llm_metrics/*.jsonl` silently sees two schemas. Also `_record_metrics` / `_metrics_dir` are defined identically in both (agentic_cli.py:267-278 vs direct_api.py:219-226). **The base should own:** one `_init_metrics`, one key vocabulary, one `_record_metrics`/`_metrics_dir`. **It owns today:** only `_write_metrics_line` and the `METRICS_DIR` constant (base.py:35-53).

**P-2 (HIGH) — the JSON-parse-with-metrics wrapper is duplicated verbatim.** `_parse` in agentic_cli.py:245-253 and direct_api.py:209-217 are the same six lines (try `parse_json_output`, on `ValueError` set `metrics["error"] = "json_parse_failed"`, log, re-raise); only the log format string differs. Both then re-bind `parse_json_output = staticmethod(...)` (agentic_cli.py:86, direct_api.py:77), and `KiroInvoker` binds it a *third* time (kiro.py:105). **The base should own** `_parse`.

**P-3 (MEDIUM) — the tolerate-and-warn "metered but no usage" path exists in three copies with three different warning texts.** `DirectApiInvoker._warn_missing_usage` (direct_api.py:228-245), `ClaudeCodeInvoker._extract_usage`'s warning (claude_code.py:378-390), `CodexInvoker._extract_usage`'s warning (codex.py:414-425). All three return `(None, "estimate")` and all three say roughly "metered backend, real usage expected, absent". Only the direct family has it as a reusable helper. **The base should own** `_warn_missing_usage`.

**P-4 (MEDIUM) — token accounting is done in three places with three key spellings.** The adapters normalise usage into camelCase (`inputTokens`) for Bedrock (bedrock.py:585-592) but pass provider snake_case straight through for Claude Code (claude_code.py:361-376) and Codex (codex.py:398-411); `cost.py:_tokens` then papers over both by trying alternative spellings (cost.py:139-146, 163-168). Separately, the CLI base computes char/4 estimates unconditionally (agentic_cli.py:191-192) even for metered backends that reported real counts. **The base should own** one `normalize_usage(provider_usage) -> dict` returning a single canonical shape, so `cost.py` stops guessing key names.

**P-5 (MEDIUM) — USD math has two entry points with different semantics.** `SpendTracker.record` (cost.py:203-225, enforces the cap, increments counters) and bare `estimate_cost_usd` (cost.py:149-181, no cap, no counters). `src/skim/runner.py` uses *both on the same call*: `record_spend(model, usage)` normally (runner.py:216) and `estimate_cost_usd(model, usage)` in the `SpendCapExceeded` branch (runner.py:219) so the last, cap-breaching call still gets added to `result.cost_usd`. Correct, but it means one call can be priced twice by two different functions. `scripts/panel_replay.py:174` uses `tracker.record` directly. **The base/cost module should own** a single `price_and_record(model, usage) -> (cost, capped)` that never needs a second call.

**P-6 (MEDIUM) — `_record_call_metrics` is defined three times.** `base.py:45` (`_write_metrics_line`, canonical), `agentic_cli.py:58-66` (wrapper reading the module-global `METRICS_DIR`), `kiro.py:61-66` (another wrapper for the same reason). All three exist to keep monkeypatch targets alive (C-8).

**P-7 (LOW) — timeout handling is split.** The CLI family maps `subprocess.TimeoutExpired` → `TimeoutError` in `_run_subprocess` (agentic_cli.py:198-224); the direct family relies on the adapter's `_map_exception` to map `ReadTimeoutError`/`ConnectTimeoutError`/`ModelTimeoutException` (bedrock.py:639-651, 657-660). Nothing guarantees a new adapter maps timeouts at all — `DirectApiInvoker._map_exception` defaults to returning the exception unchanged (direct_api.py:271-279), so a new direct adapter silently violates the documented `TimeoutError` contract (base.py:124-127).

---

## 6. COST ACCOUNTING INTEGRITY

The happy path, end to end:

1. Adapter extracts usage. Bedrock normalises Converse `usage` to camelCase plus `cacheReadInputTokens`/`cacheWriteInputTokens` and copies the **served** `serviceTier.type` (bedrock.py:584-600). Claude Code copies the envelope's `usage` and folds in `total_cost_usd` (claude_code.py:361-376). Codex parses `--json` usage events (codex.py:398-411).
2. `orchestrator._record_spend(role, result)` reads `result["usage"]` and calls `self._spend.record(self.resolver.spec_for(role).model, usage)` (orchestrator.py:182-196).
3. `SpendTracker.record` → `estimate_cost_usd(model, usage)` (cost.py:213). If the provider reported `total_cost_usd`, that wins outright (cost.py:160-162). Otherwise tokens × `price_for(model, usage["serviceTier"])` / 1e6, with cache reads at a model-specific fraction and cache writes at 1.25× input (cost.py:95-105, 170-181).
4. Total accumulates; `SpendCapExceeded` is raised *after* counting the offending call (cost.py:220-225) and deliberately does not subclass `RuntimeError` so the evaluator retry loop cannot swallow it (cost.py:183-185).

This is a sound design. The integrity problems are at the edges:

**G-1 (CRITICAL) — two paths spend without recording anything.**
- `src/express.py:481-490` constructs `AgentInvoker()` and invokes with no tracker anywhere in the call chain. On Kiro that is credits, not dollars — but the same code will bill real dollars the moment the Express editor is moved onto a metered backend, and there is no tracker to catch it.
- `src/capture/codex.py:205-212` invokes the resolved *thinker* — which under `controlled` is `bedrock/global.anthropic.claude-opus-5` at $5/$25 per 1M — and never touches a `SpendTracker`. This is a metered Bedrock call, made once per chat-session segment, with no cap and no ledger entry. It runs nightly via `scripts/jobs/codex_capture.sh`. This is the most serious cost finding: unbounded metered spend on the most expensive configured Claude model.
Fix: make the tracker a required constructor argument of any code path that resolves an invoker, or move the spend check into `Resolver.invoker_for` by wrapping the returned invoker in a metering decorator so it is impossible to invoke unmetered.

**G-2 (HIGH) — a zero-cost metered call is invisible.** `SpendTracker.record` returns early when `cost == 0.0`, *before* `self.calls += 1` (cost.py:210-212). So a metered call whose provider reported `total_cost_usd: 0.0`, or whose usage block parsed to no recognisable token keys, increments neither `calls` nor `total_usd` and produces no log line (orchestrator.py:191-196 only logs `if cost`). `tests/test_cost.py:121-122` confirms unparseable usage prices at 0. The run then reports fewer metered calls than it made. Fix: count the call unconditionally; track `calls` and `unpriced_calls` separately.

**G-3 (HIGH) — provenance and pricing both use the *requested* model, not the served one.** `_record_spend` prices against `self.resolver.spec_for(role).model` (orchestrator.py:190), and `_backend_provenance` records the same (orchestrator.py:219-225). When the CLI ignores `--model` — the documented kiro-cli 2.22 behaviour (backends.toml:105-109) detected by `_note_model_honored` (agentic_cli.py:228-243) — the served model differs from the priced model, and the warning is only a log line plus a metrics flag. A Sonnet-5 request served as Fable 5.1 would be billed at Sonnet-5's $2/$10 instead of $10/$50: a 5× understatement, and the cap would not trip. Fix: have `_extract_usage` return the served model id when the provider reports one, price on that, and treat `model_honored == False` as fatal for metered roles.

**G-4 (MEDIUM) — unknown models are billed at a ceiling that is no longer the ceiling.** `_CEILING_IN, _CEILING_OUT = 10.0, 50.0` (cost.py:75) equals the most expensive *known* row (Fable 5.1 and GPT-6 Astra, cost.py:45, 53). The comment calls it "Conservative … (= the most expensive known tier)" — true only as long as nothing pricier is ever configured, and the `DIGEST_MODEL` override (weekly_digest.py:65) plus `panel_replay --model-override` (panel_replay.py:121) both let an operator name an arbitrary model. Fix: set the ceiling to a genuine multiple of the dearest known row, or refuse to run on an unknown model rather than guessing.

**G-5 (MEDIUM) — the Flex discount depends on a field only one adapter populates.** `estimate_cost_usd` reads `usage["serviceTier"]` (cost.py:172) and Bedrock is the only adapter that sets it (bedrock.py:595-599). That is correct today, but note the failure direction: if Converse ever omits `serviceTier` from the response, the skim silently prices at the *standard* Nova rate (2× the Flex rate, cost.py:62 vs 71) — conservative, so it fails safe, but it will overstate skim cost and can trip the $1.00 sub-cap early (`_skim_cap_usd`, orchestrator.py:130-147).

**G-6 (MEDIUM) — three independent caps with no single accounting view.** `SECOND_BRAIN_RUN_COST_CAP_USD` default $5.00 (cost.py:39), the skim sub-cap `SKIM_SPEND_CAP_USD = 1.00` bounded to 15% of the run cap (orchestrator.py:111-112, 130-147), and the digest cap `DEFAULT_DIGEST_CAP_USD = 0.25` (weekly_digest.py:80). The two comment-level cost estimates in the config also disagree with the code: `controlled` says "≈ $3-4/run … under the $6/run cap" (backends.toml:117-119) but the code's default cap is $5.00 (cost.py:39), and `bedrock` says "Estimated ≈ $1.60/run" (backends.toml:75) while `hybrid` says the all-Bedrock profile is "≈ $7.50" (backends.toml:88) — two different figures for the same profile, one of them above the cap. Fix: one cap constant, documented in one place, with the per-stage sub-caps derived from it.

**G-7 (LOW) — Kiro is correctly exempt but the exemption is stated as if it were free.** `KiroInvoker._extract_usage` returns `(None, "estimate")` (kiro.py:211-216), `record` therefore contributes nothing, and `_budget_remaining` notes "With an all-Kiro profile nothing is metered … the cap binds only where usage is real" (orchestrator.py:198-203). Accurate as dollars, but it means the `laptop` and `hybrid` profiles have **no spend ceiling of any kind** and no per-call cost record — so the profiles that violate the corpus rule are also the ones with no cost telemetry.

---

## Recommended fix ordering

1. G-1 / M-5 / M-6 — stop unmetered and unresolved-backend spend: fix `src/express.py:481` and `src/capture/codex.py:190`, and wrap `Resolver.invoker_for` so an invoker cannot be obtained without a tracker.
2. M-1 / C-1 — flip `default_profile` and the code fallback to `controlled`.
3. M-4 — move the two corpus rules (no Kiro, no Fable, for corpus-bearing roles) into `_validate_profile` / `assert_backend_supports_role` so they fail at config load, then invert `tests/test_resolver.py:63`.
4. M-2 / M-3 — retire `hybrid`, move `bedrock`'s Thinker off Fable.
5. G-3 — price on the served model; make `model_honored == False` fatal for metered roles.
6. P-1 / P-2 / P-3 — hoist `_init_metrics` (one key vocabulary), `_parse`, and `_warn_missing_usage` into `base.py`.
7. C-2 — fix or remove the `codex_local` canary.
8. D-2 / D-3 / D-4 — delete the InvokeModel seam, the Gemma path, and the Claude 4.x Bedrock thinking branch.

---

## What I could NOT verify

- **Real list prices.** Every figure in `_PRICE_TABLE` / `_FLEX_PRICE_TABLE` (cost.py:44-72) is unverifiable from the repo; the models named (Fable 5.1, Opus 5, Sonnet 5, GPT-6 Astra, Nova 2 Lite) are not models I can price, and I did not make network calls. G-4's premise — that $10/$50 is still the dearest row — rests only on the table's own contents.
- **Whether kiro-cli still ignores `--model`.** M-2 depends on the claim in backends.toml:105-109. I could not exercise `kiro-cli`, so I cannot say whether `hybrid`'s Opus-5 request is currently honoured or silently served as Fable 5.1.
- **Embeddings.** I confirmed `src/embeddings.py:12` sets `EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")`, which matches the stated rule (Titan text v2, and it is env-overridable rather than profile-driven — so it is genuinely independent of the profile, as required). I did **not** read the rest of `src/embeddings.py` and so did not verify the 1024-dimension claim, the region, or that no other code path embeds with a different model. Embeddings were outside the stated file scope.
- **Whether any profile is ever active other than via the paths I found.** I checked `scripts/jobs/*.sh`, `scheduling/*.plist`, and `scripts/*.py`. I did not inspect the user's shell environment, `~/.zshrc`, or loaded launchd state, so I cannot confirm which profile is live right now.
- **Runtime behaviour of the adapters.** Read-only review: no tests were run. Claims about Claude Code's stream-json envelope, Codex's `--json` usage events, and the MCP probe's detection logic come from reading `claude_code.py`, `codex.py`, and `mcp_probe.py`, not from execution.
- **The docs.** `docs/MODEL-BACKENDS.md`, `docs/PLAN-skim-stage.md`, `docs/MODEL-BACKENDS-VERIFICATION.md`, and `.kiro/specs/model-backend-adapters/*` are cited throughout the code as the source of truth for the capability matrix and the Req numbers. They carry uncommitted user edits and were explicitly out of bounds, so every "Req N" reference in the code (e.g. direct_api.py:12-18, bedrock.py:317-320) is unverified against its spec.
- **`tests/test_backends.py` and the other role-matrix tests** beyond `test_resolver.py` were only sampled via grep, not read in full; there may be additional pinned assertions that would break under the recommended fixes.

RELEVANT FILES (ranked by importance):
  1. config/backends.toml — The profile×role matrix itself: all five profiles, the laptop default, the Kiro and Fable 5.1 cells, the invariant skim row, and the self-contradicting header/cost comments.
  2. src/backends/resolver.py — How the matrix is actually resolved: default fallback, SECOND_BRAIN_PROFILE, _validate_profile, ADAPTER_OPTIONS whitelist, invoker cache, and the unreachable codex adapter registration.
  3. src/backends/cost.py — All USD math: dual price tables, Flex tier logic, the tier-blind second price_for lookup, the zero-cost early return that loses call counts, and the ceiling for unknown models.
  4. src/backends/base.py — The capability table and assert_backend_supports_role — the only code-level enforcement of the skim rule, and the place the missing no-Kiro/no-Fable rules belong; also the unread structured_output flag and stale docstrings.
  5. src/express.py — The Express editor path that bypasses the resolver entirely, hardcodes Kiro via the compatibility shim, and spends without any SpendTracker.

NOTE: Line ranges have been extended by ~50 lines to provide additional surrounding context.

---

