# Second Brain

Second Brain captures durable knowledge from external sources and turns it into memories that agents can retrieve and synthesize across tasks.

## Language

**Agent Task**:
One source-defined unit of work or conversation from an agentic assistant. Each integration maps its own task, thread, or session concept onto this shared term.
_Avoid_: Raw transcript, capture

**Task Ownership**:
A Source Connector's evidence-based classification of an Agent Task as `user-owned`, `delegated`, or `unknown`. It describes who initiated the task, not who owns its files or semantic subject.
_Avoid_: Repository ownership, project ownership, author

**User-Owned Task**:
An Agent Task that native source evidence identifies as a conversation initiated for the user. Only User-Owned Tasks are eligible for independent capture.
_Avoid_: Main task, root task

**Delegated Task**:
An Agent Task created by an agent as child work inside another Agent Task. It is not captured independently because its useful outcome may already flow into the User-Owned Task's visible outcome.
_Avoid_: User-Owned Task, background capture

**Unknown-Ownership Task**:
An Agent Task whose Source Connector lacks reliable evidence to classify as user-owned or delegated. It is skipped and reported rather than assumed to be user-owned.
_Avoid_: User-Owned Task, safe default

**Agent Turn**:
One complete exchange within an Agent Task: a user's prompt and the agent's visible outcome. Hidden reasoning, system instructions, tool activity, and progress commentary are not part of the turn.
_Avoid_: Message, event, tool trace

**Attachment Descriptor**:
A non-content record that an image or file accompanied an Agent Turn, preserving available source metadata for audit without retaining or interpreting the attachment itself.
_Avoid_: Attachment copy, attachment content

**Amazon Q Developer**:
Amazon's developer-focused assistant and agentic coding environment. It is a distinct integration source, not another name for Kiro or Amazon Quick.
_Avoid_: Kiro, Amazon Quick

**Amazon Quick**:
Amazon's web-based work assistant and agent platform for research, insights, applications, and automation. Its source units are Amazon Quick Sessions, which map to Agent Tasks under an Amazon Quick identity namespace.
_Avoid_: Quick Desktop, Kiro, Amazon Q Developer

**Quick Desktop**:
A desktop agent application with its own local session storage. Its Quick Desktop Sessions map to Agent Tasks under a separate Quick Desktop identity namespace.
_Avoid_: Amazon Quick

**Kiro**:
An agentic coding service with IDE, CLI, and web surfaces. It is a distinct integration source, not another name for Amazon Quick or Amazon Q Developer.
_Avoid_: Amazon Quick, Amazon Q Developer

**Codex Project**:
A Codex sidebar grouping associated with a saved filesystem folder and containing one or more Codex Tasks.
_Avoid_: Workspace, repository

**Codex Task**:
One named conversation in Codex, identified internally by a stable thread ID. A task may become inactive and later resume without becoming a different task.
_Avoid_: Session, chat

**Workspace**:
The filesystem folder made available to a Codex Task. It records where the task ran, not what subject the conversation concerns.
_Avoid_: Task, thread, session

**Semantic Project**:
The real-world subject to which a memory belongs, represented by `memories.project`. It is assigned from explicit task meaning, not copied from a Codex Project, workspace, or repository location.
_Avoid_: Codex Project, workspace, repository

**Captured Task**:
The Second Brain representation of an Agent Task's ordered Agent Turns. It retains stable source identity so later activity refreshes the same captured task rather than creating another one.
_Avoid_: Captured session

**Exact Provenance**:
The traceability chain from a derived memory through its supporting Agent Turns to the original Agent Task. It identifies source evidence, not model diagnostics or processing telemetry.
_Avoid_: Processing metadata, model telemetry

**Correction Episode**:
A neutral record that the user rejected, replaced, or materially narrowed something in a visible agent outcome. It preserves the narrowest faithful version of the user's correction and Exact Provenance to both turns, but it is evidence rather than an active rule.
_Avoid_: Correction signal, steering rule, feedback score

**Steering Candidate**:
A proposed reusable instruction synthesized from Correction Episodes and related memories by the Dream Cycle. Panel acceptance makes it a retained recommendation, not behaviorally active guidance.
_Avoid_: Correction Episode, active rule

**Steering Rule**:
A Steering Candidate whose wording, Authority Scope, and Applicability the user has approved for future agent guidance.
_Avoid_: Memory, candidate, correction

**Supersession Candidate**:
A Steering Candidate that proposes replacing an active Steering Rule because later evidence contradicts or materially refines it.
_Avoid_: Duplicate, silent update

**Authority Scope**:
The owner of a Steering Rule: one project, the user personally, or the Second Brain system. It is independent of the contexts in which the rule applies.
_Avoid_: Applicability, source provenance

**Applicability**:
The integrations, semantic projects, repositories, topics, tools, languages, or paths in which a Steering Rule is relevant.
_Avoid_: Authority Scope, workspace provenance

**Task Capture**:
The act of creating or refreshing a Captured Task after its source Agent Task becomes eligible under that integration's capture policy.
_Avoid_: Session capture

**Topic Segment**:
A contiguous range of complete Agent Turns within a Captured Task that develops one coherent subject enough to stand alone for search or distillation. Brief asides remain with their surrounding segment.
_Avoid_: Arbitrary chunk, excerpt, topic mention

## Capture Pipeline

**Source Connector**:
A component that reads items from one external source and normalizes them for the shared capture pipeline.
_Avoid_: Migration, importer

**Ingestion**:
The stage that stores normalized source material as a source parent and searchable children.
_Avoid_: Migration, distillation

**Distillation**:
The stage that derives durable decisions, insights, and Correction Episodes from captured source material.
_Avoid_: Ingestion, summarization

**Task Distillation**:
The distillation of one Captured Task into durable decisions, insights, and Correction Episodes. Its results may become memories without multi-judge consensus.
_Avoid_: Dream Cycle, judging

**Task Semantic Pass**:
One interpretation of a newly captured task tail that returns Topic Segments and any genuine decisions, insights, or Correction Episodes supported by each segment. Segments may yield no derived memory, and the pass does not require separate summaries.
_Avoid_: Separate segmentation call, separate distillation call, summary pass

**Semantic Processing Cursor**:
The last Agent Turn included in a successfully stored Task Semantic Pass. Agent Turns after the cursor form the unprocessed tail retried by a later capture run.
_Avoid_: Segmentation status, distillation status, stage cursor

**Dream Cycle**:
The later consolidation stage that develops higher-order syntheses across existing memories and uses multi-judge consensus to decide which candidates become memories.
_Avoid_: Task Distillation, capture

**Express**:
The delivery boundary that surfaces memories through current or future channels and services.
_Avoid_: Distillation, storage, sync

**Task Backfill**:
The initial capture of eligible Agent Tasks that existed before their Source Connector was enabled.
_Avoid_: Schema migration

**Task Refresh**:
The reconciliation of an existing Captured Task after its source Agent Task receives additional activity and later becomes eligible again.
_Avoid_: Duplicate capture, migration

**Monotonic Task Refresh**:
A Task Refresh that preserves previously captured Agent Turns in their original order and appends only unseen complete turns. Changes, disappearance, or reordering of known source turns are Source Drift and do not revise captured evidence.
_Avoid_: Full reconciliation, historical rewrite

**Source Drift**:
A later source observation in which a previously captured Agent Turn has changed, disappeared, or moved. Source Drift is ignored without additional state when accumulating Captured Task evidence.
_Avoid_: Historical mutation, database corruption
