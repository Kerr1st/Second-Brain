# From User Corrections to Durable Agent Steering

**Research date:** 2026-07-22
**Purpose:** Design input for Second Brain's Codex Task capture
**Status:** Research synthesis and recommendation, not an implementation decision

The accepted decisions after review are recorded in
[`ADR 0007`](../adr/0007-capture-correction-episodes-before-steering.md). Where this research proposes
additional capture-time fields or numerical candidate heuristics, ADR 0007's smaller Build 1 model
and existing Dream Cycle quality gate govern.

## Executive conclusion

The strongest design is not to turn every correction directly into a rule. It is to preserve a
small, immutable **Correction Episode** with Exact Provenance, then let the Dream Cycle find repeated
or explicitly durable lessons and propose a **Steering Candidate** for human review. Only an approved
candidate becomes a **Steering Rule**. A rule may later become an **Automation Candidate** when a
machine can verify it more reliably than a model can remember it.

This keeps five concerns separate:

1. **Evidence:** what the user and agent actually said.
2. **Interpretation:** what appears to have been wrong or misaligned.
3. **Generalization:** what future agents might do differently.
4. **Authority:** whether the user approves that generalization and its scope.
5. **Enforcement:** whether guidance remains probabilistic or graduates to code.

For Codex v1, the simplest useful addition is one more output in the existing combined Task
Semantic Pass: zero or more Correction Episodes beside Decisions and Insights. No real-time hook,
new model call, automatic steering-file write, arbitrary time decay, or new cross-integration
framework is needed.

## Starting point and settled Second Brain constraints

