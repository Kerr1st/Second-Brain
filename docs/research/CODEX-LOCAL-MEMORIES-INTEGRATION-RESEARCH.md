# Codex Local Memories and Second Brain Integration

**Research date:** 2026-08-18
**Purpose:** Design input for integrating Second Brain with Codex local memory and project context
**Status:** Research synthesis and recommendation, not an implementation decision

## Executive conclusion

Second Brain should not write directly into `~/.codex/memories/` or treat Codex's generated memory
files as a stable integration API. OpenAI documents that directory as generated state and explicitly
says hand-editing it should not be the primary control surface
([Codex memories](https://learn.chatgpt.com/docs/customization/memories)).

The strongest design is a **provenance-preserving recall broker**:

1. Second Brain remains the durable, queryable source of memories and their evidence.
2. Its existing MCP surface supplies a small, task-scoped recall packet to Codex when needed.
3. A project-scoped skill defines when and how Codex retrieves that packet, taking advantage of
   Codex's documented progressive disclosure for skills.
4. Optional lifecycle hooks may preload a bounded packet at session start, refresh it per user
   prompt, and restore it after compaction.
5. Human-reviewed durable rules are promoted to `AGENTS.md` or checked-in project documentation,
   not silently copied into Codex's generated memory store.
6. Every injected Second Brain item carries stable provenance, authority, temporal validity, and
   supersession state so an answer can distinguish evidence from inference and current rules from
   historical context.

This uses the extension points OpenAI does document: `AGENTS.md` for persistent project guidance,
skills for reusable progressively disclosed workflows, and MCP for external tools and context
([Codex customization](https://learn.chatgpt.com/docs/customization/overview),
[Model Context Protocol](https://learn.chatgpt.com/docs/extend/mcp)). Codex native memories can remain
enabled as an opportunistic personal recall layer, but they should not become Second Brain's system
of record.

## What the official documentation establishes

### The local Codex memory store is separate from ChatGPT web memory

OpenAI distinguishes ChatGPT web memory from the local memory store used by Codex clients. The
ChatGPT desktop app, Codex CLI, and IDE extension can use the local store associated with the Codex
host; ChatGPT Work uses account/workspace memory instead of the local Codex store
([Codex memories](https://learn.chatgpt.com/docs/customization/memories)). This means an integration
with local Codex memory should not be assumed to propagate to ChatGPT web or ChatGPT Work.

The current configuration documentation labels `features.memories` **Experimental** and shows it as
off by default. When the feature is enabled, `memories.generate_memories` and
`memories.use_memories` each default to true
([config basics](https://learn.chatgpt.com/docs/config-file/config-basic#feature-flags),
[configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)). An
integration should therefore tolerate the feature being unavailable or disabled and must not make
native memories a hard dependency.

### Generation is asynchronous and eligibility-gated

When enabled, Codex derives memory files from eligible prior chats in the background. It skips
active or short-lived sessions, waits until a chat has been idle long enough, redacts secrets from
generated memory fields, and may skip a pass when remaining Codex rate-limit capacity is below the
configured threshold
([Codex memories](https://learn.chatgpt.com/docs/customization/memories)).

The configuration reference documents additional bounds: by default, a thread must be idle for six
hours, threads older than 30 days are not considered, at most 16 rollout candidates are processed
per startup pass, global consolidation considers at most 256 recent raw memories, and memories
unused for more than 30 days become ineligible for consolidation. These values are configurable
within documented limits
([configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)).

OpenAI names separate model overrides for per-thread extraction and global consolidation. This
supports a two-stage interpretation—chat-level extraction followed by broader consolidation—but
the documentation does not publish either prompt, output schema, consolidation algorithm, or
quality/confidence contract
([Codex memories](https://learn.chatgpt.com/docs/customization/memories),
[configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)).

### Storage is inspectable generated state, not a supported write API

Local state lives under `CODEX_HOME`, which defaults to `~/.codex`; the main memory files live under
`~/.codex/memories/`. OpenAI describes their contents broadly as summaries, durable entries, recent
inputs, and supporting evidence from prior chats
([Codex memories](https://learn.chatgpt.com/docs/customization/memories),
[advanced configuration](https://learn.chatgpt.com/docs/config-file/config-advanced#config-and-state-locations)).

The files may be inspected for troubleshooting or before sharing the Codex home directory, but
OpenAI says to treat them as generated state and not to rely on manual edits as the primary control
surface. The public documentation does not define a supported external import/write API, stable
file schema, mutation protocol, or callback when generation/consolidation completes
([Codex memories](https://learn.chatgpt.com/docs/customization/memories)). Therefore, any direct
writer into this directory would depend on undocumented internals and risk being overwritten or
invalidated by product changes. That final sentence is a design inference from the documented
absence of a write contract, not a stated OpenAI guarantee.

### Use and generation are separately controllable

`/memories` lets a user control whether a chat may use existing local memories and whether that chat
may contribute to future memory generation. Chat-level choices do not change global settings
([Codex memories](https://learn.chatgpt.com/docs/customization/memories)). The CLI command description
says the selected behavior updates the relevant settings for future sessions
([Codex slash commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli#configure-memories-with-memories)).

Global/config-based controls separate generation from retrieval:

- `memories.generate_memories` controls whether newly created threads are stored as generation
  inputs.
- `memories.use_memories` controls whether existing memories are injected into future sessions.
- `memories.disable_on_external_context` can exclude threads that used MCP, web search, or tool
  search from generation.
- `memories.extract_model` and `memories.consolidation_model` select optional model overrides.

These settings and their defaults are documented in the
[configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference). Codex
configuration can be set globally or in a trusted project's `.codex/config.toml`, with the nearest
trusted project layer taking precedence over user configuration
([config basics](https://learn.chatgpt.com/docs/config-file/config-basic)).

There is a small documentation ambiguity worth testing before implementation: the Memories page
says chat-level choices do not change global settings, while the CLI command page says `/memories`
updates the relevant settings for future sessions
([Codex memories](https://learn.chatgpt.com/docs/customization/memories#control-local-memories-per-chat),
[Codex slash commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli#configure-memories-with-memories)).
The public pages do not explain exactly how long a per-chat selection persists.

### Retrieval injection is documented only at a control level

OpenAI documents that `memories.use_memories = false` causes Codex to skip injecting existing
memories into future sessions, establishing that an injection step exists
([configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)). The public
documentation does not specify retrieval ranking, project/path filters, prompt budget, how many
memories are injected, whether use changes future ranking, or how citations to supporting evidence
are selected. A Second Brain integration should therefore not depend on native-memory retrieval
semantics that OpenAI has not committed to publicly.

### Project context, chat transcripts, and memories are different scopes

A local project gives chats access to attached folders, while each chat retains its own transcript.
The primary folder is the default working directory and is used for automatic discovery of
`AGENTS.md`, skills, and `config.toml`; secondary attached folders can be searched and edited but do
not receive that automatic discovery
([Projects and chats](https://learn.chatgpt.com/docs/projects)).

For CLI work, the chat keeps its transcript and recorded working directory while Codex reads the
current working tree. OpenAI recommends separate chats for distinct outcomes and says durable
project guidance belongs in `AGENTS.md` or checked-in documentation
([Projects and chats](https://learn.chatgpt.com/docs/projects)). The memory documentation likewise
describes memories as helpful recall rather than the only source for rules that must always apply
([Codex memories](https://learn.chatgpt.com/docs/customization/memories)).

The public memory documentation does not state that native memories are partitioned, filtered, or
namespaced by local-project path. Project association may appear in generated artifacts, but path
scoping should be treated as undocumented behavior unless verified against a supported contract.

### Compaction is transcript management, not durable memory generation

`/compact` replaces earlier visible chat turns with a concise summary to free context while keeping
critical details. `/resume` reloads the saved transcript, and `/fork` clones the current chat into a
new chat with a fresh ID
([Codex slash commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)). OpenAI does
not document `/compact` as writing to the local memory store. Second Brain should therefore record
source turns and its own distilled artifacts independently rather than treating a compacted summary
as the only durable evidence.

Codex can also compact automatically at a configurable token threshold; the default threshold comes
from the model when `model_auto_compact_token_limit` is unset, and the counting scope may be the
whole active context or only growth after the carried compaction prefix
([configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)). OpenAI does
not document whether memory extraction consumes original pre-compaction turns, compacted summaries,
or both.

### Provenance exists, but its public contract is intentionally broad

OpenAI says the local memory files include supporting evidence from prior chats and that secrets are
redacted from generated memory fields
([Codex memories](https://learn.chatgpt.com/docs/customization/memories)). It does not document a
stable evidence-link schema, immutable source identifiers, confidence values, temporal validity,
supersession, contradiction handling, or a user-facing citation guarantee. Second Brain's Exact
Provenance should therefore remain independent and should not be replaced by native Codex memory
metadata.

## Documented extension points that fit Second Brain

### Special-purpose memory inputs are documented, but no general provider API is

OpenAI documents two product-owned ways non-Codex material can enter local memories:

- Computer History creates the same kind of plain-text Markdown local memories under
  `$CODEX_HOME/memories/extensions/skysight/`. Its files can be inspected and modified, but this is
  a named Computer History path with its own permissions, retention, and privacy model
  ([Computer History](https://learn.chatgpt.com/docs/customization/computer-history#where-does-computer-history-store-my-data)).
- The supported import flow can import project memories from Claude Code into Codex Memories
  ([Import from another agent](https://learn.chatgpt.com/docs/import#how-importing-works)).

The public documentation does not define a registry for arbitrary memory providers, a supported
`extensions/<provider>` contract, a memory API, or a hook for custom memory ingestion. The named
`skysight` directory and Claude Code import should therefore not be generalized into a Second Brain
write contract without additional official support.

### 1. MCP as the runtime recall boundary

OpenAI describes MCP as the standard connection from Codex to external tools and context. Local
Codex clients support STDIO and streamable HTTP servers, and MCP servers can expose tools, resources,
prompts, and initialization instructions. MCP configuration can be global or project-scoped in a
trusted `.codex/config.toml`
([Model Context Protocol](https://learn.chatgpt.com/docs/extend/mcp),
[Codex customization](https://learn.chatgpt.com/docs/customization/overview)).

**Recommendation:** expose Second Brain recall through bounded, read-only-first MCP operations such
as `memory_search`, `memory_get`, and `memory_context_pack`. Keep writes or feedback as separately
approved operations. The context-pack response should contain stable IDs, source-turn references,
semantic project, capture source, ownership, timestamps, authority level, supersession state, and a
short reason each memory matched.

### 2. A project-scoped skill as the retrieval policy

OpenAI documents progressive disclosure for skills: Codex starts with name/description metadata,
loads `SKILL.md` only when selected, and reads references or runs scripts only when needed. Skills
may be global or checked into `.agents/skills` for a repository
([Codex customization](https://learn.chatgpt.com/docs/customization/overview#skills)).

**Recommendation:** add a future `second-brain-recall` skill whose small front page says when recall
is warranted, how to form a query, which MCP operations are read-only, what evidence must be cited,
and when to abstain. Put detailed schemas and examples in references so they consume context only
when needed. This lets retrieval policy evolve without filling every prompt with the full Second
Brain manual.

### 3. `AGENTS.md` and checked-in docs as the promotion target

Codex loads `AGENTS.md` before work, layering global and project guidance from the project root to
the working directory. Closer files override broader guidance, and the combined discovery chain has
a documented size limit. OpenAI explicitly recommends putting recurring corrections into the
closest appropriate `AGENTS.md`
([Codex `AGENTS.md`](https://learn.chatgpt.com/docs/agent-configuration/agents-md),
[Codex customization](https://learn.chatgpt.com/docs/customization/overview#when-to-update-agentsmd)).

**Recommendation:** Second Brain may propose a promotion, but a human should approve its wording,
scope, and destination:

| Knowledge kind | Durable destination |
|---|---|
| Required project behavior | Root or nearest scoped `AGENTS.md` |
| Stable project fact or decision | Checked-in architecture/decision documentation |
| Reusable multi-step workflow | Project or personal skill |
| Searchable evidence, history, or tentative insight | Second Brain only, retrieved through MCP |
| Mechanically verifiable invariant | Test, lint, hook, or CI after separate review |

The table is a Second Brain design recommendation based on the documented roles of guidance,
memories, skills, and MCP; it is not an OpenAI-prescribed taxonomy.

### 4. Hooks as an optional automatic injection layer

Codex hooks are a documented lifecycle extension point. `SessionStart` receives a source of
`startup`, `resume`, `clear`, or `compact`; its plain-text output or JSON `additionalContext` becomes
extra developer context. `UserPromptSubmit` receives the prompt about to be sent and can likewise
add developer context. The current implementation ignores matchers for `UserPromptSubmit`, so any
configured hook runs for every submitted prompt
([Codex hooks](https://learn.chatgpt.com/docs/hooks#sessionstart),
[Codex hooks: `UserPromptSubmit`](https://learn.chatgpt.com/docs/hooks#userpromptsubmit)).

Compaction has two related seams. `PreCompact` and `PostCompact` run around manual or automatic
compaction and can stop continuation, but their documented outputs do not define an
`additionalContext` field. After a root-session compaction, however, a `SessionStart` hook matching
`source: "compact"` runs before the next model request and can inject context into that immediate
continuation. This is the supported place to restore the small Second Brain packet after compaction
([Codex hooks: compaction](https://learn.chatgpt.com/docs/hooks#precompact),
[Codex hooks: `SessionStart`](https://learn.chatgpt.com/docs/hooks#sessionstart)).

Hook context is model-visible and budget-sensitive. Codex uses an approximately 2,500-token default
per-handler limit for `additionalContext`, spills oversized output to a temporary file with a shorter
preview, and warns that accumulated hook/plugin context can degrade model performance. Non-managed
command hooks require user trust review, and the hook-provided `transcript_path` is explicitly not a
stable interface
([Codex hooks: large output](https://learn.chatgpt.com/docs/hooks#large-hook-output),
[Codex hooks: common input](https://learn.chatgpt.com/docs/hooks#common-input-fields)).

**Recommendation:** treat hooks as an optional convenience, not the retrieval system itself.

- Use `SessionStart(startup|resume|compact)` to inject only approved guidance, current decisions,
  and unresolved conflicts that fit a strict packet budget.
- Use `UserPromptSubmit` only when prompt-aware recall has measured value; otherwise prefer
  skill-initiated MCP retrieval to avoid a model call or search on every turn.
- Use `PreCompact` only for side-effectful checkpointing if needed; use the following
  `SessionStart(source=compact)` event for actual reinjection.
- Never parse `transcript_path` as the primary capture contract; retain Second Brain's own
  provenance-aware Codex Task capture path.

### 5. Plugin packaging for distribution, not memory storage

OpenAI describes a plugin as an installable package that can bundle skills, MCP-backed capabilities,
and hooks. It recommends starting with a skill while iterating on one personal workflow and moving
to a plugin when sharing a stable capability or connecting an external service. Plugin-bundled hooks
still require trust review
([Build plugins](https://learn.chatgpt.com/docs/build-plugins),
[Plugins](https://learn.chatgpt.com/docs/plugins)).

**Recommendation:** develop the Context Broker first as the existing Second Brain MCP server plus a
repo-scoped skill. Package the skill, MCP connection, and optional hooks as a Second Brain plugin
only after the packet contract and trust model stabilize. This makes installation and team sharing
easier without confusing packaging with the memory system of record. Account for the documented
surface gap: plugins are available in the desktop app and CLI, but not in the IDE extension
([Plugins](https://learn.chatgpt.com/docs/plugins)).

## Recommended feature: Memory Context Broker

### User experience

For a new Codex chat inside a trusted project:

1. Codex sees only the small `second-brain-recall` skill description.
2. An optional `SessionStart` hook supplies a minimal warm-start packet. When the task appears to
   depend on prior decisions, corrections, or preferences, the skill asks
   Second Brain for a context pack using the current working directory, explicit task terms, and
   any semantic project named by the user.
3. Second Brain returns a token-bounded packet, grouped by authority:
   **approved guidance**, **current decisions**, **relevant evidence**, and **possible conflicts**.
4. Codex cites the stable Second Brain IDs it actually used and states uncertainty when evidence is
   incomplete or contradictory.
5. At task completion, Second Brain may capture the user-owned visible task through its existing
   capture path. It does not mutate Codex's native generated memory files.
6. A later Dream Cycle may propose promotion of repeated, well-supported corrections. A human
   approves any write to `AGENTS.md`, project documentation, a skill, or deterministic enforcement.

### Context-pack contract

Each returned item should include at least:

```text
memory_id
memory_class
title
concise_content
semantic_project
source_system
source_task_id
supporting_turn_ids
observed_at
valid_from / valid_to (when known)
supersedes / superseded_by
authority: evidence | inferred | approved
ownership: user-owned | delegated | unknown
retrieval_reason
```

This is a proposed Second Brain contract, not a Codex-native schema. It preserves distinctions that
the public native-memory contract does not guarantee: source ownership, semantic project versus
workspace provenance, exact turn evidence, authority, temporal validity, and supersession.

### Preventing recursive memory amplification

There is a specific feedback-loop risk: Second Brain injects a derived memory through MCP, Codex
repeats it, native Codex memory generation extracts the repetition, Second Brain later captures that
chat, and the same claim appears to gain support merely by circulating through two memory systems.

**Recommendation:** configure `memories.disable_on_external_context = true` for the environment or
trusted projects where Second Brain MCP recall is routinely used. OpenAI documents that this setting
keeps MCP/web/tool-search chats out of native memory generation
([Codex memories](https://learn.chatgpt.com/docs/customization/memories),
[configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)). The tradeoff
is explicit: such chats will not become native-memory inputs, so Second Brain capture becomes the
durable path for those tasks. If native generation remains enabled instead, Second Brain should tag
  injected IDs and refuse to treat later repetitions of those IDs as independent evidence.

### Local-storage security boundary

Memory content must be treated as locally sensitive data. OpenAI tells users not to store secrets,
to review memory files before sharing `CODEX_HOME`, and says secret redaction applies to generated
fields rather than providing a general secrecy guarantee
([Codex memories](https://learn.chatgpt.com/docs/customization/memories#review-local-memories)).
Computer History further documents that its generated Markdown memories are not encrypted by that
feature and may be accessible to other processes running as the same macOS user
([Computer History](https://learn.chatgpt.com/docs/customization/computer-history#where-does-computer-history-store-my-data)).
The Context Broker should minimize returned content, avoid secrets, and preserve source access
controls rather than copying full documents into a packet.

### Evaluation and observability

Store a recall exposure record for every packet:

- query and project scope;
- returned and actually used memory IDs;
- rank/score components and packet token count;
- source and derivation versions;
- conflicts or abstentions;
- task outcome and user correction, when explicitly observed.

This ledger is a Second Brain recommendation. It fills observability gaps in the public Codex
memory contract and makes it possible to measure useful recall, false recall, cross-project leakage,
stale-memory use, provenance coverage, and whether retrieval influenced a correction.

## Suggested delivery sequence

### Phase 1: Safe recall

- Keep `~/.codex/memories/` read-only from Second Brain's perspective.
- Define the context-pack response on top of the existing MCP server.
- Add the small project-scoped recall skill.
- Require stable Second Brain provenance in every returned item.
- Add an exposure ledger and fixture-based tests for project scope, ownership, supersession, and
  prompt-budget limits.
- Prototype `SessionStart(source=compact)` reinjection behind an opt-in hook only after the packet
  contract works through explicit skill/MCP retrieval.

### Phase 2: Reviewed promotion

- Turn repeated Correction Episodes into reviewable Steering Candidates.
- Let the user choose `AGENTS.md`, checked-in documentation, a skill, automation, or no promotion.
- Preserve the candidate-to-episode evidence chain and record later supersession.

### Phase 3: Measured personalization

- Learn retrieval preferences from explicit task outcomes, not from mere model repetition.
- Compare no-recall, native-memory-only, and Second-Brain-context-pack conditions.
- Tune ranking and packet size only from measured retrieval utility and correction rates.

## Key limitations and open questions

1. **No documented native import API:** the official memory documentation exposes configuration and
   inspection, not a supported external writer.
2. **No documented native retrieval contract:** selection, ranking, prompt budget, path scoping, and
   evidence-citation behavior are unspecified.
3. **No documented native change event:** there is no public callback for completed extraction or
   consolidation.
4. **Special inputs are product-specific:** Computer History and the supported external-agent import
   do not establish an arbitrary provider API.
5. **Hook transcript format is unstable:** hooks receive a convenience path, not a stable task-capture
   schema.
6. **Compact hooks are asymmetric:** the supported post-compaction injection seam is
   `SessionStart(source=compact)`, not an `additionalContext` contract on `PreCompact` or
   `PostCompact`.
7. **Plugin reach differs by client:** current documentation excludes the IDE extension.
8. **External-context exclusion is coarse:** excluding any chat that used MCP prevents feedback
   loops but also prevents useful native extraction from that whole chat.
9. **Desktop, CLI, IDE, and web are not one memory plane:** local Codex clients can share a host
   store, while ChatGPT web/Work use separate memory mechanisms.
10. **Compaction is lossy by design:** it keeps critical details in a summary but does not replace
   exact, independently captured source turns.

Items 1–4 are statements about what the current public documentation does not define, not claims
about undisclosed implementation behavior. Items 5–10 follow from the documented settings and
surface distinctions cited above.

## Decision recommendation

Adopt the **Memory Context Broker** as the target architecture and explicitly reject direct writes
to Codex's generated memory directory. Make Second Brain responsible for durable evidence,
provenance, retrieval, temporal truth, and promotion review; use Codex native memories for optional
personal recall, MCP for runtime access, a skill for progressive retrieval policy, optional hooks
for bounded automatic injection, plugin packaging for distribution, and `AGENTS.md` or checked-in
docs for approved authority.

Before implementation, record an ADR that fixes four boundaries:

1. Codex native memory files are never an authoritative Second Brain store.
2. Injected memories never count as independent supporting evidence when recaptured.
3. Only user-owned source turns may create or strengthen personal/project knowledge.
4. Promotion to persistent guidance always requires explicit approval and exact provenance.
