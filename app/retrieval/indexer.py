"""Repository indexing service.

The :class:`RepositoryIndexer` turns a checkout into rows in ``code_chunks``:
it discovers supported source files, extracts symbols, slices the file into
chunks at symbol boundaries, and persists them scoped to a workspace. It is
offline-first — no embeddings are computed — so indexing needs no paid API;
semantic search can be layered on later by backfilling the ``embedding``
column.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.database.models.code_chunk import CodeChunk
from app.database.repositories.code_chunk import CodeChunkRepository
from app.retrieval.chunking import chunk_source
from app.retrieval.discovery import discover_source_files
from app.retrieval.languages import detect_language
from app.retrieval.symbols import Symbol, extract_symbols

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class IndexSummary:
    """Result of one indexing pass."""

    files_indexed: int
    chunks_created: int
    symbols_indexed: int
    files_skipped: int


class RepositoryIndexer:
    """Builds and replaces a workspace's code index."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def index(
        self,
        workspace_id: uuid.UUID,
        repo_path: str | Path,
    ) -> IndexSummary:
        """(Re)index ``repo_path`` for ``workspace_id``; idempotent.

        The workspace's existing chunks are replaced atomically within one
        transaction: a stale index can never partially shadow a fresh one.
        """
        workspace_uuid = workspace_id
        root = Path(repo_path).resolve()
        code_chunks: list[CodeChunk] = []
        files_indexed = 0
        symbols_indexed = 0
        files_skipped = 0

        for path in discover_source_files(root):
            language = detect_language(path)
            relative = path.relative_to(root).as_posix()
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                files_skipped += 1
                continue
            symbols = extract_symbols(source, language) if language else []
            chunks = chunk_source(source, symbols)
            files_indexed += 1
            symbols_indexed += len(symbols)
            for chunk in chunks:
                code_chunks.append(
                    CodeChunk(
                        workspace_id=workspace_uuid,
                        file_path=relative,
                        start_line=chunk.start_line,
                        end_line=chunk.end_line,
                        content=chunk.content,
                        language=language.name if language else None,
                        meta={"symbols": _symbol_names(chunk.symbols)},
                    )
                )

        repository = CodeChunkRepository(self._session)
        inserted = await repository.replace_workspace_index(workspace_uuid, code_chunks)
        await self._session.commit()
        return IndexSummary(
            files_indexed=files_indexed,
            chunks_created=inserted,
            symbols_indexed=symbols_indexed,
            files_skipped=files_skipped,
        )


def _symbol_names(symbols: Sequence[Symbol]) -> list[str]:
    """Bare and qualified names for JSONB containment search."""
    names: list[str] = []
    for symbol in symbols:
        names.append(symbol.name)
        if symbol.qualified_name and symbol.qualified_name != symbol.name:
            names.append(symbol.qualified_name)
    return names
