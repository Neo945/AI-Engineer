"""Unit tests for the retrieval and context-assembly layers."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest

from app.database.models.code_chunk import CodeChunk
from app.retrieval.context import ContextAssembler, ContextItem, ContextWindow, format_context
from app.retrieval.retriever import (
    Retriever,
    extract_qualified_names,
    extract_query_terms,
)


def _chunk(
    *,
    content: str,
    file_path: str = "app/main.py",
    start_line: int = 1,
    symbols: list[str] | None = None,
) -> CodeChunk:
    return CodeChunk(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        file_path=file_path,
        start_line=start_line,
        end_line=start_line + content.count("\n"),
        content=content,
        language="python",
        meta={"symbols": symbols or []},
    )


def test_extract_query_terms_dedupes_and_drops_stopwords() -> None:
    terms = extract_query_terms("Fix the Greeter greet method and add a Greeter test")
    assert terms == ["Fix", "Greeter", "greet", "method", "add", "test"]
    assert len(terms) == len(set(terms))


def test_extract_query_terms_keeps_case_variants() -> None:
    assert extract_query_terms("find notes in Notes") == ["find", "notes", "Notes"]


def test_extract_query_terms_drops_short_tokens() -> None:
    assert extract_query_terms("add a b in the lib") == ["add", "lib"]


def test_extract_qualified_names() -> None:
    assert extract_qualified_names("use app.greeter.Greeter and its Greeter.greet") == [
        "app.greeter.Greeter",
        "Greeter.greet",
    ]
    assert extract_qualified_names("no qualified refs here") == []


class _FakeRepository:
    def __init__(self, chunks: list[CodeChunk]) -> None:
        self._chunks = chunks

    async def symbol_search(
        self,
        workspace_id: object,
        name: str,
        *,
        limit: int = 20,
    ) -> Sequence[CodeChunk]:
        return [chunk for chunk in self._chunks if name in (chunk.meta.get("symbols") or [])][
            :limit
        ]

    async def keyword_search(
        self,
        workspace_id: object,
        query: str,
        *,
        limit: int = 20,
    ) -> Sequence[CodeChunk]:
        return [chunk for chunk in self._chunks if query.lower() in chunk.content.lower()][:limit]


@pytest.mark.asyncio
async def test_retriever_ranks_symbol_matches_first() -> None:
    symbol_hit = _chunk(
        content="class Greeter:\n    def greet(self):\n        pass\n",
        symbols=["Greeter", "Greeter.greet"],
    )
    keyword_hit = _chunk(content="welcome = 'find me here'\n", symbols=[])
    repository = _FakeRepository([keyword_hit, symbol_hit])

    hits = await Retriever(repository).ranked_search(uuid.uuid4(), "find Greeter")

    assert [hit.chunk.id for hit in hits] == [symbol_hit.id, keyword_hit.id]
    assert hits[0].score > hits[1].score
    assert hits[0].matches == ("Greeter",)
    assert "find" in hits[1].matches


@pytest.mark.asyncio
async def test_retriever_dedupes_across_symbol_and_keyword() -> None:
    shared = _chunk(content="def greet():\n    return 'hi'\n", symbols=["greet"])
    repository = _FakeRepository([shared])

    hits = await Retriever(repository).ranked_search(uuid.uuid4(), "greet function")

    assert len(hits) == 1
    assert hits[0].score == 10.0


@pytest.mark.asyncio
async def test_retriever_binds_limit() -> None:
    chunks = [
        _chunk(
            content=f"def fn{index}():\n    return {index}\n",
            symbols=[f"fn{index}"],
        )
        for index in range(5)
    ]
    repository = _FakeRepository(chunks)

    hits = await Retriever(repository).ranked_search(uuid.uuid4(), "fn0 fn1 fn2 fn3 fn4", limit=2)

    assert len(hits) == 2


@pytest.mark.asyncio
async def test_context_assembler_bounds_total_chars() -> None:
    chunks = [
        _chunk(content=f"def fn{index}():\n    return {index}\n" * 30, symbols=[f"fn{index}"])
        for index in range(5)
    ]
    repository = _FakeRepository(chunks)
    assembler = ContextAssembler(
        Retriever(repository),
        max_chunks=10,
        max_chars=500,
    )

    window = await assembler.assemble(uuid.uuid4(), "fn0 fn1 fn2 fn3 fn4")

    assert len(window.items) == 1
    assert window.items[0].matches == ("fn0",)
    assert window.total_chars > 500
    assert window.truncated is True


@pytest.mark.asyncio
async def test_context_assembler_bounds_chunk_count() -> None:
    chunks = [
        _chunk(
            content=f"def fn{index}():\n    pass\n",
            symbols=[f"fn{index}"],
        )
        for index in range(5)
    ]
    repository = _FakeRepository(chunks)
    assembler = ContextAssembler(
        Retriever(repository),
        max_chunks=2,
        max_chars=10_000,
    )

    window = await assembler.assemble(uuid.uuid4(), "fn0 fn1 fn2 fn3 fn4")

    assert len(window.items) == 2
    assert window.truncated is True


@pytest.mark.asyncio
async def test_context_assembler_empty_when_no_matches() -> None:
    repository = _FakeRepository([_chunk(content="unrelated code\n")])
    assembler = ContextAssembler(
        Retriever(repository),
        max_chunks=5,
        max_chars=10_000,
    )

    window = await assembler.assemble(uuid.uuid4(), "nothing matches this")

    assert window.items == []
    assert window.total_chars == 0
    assert format_context(window) == ""


def test_format_context_renders_location_and_content() -> None:
    window = ContextWindow(
        items=[
            ContextItem(
                file_path="app/main.py",
                start_line=3,
                end_line=4,
                content="def main():\n    pass\n",
                score=10.0,
                matches=("main",),
            ),
        ],
        total_chars=20,
        terms=("main",),
        truncated=False,
    )
    text = format_context(window)

    assert "### app/main.py:3-4" in text
    assert "def main():" in text
