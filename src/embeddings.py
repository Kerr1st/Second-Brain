"""Provider-neutral embedding generation.

The active default is the local Ollama BGE-M3 Adapter. The Bedrock Titan
Adapter remains available only for explicit programmatic legacy diagnostics.
Callers intentionally keep the small ``generate_embedding(text)`` Interface.
"""

from __future__ import annotations

import json
import math
import os
from typing import Protocol

import boto3
import requests


EMBEDDING_DIMENSION = 1024
DEFAULT_PROVIDER = "ollama"
DEFAULT_OLLAMA_MODEL = "bge-m3"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_BEDROCK_MODEL = "amazon.titan-embed-text-v2:0"


class EmbeddingAdapter(Protocol):
    """Implementation seam for one embedding vector space."""

    @property
    def space(self) -> str: ...

    def generate_batch(self, texts: list[str], max_chars: int) -> list[list[float]]: ...


def _validate_embeddings(vectors: object, expected_count: int) -> list[list[float]]:
    if not isinstance(vectors, list) or len(vectors) != expected_count:
        received = len(vectors) if isinstance(vectors, list) else type(vectors).__name__
        raise ValueError(
            f"embedding provider returned {received}; expected {expected_count} vectors"
        )
    validated: list[list[float]] = []
    for vector in vectors:
        if not isinstance(vector, list):
            raise ValueError("embedding provider returned a non-list vector")
        if len(vector) != EMBEDDING_DIMENSION:
            raise ValueError(
                f"expected {EMBEDDING_DIMENSION} dimensions, received {len(vector)}"
            )
        floats = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in floats):
            raise ValueError("embedding provider returned a non-finite value")
        validated.append(floats)
    return validated


class OllamaEmbeddingAdapter:
    """Local embedding Adapter using Ollama's native batch endpoint."""

    def __init__(self) -> None:
        self.model = os.environ.get("OLLAMA_EMBEDDING_MODEL", DEFAULT_OLLAMA_MODEL)
        self.base_url = os.environ.get(
            "OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL
        ).rstrip("/")
        self.timeout = float(os.environ.get("OLLAMA_EMBEDDING_TIMEOUT", "120"))

    @property
    def space(self) -> str:
        return f"ollama:{self.model}:{EMBEDDING_DIMENSION}"

    def generate_batch(self, texts: list[str], max_chars: int) -> list[list[float]]:
        if not texts:
            return []
        truncated = [text[:max_chars] for text in texts]
        response = requests.post(
            f"{self.base_url}/api/embed",
            json={"model": self.model, "input": truncated, "truncate": True},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return _validate_embeddings(response.json().get("embeddings"), len(texts))


class BedrockTitanEmbeddingAdapter:
    """Legacy remote Adapter that cannot be selected as the active space."""

    def __init__(self) -> None:
        self.region = os.environ.get("BEDROCK_REGION", "us-east-1")
        self.model = os.environ.get("EMBEDDING_MODEL", DEFAULT_BEDROCK_MODEL)
        self._client = None

    @property
    def space(self) -> str:
        return f"bedrock:{self.model}:{EMBEDDING_DIMENSION}"

    def _get_client(self):
        if self._client is None:
            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def generate_batch(self, texts: list[str], max_chars: int) -> list[list[float]]:
        vectors = []
        for text in texts:
            response = self._get_client().invoke_model(
                modelId=self.model,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(
                    {
                        "inputText": text[:max_chars],
                        "dimensions": EMBEDDING_DIMENSION,
                    }
                ),
            )
            vectors.append(json.loads(response["body"].read())["embedding"])
        return _validate_embeddings(vectors, len(texts))


_adapter: EmbeddingAdapter | None = None


def reset_embedding_adapter() -> None:
    """Forget the cached Adapter so configuration changes take effect."""
    global _adapter
    _adapter = None


def _get_adapter() -> EmbeddingAdapter:
    global _adapter
    if _adapter is None:
        provider = os.environ.get("EMBEDDING_PROVIDER", DEFAULT_PROVIDER).lower()
        if provider == "ollama":
            _adapter = OllamaEmbeddingAdapter()
        elif provider in {"bedrock", "titan"}:
            raise ValueError(
                "Bedrock Titan is legacy-only and cannot be selected as the active "
                "embedding space"
            )
        else:
            raise ValueError(f"Unsupported EMBEDDING_PROVIDER: {provider}")
    return _adapter


def active_embedding_space() -> str:
    """Return the durable identity of the active embedding vector space."""
    return _get_adapter().space


def generate_embedding(text: str, max_chars: int = 25000) -> list[float]:
    """Generate one embedding in the active vector space."""
    return generate_embeddings_batch([text], max_chars=max_chars)[0]


def generate_embeddings_batch(
    texts: list[str], max_chars: int = 25000
) -> list[list[float]]:
    """Generate multiple embeddings in one provider-native request when supported."""
    return _get_adapter().generate_batch(texts, max_chars)
