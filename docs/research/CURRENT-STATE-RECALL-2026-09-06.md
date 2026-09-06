# Current-state recall repair — September 6, 2026

A real job-search briefing exposed an omission: recall returned an old unsent
outreach draft and commute concern although the user's correction and sent
update were already captured and distilled. No failure was injected into the
live workflow to produce this observation.

## Mechanism

Previously the broker truncated legacy hybrid-search candidates before utility
ranking, ordered every inferred memory ahead of correction evidence, and packed
individual items independently. Now it uses the stable candidate path and
follows exact Topic Segment derivation links to deliver later supported updates
before historical claims. See [ADR 0013](../adr/0013-deliver-later-topic-evidence-with-task-memories.md).
No role names, user-specific words, source IDs, or evaluation answers occur in
the implementation. No stored summaries, semantic prompts, embedding model,
weights, capture idle threshold, or governing rules were changed.

A second observed problem was recapture of internal Codex processing sessions.
Native records have `source=exec` and `thread_source=user`, including model calls
made by Second Brain. The user flag alone therefore cannot prove human ownership.
Such tasks are now reported as unknown ownership and skipped. Ordinary verified
Desktop user tasks still qualify. Deliberately human-launched non-interactive
CLI tasks also remain excluded until stronger ownership evidence is available.

Second Brain's Codex backend additionally uses `--ephemeral`, supported by the
installed CLI and [official non-interactive documentation](https://developers.openai.com/codex/noninteractive/),
to avoid persisting future processing sessions. Model output and operational
metrics are still returned through the existing execution path.

## Validation and limits

Regression checks first failed on older-only delivery, old claims surviving a
budget omission, persistent backend calls, and exec sessions being classified
as user-owned. The corrected broker delivers the two missing source updates in
the frozen real-data replay. Database tests also check active/project filters
and that a different topic in the same task is not pulled in.

Private evaluation files, corpus audit, and reversible repair backups live under
`evaluations/results/current-state-diagnosis/` in the working evaluation checkout.
They are ignored by Git. The original failed briefing and its `not_used` receipt
remain unchanged. Passing this repair check establishes improved delivery for
this pattern, not broad usefulness or automatic startup integration.
