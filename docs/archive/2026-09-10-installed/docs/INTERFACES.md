# Interfaces and configuration — original implementation

Source-reviewed 2026-09-10. Commands below describe interfaces; model calls, DB writes, mail, and maintenance are explicitly identified. [Operations](OPERATIONS.md) supplies setup and isolation prerequisites.

## Nine MCP tools

The server is `src/mcp_server.py`, stdio transport. The exact callable definitions are the source of truth; these are the current public arguments.

| Tool | Arguments (required first; defaults shown) | Result and side effects |
|---|---|---|
| `memory_create` | `type,title,content`; `tags,source_type,source_url,metadata,project,encoding_context=None` | Embeds content, computes class/depth, writes memory. Returns ID string and optional depth/question warnings |
| `memory_search` | `query`; `type=None,limit=10,project=None,source_type=None,since_days=None,status=None` | Embeds query, retrieves/reranks, increments access counters. Returns results+temporal/schema context; previews capped at 500 characters |
| `memory_read` | `memory_id` | Full row excluding embedding; no access-counter write; error object if absent |
| `memory_update` | `memory_id`; `title,content,status,tags,summary,type=None` | Updates supplied fields; content change re-embeds/reclassifies/rescores. Returns confirmation string |
| `memory_relate` | `source_id,target_id,relation_type`; `note=None` | Upserts directed relationship with note |
| `memory_list` | `type=None,source_type=None,status=None,limit=20` | Recent record metadata, no relevance scoring or embedding call |
| `memory_graph` | `memory_id` | Memory summary and relationship projections. Underlying query includes both edge directions, but output always reports stored `target_id`; incoming-edge neighbor handling is defective |
| `memory_learn` | `content,topics`; `source=None` | Embeds/searches comma-separated topics and returns related snippets+instructions. **Does not itself write insights or run the dream cycle**; caller must synthesize/create/relate |
| `memory_brief` | `window_days=14,use_llm=False` | Markdown briefing; deterministic by default, optional Kiro editor; no delivery receipt written |

`memory_graph` traverses memory relationships, not the imported entity graph. The graph projection bug means an incoming edge can return the requested record itself as `target_id`; consult the stored source/target before interpreting direction.

Memory type strings used by interfaces: research, synthesis, idea, connection, priority, question, insight, decision, project, source. The DB/schema helpers also support `schema`, which is an abstract memory grouping via `derived_from`; the core does not prove that such records are populated. Status values used include active, explored, archived, superseded, user_rejected. SQL does not constrain these to enums.

Relationship meanings are directed: A `supports` B means A provides evidence for B; A `derived_from` B means A synthesizes B; A `superseded_by` B means B replaces A. Other conventional labels: contradicts, extends, inspires, blocks, requires, related_to. Do not reverse support direction as an older tutorial did.

## Synthetic first-record check

Use an isolated test database and a fictional source. Create a decision with explicit status and evidence rather than invented benchmark claims:

> WHAT: For a fictional workshop exercise, test both supported execution modes before sharing the lab. WHY: The starter code may depend on a hosted proxy that participants will not have afterward. STATUS: Proposed test, not an observed outcome. Questions this answers: How should the fictional workshop's portability be checked?

Call create, retain returned ID, search for a paraphrase, read the full record, update tags, create a second fictional evidence record, and relate evidence→decision with `supports`. Record actual responses, expected source content, DB writes and provider call outcomes. A successful round trip proves that transport/store/search path for this fixture; it does not prove good discoveries.

## Main CLI contracts

All commands run with the repository virtual environment and root working directory.

| Script/module | Options | External calls / mutations |
|---|---|---|
| `scripts/brief.py` | `--window-days N`, `--no-llm`, `--json`, `--useful TARGET`, `--less TARGET`, `--mute TARGET`, `--unmute TARGET`, `--prefs` | DB reads; default Kiro editor. Feedback flags write DB. `--json` emits composed items before editor |
| `scripts/express_push.py` | `--dry-run`, `--force`, `--no-llm` | Gate+composition, optional Kiro; SMTP send and local state write unless dry-run. `--force` bypasses previous-run suppression, not the quality trigger |
| `scripts/dream_cycle_run.py` | required `--run-type scheduled|post_learn|session_start|user_triggered`; `--topic TEXT`, `--memory-ids ID,ID` | Model/MCP/embedding calls, memory/run/candidate writes, digest file. Exit2 on aborted/fatal, otherwise0 |
| `scripts/distill_sessions.py` | `--dry-run`, `--limit N`, repeatable `--source-type T`, `--skip-contradictions` | Kiro even in dry-run; Bedrock+memory/relationship writes otherwise |
| `scripts/weekly_digest.py` | `--days N` (7), `--dry-run` | Kiro even in dry-run; embedding+memory+notification otherwise |
| `scripts/chat_extract.py` | `--backfill`, `--dry-run` | Source/DB reads, logging/staging; backfill ignores skip state |
| `scripts/batch_ingest_staged.py` | `--dry-run`, `--limit N` | DB reads; normal execution embeds/writes/moves source files |
| `scripts/batch_ingest_parallel.py` | `--workers N` (10), `--limit N` | Concurrent embedding/store writes; no dry-run |
| `scripts/ingest_qd_chats.py` | `--dry-run`, `--limit N`, `--min-messages N` (2) | Read QD/DB; normal path embedding/store |
| `src.capture.youtube` | `--dry-run`, `--limit N`, `--min-duration N` (300), `--playlists IDS...` | Remote playlist enumeration/cookies even in dry-run; transcript acquisition/store otherwise |
| `scripts/eval/run_evaluation.py` | `--tier golden|curated|cold_warm|ablation|consolidation|trends`, `--limit N`, `--dry-run` | Normal tiers read DB, call embeddings, write evaluation artifacts; dry-run only prints tier list |
| `scripts/eval/recall_check.py` | `--threshold FLOAT` (0.85), `--verbose` | Query embeddings and exact-versus-index search; threshold controls exit, not usefulness |

