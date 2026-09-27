# Purpose and implementation history

The purpose is to capture work context across surfaces and discover connections that improve an actual decision, plan or deliverable. The user specifically wants prior projects' outcomes to inform current work, with sources and enough context to make the transfer useful. A reminder to follow up is insufficient unless the discovery explains what decision, why it matters and how the mechanism helps.

The original implementation stores memories in PostgreSQL/pgvector, captures source channels, distills selected conversations, retrieves evidence, runs a multi-role Dream Cycle and delivers briefings. The separate experimental GitHub implementation explored repairs and alternate structure in development/test environments. It was not established as a deployed replacement. Each implementation requires its own code/runtime identity and end-to-end evidence.

A historical 35-record comparison produced candidate ideas through model roles with frozen evidence. It did not run complete native capture, distillation, retrieval, persistence, delivery and observed user outcomes. Its useful suggestions were evidence to investigate, not proof that either full system met its purpose.

The September 26 plan repairs reliability before changing the reasoning architecture: controlled routing/accounting, source integrity, read-only exploration, deterministic candidate authorization, truthful operations, encrypted recovery and measurable retrieval. A bounded retrieval-versus-discovery trial then asks whether the repaired implementation adds grounded, novel and timely value. Human novelty and outcome remain distinct from a model's vote.

Current implementation authority starts at [START-HERE](START-HERE.md). Historical plans/specifications are preserved in [the dated archive](archive/2026-09-26/README.md). The [completion ledger](REFACTOR-COMPLETION-2026-09-26.md) distinguishes what has been implemented, verified and actually applied.
