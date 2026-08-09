"""Repository understanding and retrieval.

The indexer (discovery → language detection → symbol extraction → chunking)
populates ``code_chunks`` so the retrieval and context layers can serve
task-specific context instead of dumping the whole repository. Embeddings
are optional and offline-first: keyword and symbol search work without a
paid API; semantic search can be layered on by backfilling the embedding
column later.
"""

from __future__ import annotations

from app.retrieval.chunking import Chunk, chunk_source
from app.retrieval.discovery import discover_source_files, is_excluded
from app.retrieval.indexer import IndexSummary, RepositoryIndexer
from app.retrieval.languages import Language, detect_language
from app.retrieval.symbols import Symbol, extract_symbols

__all__ = [
    "Chunk",
    "IndexSummary",
    "Language",
    "RepositoryIndexer",
    "Symbol",
    "chunk_source",
    "detect_language",
    "discover_source_files",
    "extract_symbols",
    "is_excluded",
]