The motivating LinkedIn article proposes a loop of correction, episode, recurring pattern, rule,
relevant injection, and eventually deterministic enforcement. It explicitly says that its
three-occurrence promotion threshold, 14-day active window, 30-day archive interval, and
30-episode cap are choices made for that custom hook system, not Kiro defaults
([Landreau, 2026](https://www.linkedin.com/pulse/kiro-harness-engineering-three-days-from-fast-jean-francois-landreau-i4z6f/)).

This research assumes Second Brain's already settled Codex architecture:

- capture after six hours of Codex Task inactivity;
- capture only complete user prompts and visible final answers;
- group Agent Turns into Topic Segments;
- run segmentation and task distillation in one combined semantic pass;
- append source evidence monotonically and never rewrite established turns;
- retain Exact Provenance from derived artifacts to supporting Agent Turns;
- already emit Decisions and Insights;
- use the Dream Cycle for later durable consolidation;
- never edit steering files automatically without user approval; and
- prove the behavior on Codex before generalizing it to other integrations.

## What primary sources establish

### Persistent instructions and memory are different things

OpenAI's Codex documentation says memories are a helpful recall layer, while required team guidance
belongs in `AGENTS.md` or checked-in documentation. It also documents local memory generation from
eligible idle chats, supporting evidence, per-chat controls, secret redaction, and an option to
exclude chats that used external context from memory generation
([Codex memories](https://learn.chatgpt.com/docs/customization/memories)). This directly supports a
reviewed promotion boundary: derived memory can suggest a rule, but must not silently become the
authoritative rule.

Codex discovers `AGENTS.md` from global through increasingly specific project directories, with
nearer instructions taking precedence
([Codex `AGENTS.md`](https://learn.chatgpt.com/docs/agent-configuration/agents-md)). Claude Code
similarly separates managed, user, project, and local instructions; recommends concise, concrete,
non-conflicting rules; and notes that path-scoped rules reduce irrelevant context
([Claude Code memory](https://code.claude.com/docs/en/memory)). Kiro documents global and workspace
steering, gives workspace steering precedence on conflict, and supports always, file-match, manual,
and semantic auto-inclusion modes
([Kiro steering](https://kiro.dev/docs/steering/)). Cursor separates global User Rules,
version-controlled Project Rules, and automatically generated repository-scoped Memories that users
can inspect and delete
([Cursor rules and memories](https://docs.cursor.com/context/rules)).

Together, these products establish a common pattern: durable instructions need explicit scope,
visible ownership, and a predictable precedence rule; relevance-based inclusion is preferable to
loading all accumulated guidance into every task.

### Feedback can improve behavior without changing model weights

[Reflexion](https://arxiv.org/abs/2303.11366) turns external or internally simulated feedback into
short verbal reflections retained in an episodic memory buffer for later attempts.
[ExpeL](https://arxiv.org/abs/2308.10144) stores experiences, extracts natural-language insights,
and retrieves both at inference time. [Self-Refine](https://arxiv.org/abs/2303.17651) shows that
iterative feedback and refinement can improve an output within a task, but it does not by itself
establish a cross-task persistence or promotion policy.

These papers support the general feedback-to-memory pattern. They do **not** establish that every
user correction is a reusable rule, that three repetitions is an optimal threshold, or that a
model's self-critique has the same authority as explicit user feedback.

### Raw episodes, derived reflections, and retrieval should remain separable

[Generative Agents](https://arxiv.org/abs/2304.03442) stores an experience stream, synthesizes
higher-level reflections, and retrieves memories using relevance, importance, and recency.
[MemGPT](https://arxiv.org/abs/2310.08560) separates limited in-context memory from larger external
memory tiers. Letta's current implementation distinguishes always-visible memory blocks, searchable
files, archival memory, and external retrieval; blocks can also be read-only
([Letta context hierarchy](https://docs.letta.com/guides/core-concepts/memory/context-hierarchy),
[Letta memory blocks](https://docs.letta.com/guides/core-concepts/memory/memory-blocks)).

The useful lesson for Second Brain is architectural rather than numerical: preserve source episodes,
derive higher-order artifacts separately, and retrieve only the most applicable material. Recency
may influence attention, but it is not evidence that a correction has become false.

### Updates and contradictions require temporal reasoning and abstention

[LongMemEval](https://arxiv.org/abs/2410.10813) treats knowledge updates, temporal reasoning, and
abstention as distinct long-term-memory capabilities. Mem0's documented add pipeline extracts facts,
checks existing memories for duplicates or contradictions, and updates toward the latest truth
([Mem0 add memory](https://docs.mem0.ai/core-concepts/memory-operations/add)). Graphiti instead keeps
raw episodes as provenance and gives derived facts validity windows so superseded facts are
invalidated rather than deleted
([Graphiti source and model](https://github.com/getzep/graphiti)).

Second Brain's monotonic evidence policy is closer to Graphiti's provenance-preserving approach than
to destructive latest-value replacement. Corrections should append evidence. Decisions or rules can
be superseded at the derived layer while their source history remains intact.

### Guidance and deterministic enforcement are different mechanisms

Kiro explicitly separates steering files from hooks; its Prompt Submit hook can add context, while
shell actions provide deterministic behavior for suitable events
([Kiro hook actions](https://kiro.dev/docs/hooks/actions),
[Kiro hook types](https://kiro.dev/docs/hooks/types/)). Claude Code describes hooks as deterministic
control and recommends command hooks for production when the check can be expressed mechanically
([Claude Code hooks](https://code.claude.com/docs/en/hooks-guide)). Codex hooks likewise run scripts
during lifecycle events and require trust review for non-managed command hooks
([Codex hooks](https://learn.chatgpt.com/docs/hooks)).

This supports a graduation path from guidance to skills, hooks, tests, lint, or CI. It does not mean
every preference should become a gate. Judgment remains in steering; stable, observable invariants
are candidates for deterministic enforcement.

### Persistent memory expands the security boundary

OpenAI describes prompt injection as a social-engineering problem and recommends constraining the
impact of manipulation rather than relying only on input filtering
([OpenAI prompt-injection guidance](https://openai.com/index/designing-agents-to-resist-prompt-injection/)).
[AgentPoison](https://arxiv.org/abs/2407.12784) demonstrates that poisoning an agent's long-term
memory or retrieval knowledge base can create a persistent backdoor. Codex's own memory controls
allow exclusion of chats that used web search, MCP, or other external context, which is a concrete
acknowledgment that provenance and trust boundaries matter
([Codex memories](https://learn.chatgpt.com/docs/customization/memories)).

For Second Brain, text appearing in a user prompt is not automatically a user-authored instruction:
it may be quoted documentation, pasted logs, an attachment description, or untrusted external
content. Only an attributable user correction should be eligible for promotion, and no inferred
artifact should acquire steering authority without human review.

## Analysis of the motivating article

### What to adopt

- Preserve a correction as an episode rather than losing it with the chat.
- Keep the observed error separate from the proposed correct behavior.
- Look across episodes for recurring patterns.
- Inject only relevant approved guidance.
- Preserve the provenance from a rule back to the episodes that motivated it.
- Treat prompt-based steering as different from deterministic enforcement.
- Promote mechanically checkable rules into tests, lint, hooks, or CI where appropriate.

### What not to copy as a default

- **Real-time `UserPromptSubmit` detection.** Kiro's documented CLI event contains the submitted
  prompt and session identifier, whereas Second Brain's delayed capture sees the paired prompt and
  visible answer together
  ([Kiro CLI hooks](https://kiro.dev/docs/cli/hooks/)). The paired Task context is better evidence
  for deciding what the user was correcting and avoids adding a separate live hook path.
- **Automatic rule promotion at three occurrences.** Recurrence supports a proposal; it does not
  establish the right wording, exceptions, or scope. No reviewed source establishes three as a
  generally optimal number.
- **Fourteen- and 30-day decay.** Time can rank attention, but an old security constraint or stable
  user preference does not become less true merely because it was not triggered recently.
- **A fixed 30-episode cap.** Retrieval should control context size. Storage and audit history should
  not be coupled to a prompt-budget constant.
- **Keywords as the primary retrieval mechanism.** Topic Segments, embeddings, structured scope,
  and optional lexical search provide better retrieval inputs. Keywords can be generated later if
  measurements show they add value.
- **Writing `knowledge/rules.md` without review.** The article's team-local workflow is not a safe
  default for a general memory system, especially when captured text may include untrusted content.

## Recommended domain model

These objects are related, but they are not peer classifications at one lifecycle stage.

| Object | Meaning | Authority | Mutability |
|---|---|---|---|
| **Decision** | What was chosen or changed for the work | Task or project knowledge | Append; a later decision may supersede it |
| **Insight** | A useful understanding not itself a choice | Task or project knowledge | Append; may be contradicted or refined later |
| **Correction Episode** | Evidence that the user rejected, replaced, or materially narrowed something the agent said, assumed, proposed, or did | Evidence, not yet a rule | Immutable and append-only |
| **Steering Candidate** | A proposed reusable instruction inferred from explicit direction or one or more episodes | Proposal only | Versioned lifecycle object |
| **Steering Rule** | Candidate wording and scope explicitly approved by the user | Authoritative guidance within its scope | Versioned; may be superseded or retired |
| **Automation Candidate** | An approved rule that might be enforced or executed mechanically | Proposal for engineering work | Versioned until implemented or rejected |

A single user turn may legitimately produce more than one artifact. For example, “Use six hours,
not 24, and stop treating a Codex Project as the semantic project” can yield a Decision and a
Correction Episode. This is not double counting: the Decision records what is now chosen, while the
episode records how the agent's prior behavior was misaligned.

### Minimal Correction Episode fields

Store only fields that support interpretation, review, retrieval, and provenance:

| Field | Purpose |
|---|---|
| `what_was_misaligned` | Concise description of the agent statement, assumption, proposal, or action the user rejected |
| `corrected_expectation` | What the user indicated should be true or done instead |
| `category` | One of `fact`, `terminology`, `process`, `scope`, or `preference` |
| `scope_hint` | One of `task`, `project`, `personal`, or `global`; a hint, never promotion authority |
| `explicitness` | `explicit_standing_instruction` or `inferred_correction` |
| `supporting_turn_ids` | Exact Provenance to the user correction and the relevant prior visible agent answer |
| `topic_segment_id` | The containing semantic segment |

The existing memory identity and timestamps supply the episode ID and observation time. Do not add
keywords, decay timestamps, model telemetry, occurrence counters, or a mutable “active” status to
Codex v1 unless a demonstrated query requires them.

## Detection policy

The combined Task Semantic Pass should emit a Correction Episode only when all of these are true:

1. A user-authored statement rejects, replaces, or materially narrows something attributable to a
   prior visible agent answer.
2. The correction contains, or makes it reasonable to infer, a concrete improved expectation.
3. The supporting Agent Turns are identifiable.
4. The text is not merely quoted or pasted third-party content.

It should not emit an episode for:

- a newly introduced requirement with no prior misalignment;
- an ordinary follow-up question;
- a decision revision caused by changed circumstances alone;
- disagreement where the user's intended replacement remains unclear;
- politeness, frustration, or negative sentiment without an actionable correction; or
- an agent's own unsupported self-critique.

Use a precision-first rule: false negatives leave the original conversation available for later
review, while false positives can create bad steering candidates. The model should return no episode
when uncertain. A separate classifier, judge panel, or numerical confidence model is unnecessary in
v1. If review data later shows a need, add a small evidence-quality enum rather than an uncalibrated
floating-point score.

## Recommended lifecycle and promotion policy

```text
Codex Task idle for six hours
  -> monotonic capture of complete Agent Turns
  -> one Task Semantic Pass
       -> Topic Segments
       -> Decisions
       -> Insights
       -> Correction Episodes
  -> Dream Cycle clusters related episodes and existing knowledge
  -> Steering Candidate
  -> user reviews wording, scope, exceptions, and destination
  -> approved Steering Rule
  -> relevance-based injection
  -> optional Automation Candidate
  -> reviewed skill, hook, test, lint rule, or CI gate
```

### Provisional promotion defaults

- If the user explicitly states a durable instruction such as “always,” “never,” or “make this the
  standard,” the Dream Cycle may propose a candidate from one episode.
- Otherwise, propose a candidate after materially similar corrections occur in at least **two
  distinct Agent Tasks**. Distinct tasks are more informative than repeated turns inside one local
  misunderstanding.
- The threshold creates a **review candidate**, never an active rule. Two is therefore a queueing
  heuristic, not a claim of statistical confidence.
- Never promote solely because similar wording appears repeatedly; the episodes must share the same
  corrected behavior and compatible scope.
- Never infer `personal` or `global` scope from a single project-local episode unless the user stated
  that scope explicitly.

The two-task threshold should remain configurable and be evaluated against real captured Codex
Tasks. It is intentionally simpler and safer than the article's automatic three-occurrence rule.

### Candidate and rule lifecycle

Use explicit states rather than age-based deletion:

- Candidate: `proposed`, `approved`, `rejected`, `superseded`, or `archived`.
- Rule: `active`, `superseded`, or `retired`.
- Automation candidate: `proposed`, `accepted`, `rejected`, or `implemented`.

Approval creates a new rule version; it does not mutate the evidence episodes. A later correction
can create another candidate that supersedes the prior rule after review. Rejection should be kept
so the Dream Cycle does not repeatedly propose the same bad generalization without new evidence.

## Scope, precedence, and retrieval

### Scope

- **Task:** one-off direction for the current Agent Task; recordable but normally not promotable.
- **Project:** conventions, terminology, and architecture for one Second Brain semantic project or
  codebase. This should be the default promotion scope for technical corrections.
- **Personal:** the user's working preference across projects, such as communication or review style.
- **Global:** a universal instruction intended for every applicable agent and project. Require
  explicit user intent.

Project provenance and semantic project remain different. A Codex Project or working directory may
help retrieve a rule but must not silently determine its semantic scope.

### Precedence

At injection time:

1. Respect platform system, developer, safety, and organization policy first.
2. Apply approved rules only.
3. Prefer the more specific applicable scope on a genuine conflict: project over personal/global.
4. Within the same scope, prefer the latest active approved version.
5. If two active rules still conflict, surface the conflict for review instead of asking the model
   to choose silently.

This follows the specificity pattern documented by Codex, Claude Code, and Kiro without pretending
that Second Brain controls their higher-priority instruction layers.

### Retrieval and context injection

Retrieve approved rules using hard filters before semantic ranking:

1. applicable user and project scope;
2. active lifecycle status;
3. tool, language, path, or topic constraints when present;
4. semantic and lexical relevance to the current task; and
5. bounded context budget.

Use importance and recency only to rank otherwise applicable material. Do not inject raw Correction
Episodes by default; inject the concise approved rule and retain the episode chain for inspection.
Always-on rules should be rare and reserved for genuinely universal safety or workflow requirements.

## Deduplication, contradiction, supersession, decay, and archival

- **Source deduplication:** rely on existing Codex turn identity and the monotonic capture cursor so
  retries do not duplicate Agent Turns or semantic artifacts.
- **Episode deduplication:** do not merge or delete separate real episodes. Cluster them for pattern
  detection and preserve every provenance edge.
- **Decision revision:** represent the new Decision separately and let later consolidation attach a
  `supersedes` relationship. A correction episode may coexist with it when the agent was also being
  corrected.
- **Rule contradiction:** create a candidate that explicitly names the conflicting active rule.
  Human approval determines whether the new version supersedes the old one.
- **Decay:** use recency for review priority or retrieval ranking, never as automatic loss of truth
  or authority.
- **Archival:** archive rejected, superseded, retired, or long-unreviewed candidates to reduce review
  noise. Do not archive immutable evidence merely to meet a prompt-size cap.
- **Context budget:** solve at retrieval time with scoped top-k selection and concise rules, not by
  deleting history after 30 episodes.

## Privacy, security, and prompt-injection controls

1. Treat visible user prompts and agent answers as source evidence, not trusted steering.
2. Mark whether the correction is attributable to the user or appears inside quoted, pasted,
   attached, web, MCP, or tool-derived content.
3. Require human approval before a candidate can affect any future agent.
4. Show candidate wording, intended scope, supporting Task links, and relevant Agent Turns during
   review.
5. Never let a candidate execute commands, edit a repository, install a skill, or modify a steering
   file as part of review.
6. Keep approval and rejection events auditable and versioned.
7. Avoid copying secrets or sensitive raw passages into derived episode text; provenance can point
   back to protected source evidence.
8. Apply least privilege to any later service that writes `AGENTS.md`, skills, hooks, or CI config.
9. Treat project-committed instruction and hook changes like code: diff, review, test, and allow
   rollback.

Codex's option to exclude memory generation after external-context use is stricter than Second
Brain's current visible-turn capture model. Codex v1 should not replicate that entire policy now,
but origin attribution and human-gated promotion are minimum controls before steering integration.

## Graduation from steering to automation

An approved Steering Rule becomes an Automation Candidate only when:

- compliance can be observed from concrete inputs or outputs;
- pass/fail behavior can be specified with low ambiguity;
- false positives are acceptably low;
- the check has a clear scope and safe failure mode; and
- the proposed implementation can be reviewed and tested.

Choose the target by function:

| Need | Best target |
|---|---|
| Context, terminology, architecture, or judgment | `AGENTS.md`, scoped steering, or approved memory |
| Repeatable multi-step method with templates or scripts | Skill |
| Lifecycle-time check or context gathering | Hook |
| Code-level invariant with local feedback | Unit/integration test or lint rule |
| Repository-wide merge or release gate | CI |

Do not require a rule to survive 30 correction-free days before proposing automation. Stability is
demonstrated by clear semantics, testability, and review—not silence. Keep a concise human-readable
rationale after automation so future maintainers understand why the gate exists.

## Simplest viable Codex-first implementation

### Add now

1. Extend the existing combined semantic response with `correction_episodes`.
2. Validate the minimal fields and require supporting Agent Turn IDs, including the user's
   correction turn and the relevant preceding visible agent answer.
3. Persist each episode as a derived semantic memory with `derived_from` Exact Provenance to its
   Topic Segment, following the existing Decision and Insight path.
4. Allow the same segment to emit Decisions, Insights, and Correction Episodes.
5. Add a small real-Codex-data test set covering clear correction, decision-only revision, new
   requirement, quoted third-party text, and ambiguous disagreement.
6. Make Correction Episodes available to the Dream Cycle, but do not yet create or inject rules.

### Defer

- live correction hooks;
- automatic steering-file edits;
- cross-integration adapter redesign;
- learned numerical confidence models or multi-judge correction detection;
- a dedicated keyword index;
- fixed decay windows and storage caps;
- global/personal rule injection services;
- automatic clustering infrastructure beyond the existing Dream Cycle;
- rule authoring UI;
- contradiction resolution during capture;
- skills, hooks, lint, or CI generation; and
- automatic privacy classification beyond attributable-origin checks and existing source controls.

This sequencing produces immediate value—searchable, auditable correction evidence—without making
the capture path responsible for policy governance or execution.

## Risks and mitigations

| Risk | Consequence | Minimum mitigation |
|---|---|---|
| Ordinary requirement mistaken for correction | Noisy candidates | Require a prior attributable agent statement or action |
| Decision revision mistaken for agent failure | Wrong behavioral lesson | Allow Decision without Correction Episode; permit both only when supported |
| Over-generalization from one context | Bad cross-task rule | One episode normally remains evidence; use distinct-task recurrence and review |
| Similar wording masks different intent | Incorrect clustering | Compare corrected behavior and scope, not lexical similarity alone |
| Quoted content becomes instruction | Persistent prompt injection | Track origin; require user attribution and human approval |
| Rule scope is too broad | Unwanted behavior elsewhere | Default technical candidates to project; require explicit global scope |
| Rule conflicts with another rule | Unpredictable agent behavior | Version, apply specificity, and surface unresolved conflicts |
| Rule bloat reduces adherence | Relevant guidance is lost | Scoped retrieval and small injection budget |
| Stale rule survives changed reality | Repeated misguidance | Corrections against rules create supersession candidates; periodic review |
| Deterministic gate encodes a judgment call | Blocking false positives | Require objective predicate, test cases, safe failure, and review |

## Open decisions for design review

1. Should `Correction Episode` be stored as a new semantic memory kind beside `decision` and
   `insight`, or use a more general typed artifact envelope? The Codex-first recommendation is the
   new kind; defer a generalized envelope until a second integration needs it.
2. Is `scope_hint` limited to task/project/personal/global, or is organization/team scope needed
   later? Recommendation: keep the four-value set for Codex v1.
3. Should an explicit durable instruction generate a Steering Candidate immediately in the same
   capture, or only during the next Dream Cycle? Recommendation: always use the Dream Cycle so
   capture remains evidence-oriented and has one semantic transaction.
4. What review surface will approve candidates? Recommendation: defer UI design, but specify that
   review must show wording, scope, conflicts, and Exact Provenance.
5. How will a future injection service map an approved rule into Codex `AGENTS.md`, Kiro steering,
   Claude Code rules, Cursor rules, or a shared service? Recommendation: model the rule independently
   of any target file and create target-specific exporters later.

## Source matrix

| Source | Established pattern | Design use | Important limit |
|---|---|---|---|
| [Landreau article](https://www.linkedin.com/pulse/kiro-harness-engineering-three-days-from-fast-jean-francois-landreau-i4z6f/) | Correction episode -> pattern -> rule -> injection -> automation | Useful motivating lifecycle | Thresholds and files are custom workshop choices |
| [Kiro steering](https://kiro.dev/docs/steering/) | Global/workspace scope, workspace precedence, conditional inclusion | Scope, precedence, relevance | Does not define correction promotion |
| [Kiro hooks](https://kiro.dev/docs/hooks/) | Lifecycle triggers and deterministic shell actions | Possible future capture/enforcement | A hook is a mechanism, not a learning policy |
| [Kiro Web steering](https://kiro.dev/docs/web/steering/) | Creator feedback on PRs can influence future work | Evidence that products learn from explicit owner feedback | Internal learning schema and thresholds are undocumented |
| [Codex memories](https://learn.chatgpt.com/docs/customization/memories) | Idle-chat derivation, evidence, controls, redaction, external-context exclusion | Strong alignment with delayed capture and governed memory | Memory is not authoritative team policy |
| [Codex `AGENTS.md`](https://learn.chatgpt.com/docs/agent-configuration/agents-md) | Global-to-local instruction chain, specific overrides | Target scope and precedence | Prompt guidance remains probabilistic |
| [Codex hooks](https://learn.chatgpt.com/docs/hooks) | Trusted lifecycle scripts; can summarize chats or validate work | Future automation route | Hooks require trust and can execute code |
| [Claude Code memory](https://code.claude.com/docs/en/memory) | Managed/user/project/local scope, path rules, editable auto memory | Rule ownership, reviewability, context discipline | Conflicting prompt rules may still be ambiguous |
| [Claude Code hooks](https://code.claude.com/docs/en/hooks-guide) | Deterministic command hooks versus model judgment | Graduation to enforcement | Agent/prompt hooks remain probabilistic |
| [Cursor rules](https://docs.cursor.com/context/rules) | Global user rules, repo rules, inspectable repo-scoped memories | Scope and user control | Automatic memory internals are not specified |
| [Reflexion](https://arxiv.org/abs/2303.11366) | Verbal reflection in episodic memory from feedback | Correction Episode concept | Primarily task-attempt feedback, not human governance |
| [ExpeL](https://arxiv.org/abs/2308.10144) | Experiences plus extracted, retrievable insights | Dream Cycle pattern extraction | Does not supply a universal promotion threshold |
| [Self-Refine](https://arxiv.org/abs/2303.17651) | Iterative feedback improves current output | Same-task correction application | Does not define durable cross-task memory |
| [Generative Agents](https://arxiv.org/abs/2304.03442) | Experience stream, higher-order reflection, relevance/importance/recency retrieval | Evidence -> synthesis -> bounded retrieval | Simulation-specific scoring is not a steering policy |
| [MemGPT](https://arxiv.org/abs/2310.08560) and [Letta](https://docs.letta.com/guides/core-concepts/memory/context-hierarchy) | Tiered memory and selective context movement | Keep evidence external; inject concise rules | Does not decide what user corrections mean |
| [LongMemEval](https://arxiv.org/abs/2410.10813) | Updates, temporal reasoning, and abstention are separate capabilities | Test revision and uncertainty cases | Benchmark categories are not a domain schema |
| [Mem0 add](https://docs.mem0.ai/core-concepts/memory-operations/add) | Extraction, duplicate detection, and contradiction handling | Comparison point for consolidation | Latest-truth replacement conflicts with immutable evidence if applied at source |
| [Graphiti](https://github.com/getzep/graphiti) | Episodes, provenance, temporal validity, preserved history | Supersession without deleting evidence | Full temporal graph machinery is unnecessary for Codex v1 |
| [OpenAI prompt-injection guidance](https://openai.com/index/designing-agents-to-resist-prompt-injection/) | Constrain impact even when filtering fails | Human-gated authority and least privilege | Not specific to memory extraction |
| [AgentPoison](https://arxiv.org/abs/2407.12784) | Persistent memory or RAG poisoning can backdoor agents | Treat memory writes as a security boundary | Attack setting does not prescribe the full defense design |

## Recommended defaults to carry into specification

| Policy | Default |
|---|---|
| Stored correction object | `Correction Episode` |
| Extraction timing | Existing six-hour Task capture |
| Model calls | Same combined Task Semantic Pass; no extra call |
| Evidence policy | Immutable, monotonic, Exact Provenance |
| Detection bias | Precision over recall; abstain when ambiguous |
| Coexisting outputs | A segment may emit Decision, Insight, and Correction Episode |
| Explicit standing instruction | Candidate may be proposed after one episode, during Dream Cycle |
| Inferred recurrence | Candidate after materially similar evidence in two distinct Tasks |
| Promotion | Proposal only; user approval required |
| Default technical scope | Project |
| Personal/global scope | Require explicit user intent or reviewed cross-project evidence |
| Rule storage | Versioned with evidence links and lifecycle status |
| Decay | Ranking/review signal only; no automatic truth expiry |
| Context control | Scope filters plus bounded relevance retrieval |
| Steering-file changes | Never automatic |
| Automation | Only for stable, observable, testable predicates after review |
