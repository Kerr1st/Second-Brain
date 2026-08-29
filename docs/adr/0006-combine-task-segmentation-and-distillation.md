---
status: accepted
---

# Combine task segmentation and distillation

Codex v1 uses one Task Semantic Pass over the newly captured tail. The model returns Topic Segments
and zero or more supported decisions, insights, or Correction Episodes for each segment in one response; Topic
Segmentation and Task Distillation remain distinct concepts, but they are not separate model calls.
A segment retains a title and its original Agent Turns and does not require a separate summary. This
supersedes the separate-call and separate-stage design recorded in ADR 0002.

The combined semantic result stores atomically. Failure preserves the Captured Task, stores no
partial semantic output, and leaves one Semantic Processing Cursor at the last successfully
processed Agent Turn. The next capture invocation retries the whole unprocessed tail; once hourly
scheduling is approved, that is normally the next hourly run. Codex v1 has no stage-specific
semantic retry state.
