# Second Brain — original implementation

Second Brain captures work context and seeks useful connections across projects and sources. Its goal is to improve a current decision or deliverable with a relevant prior result, an explicit transfer rationale, and a concrete next step.

**Start with [docs/START-HERE.md](docs/START-HERE.md).** It identifies this implementation, maps every component, explains provider and deployment boundaries, and gives the resume checklist. This checkout is the original lineage at `7bf8d41` plus local, unfinished direct-API backend work. The experimental GitHub lineage is a separate implementation; do not merge or deploy it by following the original runbook.

| Need | Authoritative document |
|---|---|
| Orientation and purpose | [Start here](docs/START-HERE.md) |
| Components and data model | [Architecture](docs/ARCHITECTURE.md) |
| Capture, ingestion, distillation | [Capture](docs/CAPTURE-COMPONENTS.md) |
| Agents, embeddings, providers, authentication | [Model backends](docs/MODEL-BACKENDS.md) |
| Setup, operation, scheduling, troubleshooting | [Operations](docs/OPERATIONS.md) |
| Recovery and actual backup limitations | [Disaster recovery](docs/DISASTER-RECOVERY.md) |
| MCP, CLI, configuration, terminology | [Interfaces](docs/INTERFACES.md) |
| Tests, evidence limits, improvements | [Validation](docs/VALIDATION.md) |

These guides were reconciled against code on 2026-09-10. Code/configuration presence does not establish that a service is running, credentials work, a source is current, or an output helps the user. No production jobs were resumed and no application code was changed by this documentation review. Prior documentation is preserved as [dated historical evidence](docs/archive/2026-09-10/README-ARCHIVE.md).
