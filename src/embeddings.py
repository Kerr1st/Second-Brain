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

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    return _client


def generate_embedding(text, max_chars=50000):
    """Generate a vector embedding for the given text.

    Args:
        text: Input text to embed.
        max_chars: Reject longer input before constructing a provider client.
            This character guard does not replace the provider token limit.

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
