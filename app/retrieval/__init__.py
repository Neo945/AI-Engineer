"""Repository understanding and retrieval.

The indexer (discovery → language detection → symbol extraction → chunking)
populates ``code_chunks`` so the retrieval and context layers can serve
task-specific context instead of dumping the whole repository. Embeddings are
offline-first: the default hashing embedder backfills the pgvector columns
without a paid API, and keyword + symbol search work regardless.
"""

from __future__ import annotations

from app.retrieval.chunking import Chunk, chunk_source
from app.retrieval.context import ContextAssembler, ContextItem, ContextWindow, format_context
from app.retrieval.discovery import discover_source_files, is_excluded
from app.retrieval.embedder import (
    Embedder,
    HashingEmbedder,
    OpenAIEmbedder,
    build_embedder,
    cosine_similarity,
)
from app.retrieval.indexer import IndexSummary, RepositoryIndexer
from app.retrieval.languages import Language, detect_language
from app.retrieval.retriever import Retriever, ScoredChunk, extract_query_terms
from app.retrieval.symbols import Symbol, extract_symbols

__all__ = [
    "Chunk",
    "ContextAssembler",
    "ContextItem",
    "ContextWindow",
    "Embedder",
    "HashingEmbedder",
    "IndexSummary",
    "Language",
    "OpenAIEmbedder",
    "RepositoryIndexer",
    "Retriever",
    "ScoredChunk",
    "Symbol",
    "build_embedder",
    "chunk_source",
    "cosine_similarity",
    "detect_language",
    "discover_source_files",
    "extract_query_terms",
    "extract_symbols",
    "format_context",
    "is_excluded",
]
