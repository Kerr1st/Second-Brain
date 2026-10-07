# Installed working-tree documentation, 2026-09-10 — historical snapshot

**Provenance.** Every file below is the byte-identical content of `git show preserve/2026-09-10:<path>` — branch `preserve/2026-09-10`, commit `50ac872e2317edc8af29b016b4083f11e4def34f`, which captured the installed checkout's uncommitted working tree as it stood before R1 was deployed on 2026-10-05 (77 modified tracked files and the untracked additions; the `.dream/` runtime artefact and the `src/backends/kiro.py` edit are not documentation and are not here). The copies were produced in R7 with `git show`; a reader can verify any file with `git show preserve/2026-09-10:<path> | diff - docs/archive/2026-09-10-installed/<path>`.

**What it is.** On 2026-09-10 the operator reconciled the documentation of the then-installed system: most documents and `.kiro/specs` families were reduced to one-paragraph redirect stubs pointing at a new `docs/START-HERE.md`, and three new orientation documents (`START-HERE`, `VALIDATION`, `SPEC-INDEX`) plus the `direct-api-backends` specification were written against commit `7bf8d41`. That work was never committed; it was preserved when the release line replaced the installed `main`. The same operator's generalized snapshot of the pre-stub originals is the sibling archive [`../2026-09-10/`](../2026-09-10/README-ARCHIVE.md) (identical in both trees and therefore not duplicated here).

**Authority.** None. These files describe the unrepaired code of September 2026 and the operator's observations of that day. The current entry point is [`docs/START-HERE.md`](../../START-HERE.md); the eight files whose paths also exist in the current tree with different content (`docs/START-HERE.md`, `docs/VALIDATION.md`, `docs/SPEC-INDEX.md`, the four `direct-api-backends` files and `README.md`) were resolved in R7 by keeping their evidence-boundary content and re-pinning every code fact; see [`docs/RELEASES.md`](../../RELEASES.md), section R7. Relative links inside these files point at their original locations. Files that were identical between the snapshot and the current tree (`docs/AUDIT-*.md`, `docs/audit-2026-09-23/`, `docs/audit-2026-09-24/`, `docs/archive/2026-09-10/`) are not repeated.

## Files (105)

Top level (2):
- `README.md`
- `scheduling/README.md`

New orientation documents and stubbed guides under `docs/` (32):
- `docs/AGENTIC-RETRIEVAL-PLAN.md`
- `docs/ARCHITECTURE.md`
- `docs/CAPTURE-COMPONENTS.md`
- `docs/COMPONENTIZATION-PLAN.md`
- `docs/DESIGN-DECISIONS.md`
- `docs/DISASTER-RECOVERY.md`
- `docs/DREAM-CYCLE-MCP-DECOUPLING.md`
- `docs/EXPRESS-PLAN.md`
- `docs/FABLE5-THINKER-PLAN.md`
- `docs/HYBRID-CHAT-EXTRACTION.md`
- `docs/INTERFACES.md`
- `docs/MAC-MINI-MIGRATION.md`
- `docs/MIGRATION-native-postgres.md`
- `docs/MODEL-BACKENDS-VERIFICATION.md`
- `docs/MODEL-BACKENDS.md`
- `docs/OPERATIONS.md`
- `docs/PARALLEL-WORK.md`
- `docs/PLAN-capture-repair.md`
- `docs/PLAN-mcp-review.md`
- `docs/PLAN-panel-acceptance.md`
- `docs/PLAN-refactor.md`
- `docs/PLAN-skim-stage.md`
- `docs/QUALITY-BASELINE-2026-06-06.md`
- `docs/QUICK-DESKTOP-INTEGRATION.md`
- `docs/RESEARCH-skim-model.md`
- `docs/SECOND-BRAIN-PURPOSE-AND-EVOLUTION.md`
- `docs/SESSION-2026-06-12-handoff.md`
- `docs/SESSION-2026-06-16-handoff.md`
- `docs/SKIM-STAGE.md`
- `docs/SPEC-INDEX.md`
- `docs/START-HERE.md`
- `docs/VALIDATION.md`

