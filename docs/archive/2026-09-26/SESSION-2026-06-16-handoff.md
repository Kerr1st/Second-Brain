# Session Handoff — 2026-06-16 (Model Backends feature)

> Resume context for after compaction. Read this + `docs/MODEL-BACKENDS.md` +
> `docs/MAC-MINI-MIGRATION.md` to get fully up to speed. Repo:
> `/Users/kerrfir/second-brain`. Run tests with `.venv/bin/python -m pytest`
> (needs local Postgres on `127.0.0.1:5432`; embeddings are mocked).

## Where we are in one paragraph

We built a **pluggable model-backend layer** for the dream cycle so it can run on a
chosen backend instead of being hard-wired to Kiro/Amazon Q. The **Kiro adapter is
the only one implemented** (demand-driven plan); the abstraction + config + Kiro
refactor are done, tested (**full suite: 685 passed**), and **default-preserving**
(today's all-Kiro/Opus behavior is byte-for-byte reproduced). Everything is
**uncommitted** in the working tree — the user drives commits. The migration of the
brain to the Mac Mini is a **separate** workstream (`docs/MAC-MINI-MIGRATION.md`)
that *consumes* this refactor.

## The feature (intent — keep it this simple)

- Backend is an **instance-level choice set at setup**, not per-call magic.
  - **Option 1 (default): Kiro CLI** → Amazon Q, $0 metered, works today.
  - **Option 2 (optional): one alternative agentic CLI** — Claude Code or Codex.
    Claude Code on Bedrock is the Mac Mini's path.
- **Direct-API backends (Bedrock/OpenAI/Anthropic) are tool-less** → they CANNOT run
  the Explorer (needs the live MCP tool-loop) → they're a per-stage option, never a
  whole-instance runner.
- **Locked tenets (unchanged):** deterministic Python orchestrator; 4-evaluator BFT
  consensus (≥3/4); Explorer must be agentic (MCP-capable); default-preserving.
- **Deferred follow-ons** (interface keeps the door open): evaluator-panel model
  *diversity*, and Fable-5-on-the-Thinker. NOT being built now.

## What was implemented (code map)

New package `src/backends/`:
- **`base.py`** — `Invoker` Protocol (runtime_checkable); `InvocationResult` TypedDict
  = `{output, raw, usage, usage_source}` (`usage_source` ∈ `"real"|"estimate"`);
  `BackendCapabilities` (flags `supports_mcp, metered, structured_output,
  reports_usage`) with invariant **`metered ⇒ reports_usage`**; `BACKEND_CAPABILITIES`
  table (kiro/claude_code/codex/bedrock); `assert_backend_supports_role` (Explorer ⇒
  must be MCP-capable, else `ValueError`).
- **`kiro.py`** — `KiroInvoker` = the former `AgentInvoker`, moved here unchanged in
  behavior. Returns `usage=None, usage_source="estimate"`. Still holds the outage-class
  hardening (cwd=repo MCP server, `--require-mcp-startup`, exit-3 retry, Phase-0
  metrics) and `parse_json_output`/`_balanced_json_spans`/`_ANSI_RE`.
- **`resolver.py`** — loads `config/backends.toml`; `RoleBackend` (provenance spec);
  `Resolver` caches one Invoker per `(backend, model)` (all-Kiro ⇒ one shared invoker,
  like today); eager Explorer guard; `NotImplementedError` for unbuilt adapters;
  `default_resolver()` selects the profile (`SECOND_BRAIN_PROFILE` env, else
  `default_profile`) and **fail-fast `_validate_profile`** (active profile must define
  exactly the 6 `VALID_ROLES`, else clear `ValueError` at load — never mid-run).

Wiring + compat:
- **`src/agent_invoker.py`** — now a backward-compat shim re-exporting
  `KiroInvoker as AgentInvoker` (+ module-level names). Old imports/tests keep working.
- **`src/dream_cycle/orchestrator.py`** — `self.resolver = default_resolver()` in
  `__init__`; `_invoker_for(role)`; per-role `effort` from `resolver.spec_for(role)`.
  Sequence/prompts/consensus/storage/digest untouched.
- **`config/backends.toml`** — profiles `laptop` (all-Kiro/Opus, the committed default,
  reproduces today: explorer/evaluators `effort=high`, thinker `max`) and `mini`
  (claude_code/Opus — **adapter not built, selecting it errors**). No secrets here.

Tests: `tests/test_backends.py` (capability invariants, Explorer guard, Protocol),
`tests/test_resolver.py` (profile load, default-preserving, caching, fail-fast
validation), updated `tests/test_agent_invoker.py` (mocks now target
`src.backends.kiro`; asserts `usage_source`).

## Verification

- **Full suite: 685 passed** (~28s), 1 pre-existing unrelated Starlette/httpx warning.
- Default-preserving proven by `test_laptop_profile_reproduces_today` +
  `test_all_kiro_profile_shares_one_invoker`.

## Git state (HEAD `7ea87d0`, branch `main`) — ALL UNCOMMITTED

- **Staged:** `config/backends.toml`.
- **Untracked (the feature — commit together):** `src/backends/`,
  `tests/test_backends.py`, `tests/test_resolver.py`, `docs/MODEL-BACKENDS.md`,
  `docs/MAC-MINI-MIGRATION.md`.
- **Modified (the feature):** `src/agent_invoker.py`, `src/dream_cycle/orchestrator.py`,
  `tests/test_agent_invoker.py`, `tests/test_dream_cycle.py`, `tests/test_consensus.py`,
  `tests/test_integration.py`, `tests/test_orchestrator_properties.py`.
- **Modified, UNRELATED (commit separately):** `scripts/jobs/backup.sh` (fixes
  `cd ".."`→`cd "../.."` — backups were landing in `scripts/backups/`, the ~21 GB there;
  + GDrive verify no longer false-fails on transient rate-limits), `verify_backup.sh`,
  `verify_liveness.sh`.
- The user drives commits — **do not commit without an explicit ask.**

## What we did this session

Code-reviewed the backend implementation. Found one real bug (config uncommitted) +
gaps. Then, per the user's go-ahead: **(A)** staged `config/backends.toml`; **(B)** added
the `usage_source` field + the fail-fast profile validation + 2 tests. Verified 685 pass.

## Next steps / open items (priority order)

1. **Commit** the backend feature (config + src/backends + tests + orchestrator/shim +
   the two docs) when the user says so; commit the job-script fixes separately.
2. **"Before the Claude Code adapter" enhancements** (from the review, not yet done):
   - Persist `(backend, model)` provenance to the run/candidate DB record (build-step 4;
     only `effort` reaches the metrics JSONL today, and `backend`/`model` reach neither
     the metrics nor the decision record).
   - Hoist `parse_json_output`/`_balanced_json_spans`/`_ANSI_RE` from `kiro.py` →
     `base.py` (shared backstop) so `ClaudeCodeInvoker` doesn't import from `kiro`.
   - **Backend-namespaced model IDs:** `mini` profile pre-fills `"claude-opus-4.8"` (Kiro
     name); Claude Code wants `opus` / `us.anthropic.claude-opus-4-8`. Adapter needs a
     model-ID map.
   - Align `mini` evaluator cost: route evaluators to **Haiku-class**, not Opus-on-Bedrock
     (cost posture — BFT tolerates a weaker per-judge model).
   - Backend-name validation with a clear "unknown backend" error.
3. **Build `ClaudeCodeInvoker` (on Bedrock).** Code + mocked tests are buildable on this
   laptop; **empirical** verification (tools attach, fail-loud, parity vs Kiro) must run
   where Claude Code actually lives. NOTE: this laptop's `claude` is an **ASBX/internal
   build** (`/Users/kerrfir/.toolbox/bin/claude`, v2.1.177) whose auth/metering may differ
   from public Claude Code on the Mini. Each adapter must honor **failure-mode parity**
   (CLI failures → `TimeoutError`/`RuntimeError`, never a silent fake REJECT — the
   evaluator integrity fix depends on it) and a **startup probe** (one trivial tool call),
   not just process-attach.
4. **Migration decisions** (`docs/MAC-MINI-MIGRATION.md`, all OPEN): capture-locality —
   **A split (recommended)** vs B full source-sync; parity tolerance + Explorer-port
   fallback; scheduled-context shadow run before cutover; rollback discipline.

## Key facts / gotchas

- **Mac Mini:** `KerrClaws-Mini`, `kerrclaw@192.168.1.224`, Apple M4 / **16 GB shared
  with OpenClaw** / 67 GB free, macOS 26.5.1, Python 3.14.5. Has rclone (`gdrive-backup:`),
  tailscale (installed, not up), bare git repo `kerrclaw@…:repos/second-brain.git`.
  **Missing:** Kiro, postgresql@17/pgvector, gpg, awscli. Runtime files hardcode
  `/Users/kerrfir` → need `/Users/kerrclaw`.
- **cwd outage lesson (Jun 8–12):** launchd runs with cwd=`/`; the MCP server
  (`python -m src.mcp_server`) needs cwd=repo. Fixed via `WorkingDirectory` +
  `--require-mcp-startup` (loud fail). Carry this to every backend adapter.
- **Claude Code on Bedrock:** `CLAUDE_CODE_USE_BEDROCK=1` + `awsAuthRefresh` =
  *mostly*-unattended (silent SSO refresh within the session window, then human re-auth).
  Truly hands-off = dedicated **Bedrock-invoke-only IAM principal** (or AWS-discouraged
  long-term Bedrock API key). **Bedrock creds are needed regardless** — embeddings
  (`src/embeddings.py`, Titan) always call Bedrock.
- Dream cycle = Explorer (agentic, MCP) → Thinker (≤5 slices) → 4 evaluators (tool-less,
  BFT ≥3/4). Evaluators are the call-volume hog (~30/run).
