# Design Document: Capture API

## Overview

This design implements the Capture API as a single FastAPI application in `src/capture_api.py` with Pydantic models for request validation, a shared storage pipeline function, bearer-token middleware, and channel-specific endpoints that normalize inputs before calling the shared pipeline. The server reuses the existing connection pool, embedding, classification, and depth-scoring modules.

## Architecture

```
External Channels                    Capture API (src/capture_api.py)
┌──────────────┐                    ┌──────────────────────────────────┐
│ Slack bot    │──POST /capture/───│  Channel endpoint                │
│ iOS Shortcut  │  slack            │  (normalize to common fields)    │
│ Browser ext.  │──POST /capture/───│         │                        │
│ Email fwd     │  browser          │         ▼                        │
│ Generic       │──POST /capture────│  _store_memory()                 │
└──────────────┘                    │  ├─ generate_embedding()         │
                                    │  ├─ classify_memory()            │
                                    │  ├─ compute_depth_score()        │
                                    │  ├─ normalize_project_tag()      │
                                    │  └─ create_memory()              │
                                    └──────────────────────────────────┘
                                              │
                                              ▼
                                    PostgreSQL + pgvector (existing)
```

## Design Decisions

### DD1: Single file (`src/capture_api.py`) — no sub-package

The MCP server is a single file (`src/mcp_server.py`). The Capture API has comparable complexity — a few endpoints, Pydantic models, and a shared storage function. A single file keeps the pattern consistent and avoids premature structure.

### DD2: FastAPI over Flask

FastAPI provides Pydantic validation, automatic OpenAPI docs, async support, and type hints out of the box. The project already uses type hints throughout. FastAPI's `Depends()` system cleanly handles auth middleware. Adding `fastapi` and `uvicorn` to `requirements.txt` is the only new dependency.

### DD3: Shared `_store_memory()` internal function

All endpoints normalize their channel-specific input into the same fields, then call `_store_memory(title, content, source_type, source_url, tags, metadata, project, type)`. This function runs the identical pipeline as `mcp_server.py`'s `memory_create` tool:

```python
def _store_memory(title, content, type="research", source_url=None,
                  source_type=None, tags=None, metadata=None, project=None):
    embedding = generate_embedding(content)
    mem_class = classify_memory(type, content)
    depth_score = compute_depth_score(content)
    project = normalize_project_tag(project)
    meta = metadata or {}
    meta["depth_score"] = depth_score
    return create_memory(
        type=type, title=title, content=content, embedding=embedding,
        tags=tags, source_type=source_type, source_url=source_url,
        metadata=meta, mem_class=mem_class, project=project,
    )
```

This avoids duplicating the pipeline logic and ensures Capture API memories are identical in quality to MCP-created ones.

### DD4: Bearer token auth via FastAPI dependency

The token check runs inside the `if __name__ == "__main__"` guard, not at module level. This ensures `sys.exit(1)` fires when the server is launched directly but does NOT fire when tests import the module. The `verify_token` dependency reads the token from `os.environ` at call time and compares using `hmac.compare_digest`.

```python
import hmac, os, sys, logging
from fastapi import Depends, HTTPException, Header

logger = logging.getLogger(__name__)

def verify_token(authorization: str = Header(...)):
    token_expected = os.environ.get("CAPTURE_API_TOKEN", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, token_expected):
        raise HTTPException(401, "Invalid or missing token")

# In __main__ block:
if __name__ == "__main__":
    if not os.environ.get("CAPTURE_API_TOKEN"):
        logger.fatal("CAPTURE_API_TOKEN environment variable is not set")
        sys.exit(1)
    uvicorn.run(...)
```

This pattern means:
- **Production**: `python -m src.capture_api` → `__main__` fires → token checked → exits if missing
- **Tests**: `from src.capture_api import app` → module imports cleanly → tests set `CAPTURE_API_TOKEN` in the environment before making requests

### DD5: Pydantic models for each channel

Each endpoint gets a Pydantic `BaseModel` for request validation. FastAPI handles 422 responses automatically for missing/invalid fields.

```python
from pydantic import BaseModel, Field

VALID_TYPES = {"idea", "synthesis", "research", "insight", "question",
               "decision", "priority", "project", "connection", "source"}

class CaptureRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., min_length=1, max_length=100_000)
    type: str = "research"
    source_url: str | None = None
    source_type: str | None = None
    tags: list[str] | None = None
    metadata: dict | None = None
    project: str | None = None

class SlackCaptureRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=100_000)
    user_name: str | None = None
    channel_name: str | None = None
    thread_ts: str | None = None
    tags: list[str] | None = None
    project: str | None = None

class BrowserCaptureRequest(BaseModel):
    url: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1, max_length=100_000)
    title: str | None = Field(None, max_length=500)
    tags: list[str] | None = None
    project: str | None = None

class EmailCaptureRequest(BaseModel):
    subject: str = Field(..., min_length=1, max_length=500)
    body: str = Field(..., min_length=1, max_length=100_000)
    sender: str | None = None
    tags: list[str] | None = None
    project: str | None = None
```

