# Requirements Document: Capture API

## Introduction

The Second Brain currently accepts input only through terminal chats (Kiro CLI/IDE sessions) and scheduled scraping jobs (YouTube, bookmarks, Crawlee). There is no way to capture knowledge from Slack, mobile devices, web browsers, or email. The architecture diagram in `docs/AGENTS.md` identifies a "Capture API" as a planned layer between external channels and the ingestion pipeline.

This spec defines a lightweight HTTP API that accepts knowledge from four channels — Slack, iOS Shortcuts, browser extensions, and forwarded emails — and stores it in PostgreSQL via the existing `create_memory()` and `generate_embedding()` functions. The API runs as a local macOS service (not a cloud deployment), uses FastAPI for minimal overhead, and requires bearer-token authentication since the endpoint is network-exposed.

Because the API runs locally (no public URL), Slack integration uses a client-side approach: a Slack Workflow or bot posts to the Capture API, rather than Slack pushing to a webhook. The `/capture/slack` endpoint accepts a simple JSON payload with the message text and metadata — it is not a Slack-initiated webhook receiver.

Each channel submits content in a channel-specific format. The API normalizes all inputs into the common fields required by `create_memory()` (type, title, content, source_url, source_type, tags, metadata) and runs them through the same embedding + classification pipeline used by `mcp_server.py`'s `memory_create` tool. Content is capped at 100KB per request to prevent abuse — `generate_embedding()` already truncates at 25K chars, so larger payloads waste storage without improving searchability.

## Glossary

- **Capture API**: The FastAPI HTTP server defined in `src/capture_api.py` that accepts knowledge from external channels and stores it in the Second Brain.
- **Channel**: A source of incoming knowledge — one of: Slack (client-push), mobile shortcut (iOS Shortcuts), browser extension, or email forward.
- **Bearer Token**: A shared secret in the `Authorization: Bearer <token>` header, used to authenticate all API requests. Stored in the `CAPTURE_API_TOKEN` environment variable.
- **Normalized Payload**: The common set of fields (type, title, content, source_url, source_type, tags, metadata, project) that all channel-specific inputs are converted to before storage.
- **create_memory()**: The function in `src/db.py` that inserts a memory row into PostgreSQL.
- **generate_embedding()**: The function in `src/embeddings.py` that produces a 1024-dim vector via Bedrock Titan v2.
- **classify_memory()**: The function in `src/classify.py` that deterministically classifies a memory as semantic, episodic, or procedural.
- **compute_depth_score()**: The function in `src/depth.py` that scores content depth from 0.0 to 1.0.
- **normalize_project_tag()**: The function in `src/project.py` that normalizes project tags.
- **Connection Pool**: The `SimpleConnectionPool` in `src/db.py`, accessed via the `get_connection()` context manager.

## Requirements

### Requirement 1: Generic Capture Endpoint

**User Story:** As a user, I want a single HTTP POST endpoint that accepts knowledge from any channel in a unified JSON format so that simple integrations (iOS Shortcuts, scripts, and other HTTP clients) can store memories without channel-specific logic. This endpoint is the primary integration point for iOS Shortcuts and any future channels that can make a simple HTTP POST.

#### Acceptance Criteria

1. THE Capture API SHALL expose `POST /capture` accepting a JSON body with required fields `title` (string, max 500 chars) and `content` (string, max 100,000 chars), and optional fields `type` (string, default `"research"`), `source_url` (string), `source_type` (string), `tags` (list of strings), `metadata` (object), and `project` (string).
2. WHEN a valid request is received, THE endpoint SHALL generate an embedding via `generate_embedding()`, classify via `classify_memory()`, compute depth via `compute_depth_score()`, normalize the project tag via `normalize_project_tag()`, and store via `create_memory()` — the same pipeline as `mcp_server.py`'s `memory_create` tool.
3. THE endpoint SHALL return HTTP 201 with a JSON body containing `{"memory_id": "<uuid>"}` on success.
4. WHEN `title` or `content` is missing or empty, THE endpoint SHALL return HTTP 422 with a descriptive error.
5. WHEN `type` is provided, it SHALL be validated against the allowed set: `idea`, `synthesis`, `research`, `insight`, `question`, `decision`, `priority`, `project`, `connection`, `source`. Invalid values SHALL return HTTP 422.

### Requirement 2: Slack Capture Endpoint

**User Story:** As a user, I want to send Slack messages and threads to the Second Brain so that knowledge shared in Slack conversations is captured. Since the API runs locally without a public URL, a Slack Workflow or bot script posts to this endpoint — Slack does not push to it directly.

#### Acceptance Criteria

1. THE Capture API SHALL expose `POST /capture/slack` accepting a JSON body with required field `text` (string, max 100,000 chars), and optional fields `user_name` (string), `channel_name` (string), `thread_ts` (string), `tags` (list of strings), and `project` (string).
2. THE endpoint SHALL store the memory with `source_type="slack"`, title derived from the first 80 characters of `text`, and metadata containing `{"slack_user": "<user_name>", "slack_channel": "<channel_name>"}` when those fields are provided.
3. WHEN the payload contains a `thread_ts` field, THE endpoint SHALL include `thread_ts` in the metadata.
4. THE endpoint SHALL return HTTP 201 with `{"memory_id": "<uuid>"}`.
5. WHEN `text` is missing or empty, THE endpoint SHALL return HTTP 422.

