"""Unit tests for the embedding layer.

The hashing embedder is deterministic and offline, so its geometry (dimension,
normalization, token-overlap similarity) is fully verifiable without any
network or model. The factory tests mirror the LLM factory tests.
"""

from __future__ import annotations

import math

import pytest

from app.core.config import Settings
from app.retrieval.embedder import (
    Embedder,
    HashingEmbedder,
    OpenAIEmbedder,
    build_embedder,
    cosine_similarity,
)


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(component * component for component in vector))


def test_settings_embedding_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.embedding_enabled is True
    assert settings.embedding_provider == "hash"
    assert settings.embedding_model == "text-embedding-3-small"
    assert settings.embedding_api_key is None
    assert settings.embedding_base_url is None


async def test_hashing_embedder_shape_and_normalization() -> None:
    embedder = HashingEmbedder(dimension=64)
    vectors = await embedder.embed(["hello world", "another chunk of code"])

    assert len(vectors) == 2
    assert all(len(vector) == 64 for vector in vectors)
    assert all(math.isclose(_norm(vector), 1.0, rel_tol=1e-9) for vector in vectors)


async def test_hashing_embedder_is_deterministic() -> None:
    embedder = HashingEmbedder(dimension=128)
    first = await embedder.embed(["Fix the Greeter greet method"])
    second = await embedder.embed(["Fix the Greeter greet method"])

    assert first == second


async def test_hashing_embedder_empty_text_is_zero_vector() -> None:
    embedder = HashingEmbedder(dimension=32)
    (vector,) = await embedder.embed([""])

    assert _norm(vector) == 0.0


async def test_hashing_embedder_similarity_tracks_token_overlap() -> None:
    embedder = HashingEmbedder(dimension=512)
    base, similar, unrelated = await embedder.embed(
        [
            "the authentication token validation logic",
            "authentication token validation helper",
            "unrelated database migration script",
        ]
    )

    assert cosine_similarity(base, similar) > cosine_similarity(base, unrelated)


def test_hashing_embedder_rejects_bad_dimension() -> None:
    with pytest.raises(ValueError, match="dimension"):
        HashingEmbedder(dimension=0)


def test_cosine_similarity_edge_cases() -> None:
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert math.isclose(cosine_similarity([1.0, 1.0], [1.0, 1.0]), 1.0)


def test_hashing_embedder_satisfies_protocol() -> None:
    assert isinstance(HashingEmbedder(dimension=8), Embedder)


def test_build_embedder_defaults_to_hashing() -> None:
    embedder = build_embedder(Settings(_env_file=None, embedding_dimension=256))

    assert isinstance(embedder, HashingEmbedder)
    assert embedder.dimension == 256


def test_build_embedder_builds_openai_embedder() -> None:
    settings = Settings(
        _env_file=None,
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_api_key="secret",
    )
    embedder = build_embedder(settings)

    assert isinstance(embedder, OpenAIEmbedder)
    assert embedder.model == "text-embedding-3-small"


def test_build_embedder_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unsupported embedding_provider"):
        build_embedder(Settings(_env_file=None, embedding_provider="bogus"))


async def test_openai_embedder_delegates_to_sdk() -> None:
    from typing import Any, cast

    class _FakeItem:
        def __init__(self, embedding: list[float]) -> None:
            self.embedding = embedding

    class _FakeResponse:
        def __init__(self) -> None:
            self.data = [_FakeItem([0.1, 0.2]), _FakeItem([0.3, 0.4])]

    class _FakeEmbeddings:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def create(self, **kwargs: Any) -> _FakeResponse:
            self.calls.append(kwargs)
            return _FakeResponse()

    class _FakeClient:
        def __init__(self) -> None:
            self.embeddings = _FakeEmbeddings()

    embedder = OpenAIEmbedder(model="text-embedding-3-small", api_key="secret")
    fake = cast(Any, _FakeClient())
    embedder._client = fake

    vectors = await embedder.embed(["a", "b"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert fake.embeddings.calls[0]["model"] == "text-embedding-3-small"
    assert fake.embeddings.calls[0]["input"] == ["a", "b"]
    assert await embedder.embed([]) == []