Other entrypoints and their purpose are listed in [Capture](CAPTURE-COMPONENTS.md). Manual maintenance scripts are not normal capture stages: `backfill_projects.py`, `redistill_capped.py`, `jobs/reindex_embedding.sh`, `jobs/dedup_ide_backfill.sh`. Review their arguments and target paths before use. The existing parallel ingestion path uses a shared `SimpleConnectionPool`; increasing workers is not a demonstrated concurrency-safety fix.

Feedback target forms: hexadecimal item prefix (optional leading `#`), kind (`insight`, `contradiction`, `resurface`, `digest`, `question`), or topic/project string. `--useful`, `--less`, `--mute` set latest preference; `--unmute` removes the target's preference. These are **delivery preferences**, not per-version acceptance/correction/outcome ratings.

## HTTP capture

`python -m src.capture_api` serves `/health` (DB reachability only) and authenticated POST endpoints `/capture`, `/capture/slack`, `/capture/browser`, `/capture/email`. Generic capture requires title/content, supports type/source/tags/metadata/project/context, bounds title to 500 / content to 100,000 characters, and embeds before storage. Specialized endpoints normalize caller-supplied payloads; they are not background connectors. Token missing at module CLI startup exits 1; bad bearer authorization returns 401.

## Environment/configuration

| Name | Default/behavior | Scope |
|---|---|---|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | localhost, 5432,memory_bank role/database; code contains a development password default | Python DB client, read at import; choose explicit credentials/target, not fallback for a shared deployment |
| `DB_POOL_MIN`, `DB_POOL_MAX` | 1,5; some ingestion scripts set max 20 | Python pool sizing; no global source transaction |
| `TEST_DB_NAME` | memory_bank_test | Tests create/delete data in this target. **No production-name safety guard** |
| `BEDROCK_REGION` | us-east-1 | Embedding client |
| `EMBEDDING_MODEL` | amazon.titan-embed-text-v2:0 | Model name override; request format/dimension remain Titan-style 1024, not arbitrary model compatibility |
| Standard AWS profile/credential variables | Boto3 chain and CLI-specific behavior | Credentials are separate from model-backend TOML |
| `SECOND_BRAIN_PROFILE` | laptop | Six dream-cycle roles only |
| `SECOND_BRAIN_BACKENDS_CONFIG` | repo config/backends.toml | Profile file |
| `KIRO_CLI`, `CLAUDE_CLI`, `CODEX_CLI` | Kiro user-local path; claude/codex executable names | Backend executables |
| `KIRO_LLM_METRICS_DIR` | repo logs/llm_metrics | Per-call metrics/debug context; do not assume these files are public-safe |
| `CAPTURE_API_TOKEN`, `CAPTURE_API_HOST`, `CAPTURE_API_PORT` | Token required (read from gitignored `.env` by `scripts/jobs/capture_api.sh`);127.0.0.1;8100 | Optional HTTP capture listener |
| `EXPRESS_EMAIL_TO`, `EXPRESS_EMAIL_FROM`, `GMAIL_APP_PASSWORD` | No default credentials | Gmail SMTP STARTTLS; absent configuration skips delivery with exit 0 |

Shell wrappers frequently hardcode DB host/port and original home paths; `DB_*` overrides do not make every wrapper portable. No automatic `.env` loading or secret-manager retrieval is present in the normal application path. Preserve secrets outside committed docs/configuration.

## Terminology

- **Memory:** stored text+metadata, optionally embedded; not every memory is a verified fact.
- **Distillation:** extraction of durable claims from raw content, currently a separate chat process.
- **Discovery:** proposal connecting existing evidence to a useful current action.
- **Consensus:** four role-based judgments with a three-vote acceptance threshold, not proof or Bayesian confidence.
- **Confidence:** source/model-assigned metadata; QD imports upstream numeric confidence; no unified calibrated probability.
- **Depth:** text heuristic used in warnings/ranking, not human quality score.
- **Retrieval reinforcement:** stored access counts/timestamps influence future ranking; no measured outcome implied.
- **Source provenance:** IDs/URLs/file metadata and source links. Original does not validate exact quotes or immutable revisions.
- **Delivered:** actually shown or sent; original has no precise per-item versioned delivery ledger.
- **Useful:** user judges relevant/new/actionable and preferably applies it with an observed benefit; not interchangeable with accepted.
