# Tasks: Capture API

> **STATUS (2026-06-04): BUILT & DEPLOYED.** Fully implemented and running as a
> live service — `src/capture_api.py`, `tests/test_capture_api.py`, LaunchAgent
> `com.second-brain.capture-api` (port 8100). The unchecked boxes below predate
> completion; treat all tasks as done. Kept as the design reference for the
> planned Slack/Outlook capture extension.

## Task 1: Add dependencies
- [ ] Add `fastapi>=0.115.0` and `uvicorn[standard]>=0.34.0` to `requirements.txt`
- [ ] Install: `pip install -r requirements.txt`

**Requirements:** R7 (Server Configuration)
**Files:** `requirements.txt`

## Task 2: Implement `src/capture_api.py` — core structure and auth
- [ ] Create `src/capture_api.py` with FastAPI app instance
- [ ] Add module-level startup check inside `if __name__ == "__main__"` guard: if `CAPTURE_API_TOKEN` is not set, log error and `sys.exit(1)`. The check must NOT be at module top-level — tests need to import the module without triggering the exit
- [ ] Implement `verify_token` dependency: read `CAPTURE_API_TOKEN` from `os.environ` at call time, compare request header with `hmac.compare_digest`, return 401 on mismatch
- [ ] Implement `_store_memory()` internal function: calls `generate_embedding()`, `classify_memory()`, `compute_depth_score()`, `normalize_project_tag()`, `create_memory()` — same pipeline as `mcp_server.py`'s `memory_create`
- [ ] Add `if __name__ == "__main__"` block running uvicorn with `CAPTURE_API_HOST` / `CAPTURE_API_PORT` env vars (defaults: `127.0.0.1`, `8100`)
- [ ] Add request logging and error handling with Python `logging` module

**Requirements:** R5 (Authentication), R7 (Server Configuration), R8 (Error Handling)
**Files:** `src/capture_api.py`

## Task 3: Implement `GET /health`
- [ ] Add `GET /health` endpoint (no auth required) that calls `is_reachable()` from `src/db.py`
- [ ] Return 200 `{"status": "healthy", "database": "connected"}` when DB is reachable
- [ ] Return 503 `{"status": "unhealthy", "database": "disconnected"}` when DB is not reachable

**Requirements:** R6 (Health Check)
**Files:** `src/capture_api.py`

## Task 4: Implement `POST /capture` — generic endpoint
- [ ] Define `CaptureRequest` Pydantic model with `title` (required), `content` (required), `type` (default `"research"`), `source_url`, `source_type`, `tags`, `metadata`, `project`
- [ ] Add type validation against allowed set (`idea`, `synthesis`, `research`, `insight`, `question`, `decision`, `priority`, `project`, `connection`, `source`)
- [ ] Implement `POST /capture` endpoint: validate, call `_store_memory()`, return 201 `{"memory_id": "..."}`
- [ ] Wrap in try/except, return 500 on pipeline failure

**Requirements:** R1 (Generic Capture), R8 (Error Handling)
**Files:** `src/capture_api.py`

## Task 5: Implement `POST /capture/slack`
- [ ] Define `SlackCaptureRequest` Pydantic model with `text` (required, max 100K), `user_name`, `channel_name`, `thread_ts`, `tags`, `project`
- [ ] Title: first 80 chars of `text`; metadata: `slack_user`, `slack_channel`, optionally `thread_ts`
- [ ] Store with `source_type="slack"`, return 201 `{"memory_id": "..."}`

**Requirements:** R2 (Slack Capture)
**Files:** `src/capture_api.py`

## Task 6: Implement `POST /capture/browser`
- [ ] Define `BrowserCaptureRequest` Pydantic model with `url` (required), `content` (required), `title` (optional), `tags`, `project`
- [ ] If `title` not provided, use first 80 chars of `content`
- [ ] Store with `source_type="browser_extension"`, `source_url` from `url` field
- [ ] Return 201 `{"memory_id": "..."}`

**Requirements:** R3 (Browser Extension)
**Files:** `src/capture_api.py`

## Task 7: Implement `POST /capture/email`
- [ ] Define `EmailCaptureRequest` Pydantic model with `subject` (required), `body` (required), `sender` (optional), `tags`, `project`
- [ ] Store with `source_type="email"`, title from `subject`, content from `body`
- [ ] Include `{"email_sender": sender}` in metadata when sender is provided
- [ ] Return 201 `{"memory_id": "..."}`

**Requirements:** R4 (Email Forward)
**Files:** `src/capture_api.py`

## Task 8: Create shell wrapper and launchd plist
- [ ] Create `scripts/capture_api.sh` — hardcoded `cd /path/to/second-brain`, `exec python3 -m src.capture_api` (matching `mcp_serve.sh` convention)
- [ ] Make executable: `chmod +x scripts/capture_api.sh`
- [ ] Create `scheduling/com.second-brain.capture-api.plist` — launchd plist with `KeepAlive` (persistent service, not `StartCalendarInterval`), include `StandardOutPath` and `StandardErrorPath` pointing to `~/Library/Logs/second-brain/capture-api-stdout.log` and `capture-api-stderr.log`, matching existing plist conventions in `scheduling/`

**Requirements:** R7 (Server Configuration)
**Files:** `scripts/capture_api.sh`, `scheduling/com.second-brain.capture-api.plist`

## Task 9: Tests
- [ ] Add `tests/test_capture_api.py` using FastAPI's `TestClient` with mocked `generate_embedding` (use existing embedding mock pattern from `conftest.py`). Set `CAPTURE_API_TOKEN` in the environment before importing the app (e.g., via a module-level `os.environ.setdefault` or a pytest fixture)
- [ ] Test auth: missing header → 401, invalid token → 401, valid token → passes
- [ ] Test `POST /capture`: valid payload → 201 with `memory_id`, missing title → 422, missing content → 422, invalid type → 422, content exceeding max_length → 422
- [ ] Test `POST /capture/slack`: valid text → 201, metadata includes `slack_user`/`slack_channel`/`thread_ts` when provided
- [ ] Test `POST /capture/browser`: valid payload → 201, missing url → 422, auto-title when title omitted
- [ ] Test `POST /capture/email`: valid payload → 201, metadata includes `email_sender` when provided
- [ ] Test `GET /health`: returns 200 when DB reachable, 503 when not (mock `is_reachable`)
- [ ] Test error handling: mock `generate_embedding` to raise → endpoint returns 500

**Requirements:** R1–R8 (all)
**Files:** `tests/test_capture_api.py`
