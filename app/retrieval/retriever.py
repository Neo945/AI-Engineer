"""Task-specific retrieval over the ``code_chunks`` index.

The retriever turns a natural-language goal into a small set of search
terms — function/class names and qualified ``Module.symbol`` references get
exact symbol lookups, everything else becomes a substring scan — and merges
the hits into a single scored, de-duplicated ranking. It is deliberately
dependency-free: no embeddings, no external search service, so it works as
soon as ``engineer index`` has run.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.database.models.code_chunk import CodeChunk

#: English function words and generic task verbs that add no retrieval signal.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "here",
        "how",
        "if",
        "in",
        "into",
        "is",
        "it",
        "no",
        "not",
        "of",
        "on",
        "or",
        "please",
        "should",
        "such",
        "that",
        "the",
        "then",
        "there",
        "thing",
        "this",
        "to",
        "use",
        "using",
        "we",
        "what",
        "when",
        "which",
        "why",
        "with",
        "would",
    }
)

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUALIFIED_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")

_SYMBOL_MATCH_SCORE = 10.0
_KEYWORD_MAX_CHARLEN = 2_000


@dataclass(frozen=True)
class ScoredChunk:
    """One retrieved chunk plus its ranking metadata."""

    chunk: CodeChunk
    score: float
    matches: tuple[str, ...]


def extract_query_terms(goal: str) -> list[str]:
    """Return the identifiers in ``goal`` as deduplicated search terms.

    Tokens shorter than two characters and stopwords are dropped. Dedup is
    case-sensitive on purpose: symbol lookup is exact, so both ``notes`` and
    ``Notes`` must survive — a capitalized spelling is what matches the
    symbol index while the lowercase form only matches keyword scans.
    """
    seen: set[str] = set()
    terms: list[str] = []
    for token in _IDENTIFIER_RE.findall(goal):
        if token in seen or token.lower() in _STOPWORDS or len(token) < 2:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def extract_qualified_names(goal: str) -> list[str]:
    """Return ``Module.symbol`` references in ``goal`` (deduplicated)."""
    seen: set[str] = set()
    names: list[str] = []
    for match in _QUALIFIED_RE.findall(goal):
        if match not in seen:
            seen.add(match)
            names.append(match)
    return names


def _keyword_score(content: str, term: str, *, lower_content: str) -> float:
    """Score a substring hit: longer terms weigh more, tighter chunks win."""
    if term.lower() not in lower_content:
        return 0.0
    weight = len(term)
    charlen = len(content)
    compactness = max(0.0, 1.0 - min(charlen, _KEYWORD_MAX_CHARLEN) / _KEYWORD_MAX_CHARLEN)
    return weight + compactness * 2.0


class ChunkSearch(Protocol):
    """Structural interface the retriever needs over the ``code_chunks`` index.

    Satisfied by :class:`CodeChunkRepository` and by in-memory fakes in
    tests, so retrieval logic is unit-testable without a database.
    """

    async def symbol_search(
        self,
        workspace_id: uuid.UUID,
        name: str,
        *,
        limit: int = 20,
    ) -> Sequence[CodeChunk]: ...

    async def keyword_search(
        self,
        workspace_id: uuid.UUID,
        query: str,
        *,
        limit: int = 20,
    ) -> Sequence[CodeChunk]: ...


class Retriever:
    """Merge symbol and keyword hits into a scored, de-duplicated ranking.

    Args:
        repository: Search source over the workspace's ``code_chunks``.
    """

    def __init__(self, repository: ChunkSearch) -> None:
        self._repository = repository

    async def ranked_search(
        self,
        workspace_id: uuid.UUID,
        goal: str,
        *,
        limit: int = 20,
    ) -> list[ScoredChunk]:
        """Search the workspace index for ``goal``, best matches first.

        Symbol lookups (exact names and qualified ``Module.symbol``
        references) dominate the ranking; keyword hits provide recall. Each
        chunk appears at most once, scored by its strongest match.
        """
        terms = extract_query_terms(goal)
        qualified = extract_qualified_names(goal)

        ranked: dict[uuid.UUID, ScoredChunk] = {}

        async def _record(chunk: CodeChunk, score: float, match: str) -> None:
            current = ranked.get(chunk.id)
            if current is None:
                ranked[chunk.id] = ScoredChunk(chunk=chunk, score=score, matches=(match,))
            elif score > current.score:
                ranked[chunk.id] = ScoredChunk(
                    chunk=chunk,
                    score=score,
                    matches=(*current.matches, match),
                )

        for name in [*qualified, *terms]:
            for chunk in await self._repository.symbol_search(workspace_id, name, limit=limit):
                await _record(chunk, _SYMBOL_MATCH_SCORE, name)

        lower_contents: dict[uuid.UUID, str] = {}
        for term in terms:
            for chunk in await self._repository.keyword_search(workspace_id, term, limit=limit):
                if chunk.id not in lower_contents:
                    lower_contents[chunk.id] = chunk.content.lower()
                score = _keyword_score(chunk.content, term, lower_content=lower_contents[chunk.id])
                if score > 0:
                    await _record(chunk, score, term)

        return sorted(ranked.values(), key=lambda hit: hit.score, reverse=True)[:limit]