### Requirement 3: Browser Extension Endpoint

**User Story:** As a user, I want to capture selected text and the page URL from my browser so that I can save web content to the Second Brain with one click.

#### Acceptance Criteria

1. THE Capture API SHALL expose `POST /capture/browser` accepting a JSON body with required fields `url` (string) and `content` (string, max 100,000 chars), and optional fields `title` (string, max 500 chars), `tags` (list of strings), and `project` (string).
2. WHEN `title` is not provided, THE endpoint SHALL use the first 80 characters of `content` as the title.
3. THE endpoint SHALL store the memory with `source_type="browser_extension"` and `source_url` set to the provided `url`.
4. THE endpoint SHALL return HTTP 201 with `{"memory_id": "<uuid>"}`.
5. WHEN `url` or `content` is missing or empty, THE endpoint SHALL return HTTP 422.

### Requirement 4: Email Forward Endpoint

**User Story:** As a user, I want to forward emails to the Second Brain so that knowledge received via email is captured without manual copy-paste.

#### Acceptance Criteria

1. THE Capture API SHALL expose `POST /capture/email` accepting a JSON body with required fields `subject` (string, max 500 chars) and `body` (string, max 100,000 chars), and optional fields `sender` (string), `tags` (list of strings), and `project` (string).
2. THE endpoint SHALL store the memory with `source_type="email"`, title set to the email subject, content set to the email body, and metadata containing `{"email_sender": "<sender>"}` when sender is provided.
3. THE endpoint SHALL return HTTP 201 with `{"memory_id": "<uuid>"}`.
4. WHEN `subject` or `body` is missing or empty, THE endpoint SHALL return HTTP 422.

### Requirement 5: Authentication

**User Story:** As a user, I want all Capture API endpoints protected by a bearer token so that only authorized clients can store memories.

#### Acceptance Criteria

1. ALL Capture API endpoints (including channel-specific ones) SHALL require an `Authorization: Bearer <token>` header.
2. THE expected token SHALL be read from the `CAPTURE_API_TOKEN` environment variable at startup.
3. WHEN the `CAPTURE_API_TOKEN` environment variable is not set, THE server SHALL refuse to start and exit with a non-zero status code after logging an error message. The token check SHALL be inside the `__main__` guard so that test imports do not trigger the exit.
4. WHEN a request is missing the `Authorization` header or provides an invalid token, THE endpoint SHALL return HTTP 401 with `{"detail": "Invalid or missing token"}`.
5. THE token comparison SHALL use a constant-time comparison function (`hmac.compare_digest`) to prevent timing attacks.

### Requirement 6: Health Check

**User Story:** As a user, I want a health check endpoint so that monitoring tools and scripts can verify the API is running and the database is reachable.

#### Acceptance Criteria

1. THE Capture API SHALL expose `GET /health` that does NOT require authentication.
2. THE health endpoint SHALL check database connectivity using `is_reachable()` from `src/db.py`.
3. WHEN the database is reachable, THE endpoint SHALL return HTTP 200 with `{"status": "healthy", "database": "connected"}`.
4. WHEN the database is not reachable, THE endpoint SHALL return HTTP 503 with `{"status": "unhealthy", "database": "disconnected"}`.

### Requirement 7: Server Configuration

**User Story:** As a developer, I want the Capture API server to be configurable via environment variables and runnable as a local macOS service so that it integrates with the existing launchd scheduling system.

#### Acceptance Criteria

1. THE server SHALL listen on the host and port specified by `CAPTURE_API_HOST` (default `"127.0.0.1"`) and `CAPTURE_API_PORT` (default `8100`) environment variables.
2. THE server SHALL be runnable via `python -m src.capture_api` (matching the `mcp_server.py` pattern).
3. THE server SHALL use the existing `src/db.py` connection pool — no separate database configuration.
4. A launchd plist (`scheduling/com.second-brain.capture-api.plist`) SHALL be provided to run the API as a persistent local service, with `StandardOutPath` and `StandardErrorPath` configured for log capture.
5. A shell wrapper script (`scripts/capture_api.sh`) SHALL be provided to start the server with the correct Python environment and environment variables.

### Requirement 8: Error Handling and Logging

**User Story:** As a developer, I want consistent error handling and structured logging so that failures are diagnosable without exposing internal details to clients.

#### Acceptance Criteria

1. ALL endpoints SHALL catch exceptions from `generate_embedding()`, `create_memory()`, and other pipeline functions and return HTTP 500 with `{"detail": "Internal server error"}` — no stack traces or internal details in the response.
2. THE server SHALL log all requests (method, path, status code) and all errors (with stack traces) using Python's `logging` module.
3. WHEN an embedding generation fails (Bedrock unavailable), THE error log SHALL include the exception details for debugging.
