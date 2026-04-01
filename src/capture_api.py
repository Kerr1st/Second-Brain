"""Capture API — lightweight HTTP endpoint for multi-channel knowledge capture.

Accepts knowledge from Slack, iOS Shortcuts, browser extensions, and email,
normalizes it, and stores it in the Second Brain via the same pipeline as
the MCP server's memory_create tool.

Run: python -m src.capture_api
"""

import hmac
import logging
import os
import sys

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from src.classify import classify_memory
from src.db import create_memory, is_reachable
from src.depth import compute_depth_score
from src.embeddings import generate_embedding
from src.project import normalize_project_tag

logger = logging.getLogger(__name__)

VALID_TYPES = {
    "idea", "synthesis", "research", "insight", "question",
    "decision", "priority", "project", "connection", "source",
}

app = FastAPI(title="Second Brain Capture API")


# --- Auth dependency ---

def verify_token(authorization: str = Header(...)):
    token_expected = os.environ.get("CAPTURE_API_TOKEN", "")
    scheme, _, token = authorization.partition(" ")
    if not token_expected or scheme.lower() != "bearer" or not hmac.compare_digest(token, token_expected):
        raise HTTPException(401, "Invalid or missing token")


# --- Request logging middleware ---

@app.middleware("http")
async def log_requests(request: Request, call_next):
    response: Response = await call_next(request)
    logger.info("%s %s %s", request.method, request.url.path, response.status_code)
    return response


# --- Pydantic models ---

class CaptureRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., min_length=1, max_length=100_000)
    type: str = "research"
    source_url: str | None = None
    source_type: str | None = None
    tags: list[str] | None = None
    metadata: dict | None = None
    project: str | None = None
    encoding_context: str | None = None

    @field_validator("type")
    @classmethod
    def validate_type(cls, v):
        if v not in VALID_TYPES:
            raise ValueError(f"type must be one of: {', '.join(sorted(VALID_TYPES))}")
        return v


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


# --- Shared storage pipeline ---

def _store_memory(title, content, type="research", source_url=None,
                  source_type=None, tags=None, metadata=None, project=None,
                  encoding_context=None):
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
        encoding_context=encoding_context,
    )


# --- Endpoints ---

@app.get("/health")
def health():
    if is_reachable():
        return {"status": "healthy", "database": "connected"}
    return JSONResponse(
        status_code=503,
        content={"status": "unhealthy", "database": "disconnected"},
    )


@app.post("/capture", status_code=201, dependencies=[Depends(verify_token)])
def capture(req: CaptureRequest):
    try:
        memory_id = _store_memory(
            title=req.title, content=req.content, type=req.type,
            source_url=req.source_url, source_type=req.source_type,
            tags=req.tags, metadata=req.metadata, project=req.project,
            encoding_context=req.encoding_context,
        )
    except Exception:
        logger.exception("Failed to store memory via /capture")
        raise HTTPException(500, "Internal server error")
    return {"memory_id": memory_id}


@app.post("/capture/slack", status_code=201, dependencies=[Depends(verify_token)])
def capture_slack(req: SlackCaptureRequest):
    title = req.text[:80]
    meta = {}
    if req.user_name:
        meta["slack_user"] = req.user_name
    if req.channel_name:
        meta["slack_channel"] = req.channel_name
    if req.thread_ts:
        meta["thread_ts"] = req.thread_ts
    try:
        memory_id = _store_memory(
            title=title, content=req.text, source_type="slack",
            tags=req.tags, metadata=meta, project=req.project,
        )
    except Exception:
        logger.exception("Failed to store memory via /capture/slack")
        raise HTTPException(500, "Internal server error")
    return {"memory_id": memory_id}


@app.post("/capture/browser", status_code=201, dependencies=[Depends(verify_token)])
def capture_browser(req: BrowserCaptureRequest):
    title = req.title if req.title else req.content[:80]
    try:
        memory_id = _store_memory(
            title=title, content=req.content, source_type="browser_extension",
            source_url=req.url, tags=req.tags, project=req.project,
        )
    except Exception:
        logger.exception("Failed to store memory via /capture/browser")
        raise HTTPException(500, "Internal server error")
    return {"memory_id": memory_id}


@app.post("/capture/email", status_code=201, dependencies=[Depends(verify_token)])
def capture_email(req: EmailCaptureRequest):
    meta = {}
    if req.sender:
        meta["email_sender"] = req.sender
    try:
        memory_id = _store_memory(
            title=req.subject, content=req.body, source_type="email",
            tags=req.tags, metadata=meta, project=req.project,
        )
    except Exception:
        logger.exception("Failed to store memory via /capture/email")
        raise HTTPException(500, "Internal server error")
    return {"memory_id": memory_id}


# --- Entry point ---

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not os.environ.get("CAPTURE_API_TOKEN"):
        logger.fatal("CAPTURE_API_TOKEN environment variable is not set")
        sys.exit(1)

    host = os.environ.get("CAPTURE_API_HOST", "127.0.0.1")
    port = int(os.environ.get("CAPTURE_API_PORT", "8100"))
    uvicorn.run(app, host=host, port=port)
