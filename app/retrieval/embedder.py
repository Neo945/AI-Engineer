"""Pluggable text embeddings for semantic retrieval.

Mirrors the :class:`LLMProvider` abstraction: an :class:`Embedder` protocol
with an offline-first default (a deterministic feature-hashing embedder that
needs no API key or model) and an optional OpenAI-compatible adapter for real
embeddings. Embeddings back the pgvector columns on ``code_chunks`` and
``memory_entries`` so semantic recall layers on top of the existing keyword
and symbol search.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from openai import AsyncOpenAI

from app.core.config import Settings

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


@runtime_checkable
class Embedder(Protocol):
    """Contract for services that turn text into embedding vectors.

    All conforming embedders are offline-safe to construct, produce
    ``dimension``-length unit vectors, and are idempotent per input so
    re-indexing and recall are deterministic.
    """

    name: str
    dimension: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def close(self) -> None: ...


class HashingEmbedder:
    """Deterministic, offline feature-hashing embedder.

    Each lowercased token is hashed to a vector index with a signed hash and
    the resulting bag-of-words vector is L2-normalized, so cosine similarity
    approximates token overlap. It needs no network and no model, which makes
    it the offline-first default: indexing and recall work without a paid API.
    """

    name = "hash"

    def __init__(self, *, dimension: int = 1536) -> None:
        if dimension < 1:
            raise ValueError("embedding dimension must be positive")
        self.dimension = dimension
        self._cache: dict[str, tuple[int, int]] = {}

    def _shard(self, token: str) -> tuple[int, int]:
        cached = self._cache.get(token)
        if cached is not None:
            return cached
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        index = value % self.dimension
        sign = 1.0 if (value >> 63) & 1 else -1.0
        shard = (index, int(sign))
        self._cache[token] = shard
        return shard

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in _TOKEN_RE.findall(text.lower()):
            index, sign = self._shard(token)
            vector[index] += sign
        norm = math.sqrt(sum(component * component for component in vector))
        if norm > 0:
            vector = [component / norm for component in vector]
        return vector

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed ``texts``, one deterministic unit vector per input."""
        return [self._embed_one(text) for text in texts]

    async def close(self) -> None:
        """Release any resources; the hashing embedder holds none."""


class OpenAIEmbedder:
    """OpenAI-compatible embeddings adapter.

    Callers may point ``base_url`` at any server that speaks the OpenAI
    ``/embeddings`` protocol (OpenAI, vLLM, Ollama, openai-compatible local
    endpoints) so real semantic embeddings are a configuration change, not a
    code change.
    """

    name = "openai"

    def __init__(
        self,
        *,
        model: str,
        dimension: int = 1536,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.model = model
        self.dimension = dimension
        self.base_url = base_url
        resolved_key = api_key
        if resolved_key is None:
            resolved_key = os.environ.get("OPENAI_API_KEY")
        if resolved_key is None and base_url is not None:
            resolved_key = "local"
        self._client = AsyncOpenAI(
            api_key=resolved_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=2,
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed ``texts`` with the configured model, order preserved."""
        if not texts:
            return []
        response = await self._client.embeddings.create(model=self.model, input=list(texts))
        return [list(item.embedding) for item in response.data]

    async def close(self) -> None:
        """Close the underlying HTTP client, releasing its connection pool."""
        await self._client.close()


def build_embedder(settings: Settings) -> Embedder:
    """Build the configured embedding adapter.

    Args:
        settings: Application settings. ``embedding_provider`` selects the
            adapter (``hash`` or ``openai``); ``embedding_base_url`` may route
            the OpenAI adapter to an OpenAI-compatible local backend.

    Returns:
        A ready-to-use :class:`Embedder`.

    Raises:
        ValueError: If ``settings.embedding_provider`` is unsupported.
    """
    provider = settings.embedding_provider.strip().lower()
    if provider in {"hash", "hashing", "local", "none"}:
        return HashingEmbedder(dimension=settings.embedding_dimension)
    if provider == "openai":
        return OpenAIEmbedder(
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            api_key=settings.embedding_api_key or settings.llm_api_key,
            base_url=settings.embedding_base_url or settings.llm_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    raise ValueError(
        f"unsupported embedding_provider {settings.embedding_provider!r}; "
        f"expected one of 'hash', 'openai'"
    )


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity between two vectors; 0.0 when either is empty/zero."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)