User guide stubs (18):
- `docs/user-guide/CONTRIBUTING.md`
- `docs/user-guide/STYLE.md`
- `docs/user-guide/architecture.md`
- `docs/user-guide/connect-ai-agent.md`
- `docs/user-guide/database-schema.md`
- `docs/user-guide/dream-cycle-design.md`
- `docs/user-guide/first-memory.md`
- `docs/user-guide/getting-started.md`
- `docs/user-guide/glossary.md`
- `docs/user-guide/how-memory-works.md`
- `docs/user-guide/index.md`
- `docs/user-guide/operations.md`
- `docs/user-guide/overview.md`
- `docs/user-guide/reference.md`
- `docs/user-guide/security-model.md`
- `docs/user-guide/troubleshoot.md`
- `docs/user-guide/upgrade.md`
- `docs/user-guide/using-second-brain.md`

`direct-api-backends` specification, the Sep-10 pre-implementation design with its task execution history (5):
- `.kiro/specs/direct-api-backends/.config.kiro`
- `.kiro/specs/direct-api-backends/design.md`
- `.kiro/specs/direct-api-backends/requirements.md`
- `.kiro/specs/direct-api-backends/tasks.md`
- `.kiro/specs/direct-api-backends/tasks.meta.json`

Specification redirect stubs (36) and their `.config.kiro` identities (12):
- `.kiro/specs/byzantine-consensus-panel/design.md`
- `.kiro/specs/byzantine-consensus-panel/requirements.md`
- `.kiro/specs/byzantine-consensus-panel/tasks.md`
- `.kiro/specs/capture-api/design.md`
- `.kiro/specs/capture-api/requirements.md`
- `.kiro/specs/capture-api/tasks.md`
- `.kiro/specs/claude-code-stream-json-probe-fix/bugfix.md`
- `.kiro/specs/claude-code-stream-json-probe-fix/design.md`
- `.kiro/specs/claude-code-stream-json-probe-fix/tasks.md`
- `.kiro/specs/data-layer-decomposition/design.md`
- `.kiro/specs/data-layer-decomposition/requirements.md`
- `.kiro/specs/data-layer-decomposition/tasks.md`
- `.kiro/specs/db-layer-hardening/design.md`
- `.kiro/specs/db-layer-hardening/requirements.md`
- `.kiro/specs/db-layer-hardening/tasks.md`
- `.kiro/specs/dream-cycle/design.md`
- `.kiro/specs/dream-cycle/requirements.md`
- `.kiro/specs/dream-cycle/tasks.md`
- `.kiro/specs/evaluation-framework/design.md`
- `.kiro/specs/evaluation-framework/requirements.md`
- `.kiro/specs/evaluation-framework/tasks.md`
- `.kiro/specs/model-backend-adapters/design.md`
- `.kiro/specs/model-backend-adapters/requirements.md`
- `.kiro/specs/model-backend-adapters/tasks.md`
- `.kiro/specs/project-auto-tagging/design.md`
- `.kiro/specs/project-auto-tagging/requirements.md`
- `.kiro/specs/project-auto-tagging/tasks.md`
- `.kiro/specs/question-aware-search/design.md`
- `.kiro/specs/question-aware-search/requirements.md`
- `.kiro/specs/question-aware-search/tasks.md`
- `.kiro/specs/rerank-weights-single-source-of-truth/design.md`
- `.kiro/specs/rerank-weights-single-source-of-truth/requirements.md`
- `.kiro/specs/rerank-weights-single-source-of-truth/tasks.md`
- `.kiro/specs/retrieval-quality/design.md`
- `.kiro/specs/retrieval-quality/requirements.md`
- `.kiro/specs/retrieval-quality/tasks.md`
- `.kiro/specs/byzantine-consensus-panel/.config.kiro`
- `.kiro/specs/capture-api/.config.kiro`
- `.kiro/specs/claude-code-stream-json-probe-fix/.config.kiro`
- `.kiro/specs/data-layer-decomposition/.config.kiro`
- `.kiro/specs/db-layer-hardening/.config.kiro`
- `.kiro/specs/dream-cycle/.config.kiro`
- `.kiro/specs/evaluation-framework/.config.kiro`
- `.kiro/specs/model-backend-adapters/.config.kiro`
- `.kiro/specs/project-auto-tagging/.config.kiro`
- `.kiro/specs/question-aware-search/.config.kiro`
- `.kiro/specs/rerank-weights-single-source-of-truth/.config.kiro`
- `.kiro/specs/retrieval-quality/.config.kiro`
