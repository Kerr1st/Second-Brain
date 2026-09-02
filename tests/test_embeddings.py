"""Behavior tests for provider-neutral embedding generation."""

from unittest.mock import Mock, patch

import pytest

import src.embeddings as embeddings


@pytest.fixture(autouse=True)
def reset_embedding_adapter(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_EMBEDDING_MODEL", "bge-m3")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    embeddings.reset_embedding_adapter()
    yield
    embeddings.reset_embedding_adapter()


def test_ollama_adapter_generates_one_1024_dimension_embedding():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"embeddings": [[0.25] * 1024]}

    with patch("src.embeddings.requests.post", return_value=response) as post:
        result = embeddings.generate_embedding("remember this")

    assert result == [0.25] * 1024
    post.assert_called_once_with(
        "http://127.0.0.1:11434/api/embed",
        json={"model": "bge-m3", "input": ["remember this"], "truncate": True},
        timeout=120.0,
    )


def test_ollama_adapter_uses_native_batch_endpoint():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "embeddings": [[0.1] * 1024, [0.2] * 1024],
    }

    with patch("src.embeddings.requests.post", return_value=response) as post:
        result = embeddings.generate_embeddings_batch(["one", "two"])

    assert result == [[0.1] * 1024, [0.2] * 1024]
    assert post.call_args.kwargs["json"]["input"] == ["one", "two"]


def test_ollama_adapter_rejects_wrong_vector_dimension():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"embeddings": [[0.25] * 3]}

    with patch("src.embeddings.requests.post", return_value=response):
        with pytest.raises(ValueError, match="expected 1024 dimensions, received 3"):
            embeddings.generate_embedding("wrong shape")


def test_active_embedding_space_records_provider_model_and_dimension():
    assert embeddings.active_embedding_space() == "ollama:bge-m3:1024"


def test_unknown_embedding_provider_fails_closed(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "mystery")
    embeddings.reset_embedding_adapter()

    with pytest.raises(ValueError, match="Unsupported EMBEDDING_PROVIDER"):
        embeddings.generate_embedding("text")


def test_legacy_bedrock_space_cannot_be_selected_as_active(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "bedrock")
    embeddings.reset_embedding_adapter()

    with pytest.raises(ValueError, match="legacy-only"):
        embeddings.generate_embedding("must not mix vector spaces")
