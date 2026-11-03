"""Embedding generation via the explicitly supported Bedrock Titan V2 model.

Provides a single function to generate vector embeddings from text,
used by the ingestion pipeline for all source types.
"""

import json
import os
import boto3
import logging

logger = logging.getLogger(__name__)


class EmbeddingInputTooLarge(ValueError):
    """The caller must chunk this text; it has not been sent to a provider."""

    def __init__(self, chars, limit):
        self.chars, self.limit = chars, limit
        super().__init__(f"Embedding input has {chars} characters; limit is {limit}. Chunk before embedding.")


BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
SUPPORTED_EMBEDDING_MODELS = frozenset({"amazon.titan-embed-text-v2:0"})
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")
EMBEDDING_DIMENSION = 1024

#: Provider hard limit. Bedrock documents Titan Text Embeddings V2 as accepting
#: "up to 8,192 tokens or 50,000 characters" (docs.aws.amazon.com/bedrock/latest/
#: userguide/titan-embedding-models.html, checked 2026-10-05 without a model call).
#: Both limits apply; anything longer is rejected by the service.
EMBEDDING_MAX_CHARS = 50_000

#: Largest single string this codebase sends to the provider. The token limit
#: binds before the character limit: English prose runs ~4 chars/token, but
#: markdown, code, JSON and identifiers run ~2.5-3 chars/token, so 50,000 chars
#: can exceed 8,192 tokens. 20,000 chars stays under 8,192 tokens at 2.5
#: chars/token. ``ingest_content`` chunks far below this (4,000); leaf writers
#: that must embed one row use :func:`embed_leaf`, which bounds the prefix and
#: records the truncation instead of failing or dropping the item.
EMBEDDING_SAFE_CHARS = 20_000

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    return _client


def generate_embedding(text, max_chars=EMBEDDING_MAX_CHARS):
    """Generate a vector embedding for the given text.

    Args:
        text: Input text to embed.
        max_chars: Reject longer input before constructing a provider client.
            Defaults to the provider's documented 50,000-character hard limit;
            this character guard does not replace the 8,192-token limit, so
            callers chunk (``ingest_content``) or bound (:func:`embed_leaf`)
            their input to :data:`EMBEDDING_SAFE_CHARS` or less.

    Returns:
        List of floats (1024 dimensions for Titan v2).
    """
    if EMBEDDING_MODEL not in SUPPORTED_EMBEDDING_MODELS:
        raise ValueError("Unsupported embedding model; this payload requires amazon.titan-embed-text-v2:0")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Embedding input must be non-empty text")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if len(text) > max_chars:
        logger.warning("embedding_input_rejected chars=%d limit=%d", len(text), max_chars)
        raise EmbeddingInputTooLarge(len(text), max_chars)
    client = _get_client()

    response = client.invoke_model(
        modelId=EMBEDDING_MODEL,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "inputText": text,
            "dimensions": EMBEDDING_DIMENSION,
        }),
    )

    result = json.loads(response["body"].read())
    return result["embedding"]


def embed_leaf(text, *, max_chars=EMBEDDING_SAFE_CHARS, embed=None):
    """Embed one row's text with a bounded provider input; never drop the row.

    For leaf memories (one row, one vector: QD feed events, QD document chunks,
    agent-created memories) there is no parent/chunk structure to split into.
    Text within ``max_chars`` is embedded whole. Longer text has its first
    ``max_chars`` characters embedded, the truncation is logged, and the
    returned metadata records it so the row is never silently lossy:
    ``{"embedding_truncated": True, "original_chars": N, "embedded_chars": M}``.
    The stored ``content`` is the caller's responsibility and stays complete.

    Returns ``(embedding, metadata_patch)``; the patch is ``{}`` when nothing
    was truncated. ``embed`` overrides the provider call (tests, caches).
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Embedding input must be non-empty text")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 0 < max_chars <= EMBEDDING_MAX_CHARS:
        raise ValueError(f"max_chars must be an integer between 1 and {EMBEDDING_MAX_CHARS}")
    embed = embed or generate_embedding
    if len(text) <= max_chars:
        return embed(text), {}
    logger.warning("embedding_input_truncated chars=%d embedded=%d", len(text), max_chars)
    return embed(text[:max_chars]), {
        "embedding_truncated": True,
        "original_chars": len(text),
        "embedded_chars": max_chars,
    }