### DD6: Slack as client-push, not server-receive

The API runs locally with no public URL. Slack cannot push to it via outgoing webhooks or Events API without a tunnel. Instead, the `/capture/slack` endpoint is a standard authenticated POST that a Slack Workflow, Slack bot, or script calls after extracting the message. This keeps the integration simple and avoids tunnel dependencies. The endpoint accepts a `SlackCaptureRequest` Pydantic model with `text` (required) and optional `user_name`, `channel_name`, `thread_ts`.

### DD7: Default port 8100

The MCP server uses stdio transport. The Capture API needs a TCP port. Port 8100 avoids conflicts with common local services (8000 Django, 8080 various, 5000 Flask). Configurable via `CAPTURE_API_PORT`.

### DD8: No chunking in the Capture API

The ingestion pipeline (`src/ingest.py`) chunks long documents into sections with parent/child relationships. Capture API inputs are short-form — a Slack message, a browser selection, an email body. These don't need chunking. If a user sends a very long document, they should use the ingestion pipeline directly. The Capture API stores one memory per request, matching the MCP `memory_create` behavior.

## File Changes

### New Files

1. **`src/capture_api.py`** — FastAPI application with all endpoints, Pydantic models, auth dependency, and `_store_memory()` function. Runnable via `python -m src.capture_api`.

2. **`scripts/capture_api.sh`** — Shell wrapper to start the server, matching the `mcp_serve.sh` convention (hardcoded absolute path, `python3`, no venv activation):
   ```bash
   #!/bin/bash
   cd /path/to/second-brain
   exec python3 -m src.capture_api
   ```

3. **`scheduling/com.second-brain.capture-api.plist`** — launchd plist for persistent service. Unlike the scheduled jobs (which use `StartCalendarInterval` and route through `job_wrapper.sh`), this is a `KeepAlive` service that runs continuously. This is a legitimate divergence — the Capture API is a server, not a batch job. The plist SHALL include `StandardOutPath` and `StandardErrorPath` pointing to `~/Library/Logs/second-brain/capture-api-stdout.log` and `capture-api-stderr.log` respectively, since the service is not routed through `job_wrapper.sh` and needs its own log capture for debugging startup failures and runtime errors.

### Modified Files

4. **`requirements.txt`** — Add `fastapi` and `uvicorn[standard]`.

### Unchanged Files

- `src/db.py` — No changes. `create_memory()` and `get_connection()` used as-is.
- `src/embeddings.py` — No changes. `generate_embedding()` called directly.
- `src/classify.py` — No changes. `classify_memory()` called directly.
- `src/depth.py` — No changes. `compute_depth_score()` called directly.
- `src/project.py` — No changes. `normalize_project_tag()` called directly.
- `src/ingest.py` — No changes. Capture API does not use the chunking pipeline.
- `src/mcp_server.py` — No changes.
- `src/search.py` — No changes.

## Endpoint Summary

| Method | Path | Auth | Request Body | Response | Req |
|--------|------|------|-------------|----------|-----|
| POST | `/capture` | Bearer | `CaptureRequest` | 201 `{"memory_id": "..."}` | R1 |
| POST | `/capture/slack` | Bearer | `SlackCaptureRequest` | 201 `{"memory_id": "..."}` | R2 |
| POST | `/capture/browser` | Bearer | `BrowserCaptureRequest` | 201 `{"memory_id": "..."}` | R3 |
| POST | `/capture/email` | Bearer | `EmailCaptureRequest` | 201 `{"memory_id": "..."}` | R4 |
| GET | `/health` | None | — | 200/503 `{"status": "..."}` | R6 |

## Traceability Matrix

| Requirement | Design Element |
|-------------|---------------|
| R1: Generic Capture | `POST /capture` + `CaptureRequest` model + `_store_memory()` |
| R2: Slack Capture | `POST /capture/slack` + `SlackCaptureRequest` model + metadata extraction |
| R3: Browser Extension | `POST /capture/browser` + `BrowserCaptureRequest` model + auto-title |
| R4: Email Forward | `POST /capture/email` + `EmailCaptureRequest` model + sender metadata |
| R5: Authentication | `verify_token` dependency + `CAPTURE_API_TOKEN` env var + `hmac.compare_digest` |
| R6: Health Check | `GET /health` + `is_reachable()` call |
| R7: Server Config | Env vars (`CAPTURE_API_HOST`, `CAPTURE_API_PORT`) + `__main__` block + plist + shell script |
| R8: Error Handling | Try/except in endpoint handlers wrapping `_store_memory()` calls + `logging` module + 500 responses |
