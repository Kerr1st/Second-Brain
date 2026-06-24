"""Embedding generation via Amazon Bedrock (Titan/Cohere).

Provides a single function to generate vector embeddings from text,
used by the ingestion pipeline for all source types.
"""

import json
import os
import boto3

BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")
EMBEDDING_DIMENSION = 1024

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    return _client


def generate_embedding(text, max_chars=25000):
    """Generate a vector embedding for the given text.

    Args:
        text: Input text to embed.
        max_chars: Truncate text to this length (Titan v2 limit is ~8k tokens ≈ 25k chars).

    Returns:
        List of floats (1024 dimensions for Titan v2).
    """
    client = _get_client()
    truncated = text[:max_chars] if len(text) > max_chars else text

    response = client.invoke_model(
        modelId=EMBEDDING_MODEL,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "inputText": truncated,
            "dimensions": EMBEDDING_DIMENSION,
        }),
    )

    result = json.loads(response["body"].read())
    return result["embedding"]


def generate_embeddings_batch(texts, max_chars=25000):
    """Generate embeddings for multiple texts. Returns list of embedding vectors.

    Processes sequentially — Titan v2 doesn't support batch embedding.
    For large batches, consider adding rate limiting.
    """
    return [generate_embedding(t, max_chars) for t in texts]
