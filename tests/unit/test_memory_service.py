"""Unit tests for the memory service and CLI wiring.

The service is tested against an in-memory fake repository so recall logic is
verifiable without a database; the parser wiring just checks that the nested
``memory`` subcommand parses and dispatches to the expected handler.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest

from app.cli.main import build_parser
from app.database.models.enums import MemoryKind
from app.database.models.memory import MemoryEntry
from app.memory.service import MemoryService, format_memory_block

_WORKSPACE_ID = uuid.uuid4()


def _entry(
    *,
    content: str,
    kind: MemoryKind = MemoryKind.FACT,
    source: str = "cli",
    workspace_id: uuid.UUID = _WORKSPACE_ID,
) -> MemoryEntry:
    return MemoryEntry(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        kind=kind,
        content=content,
        source=source,
    )


class _FakeRepository:
    def __init__(self, entries: list[MemoryEntry]) -> None:
        self._entries = entries
        self.added: list[MemoryEntry] = []
        self.deleted = 0

    async def add(self, entity: MemoryEntry) -> MemoryEntry:
        self.added.append(entity)
        self._entries.append(entity)
        return entity

    async def list_for_workspace(
        self,
        workspace_id: uuid.UUID,
        *,
        kind: MemoryKind | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[MemoryEntry]:
        matches = [
            entry
            for entry in self._entries
            if entry.workspace_id == workspace_id and (kind is None or entry.kind == kind)
        ]
        return tuple(matches[offset : offset + limit])

    async def count_for_workspace(self, workspace_id: uuid.UUID) -> int:
        return sum(1 for entry in self._entries if entry.workspace_id == workspace_id)

    async def keyword_search(
        self,
        workspace_id: uuid.UUID,
        query: str,
        *,
        limit: int = 20,
    ) -> Sequence[MemoryEntry]:
        return tuple(
            entry
            for entry in self._entries
            if entry.workspace_id == workspace_id and query.lower() in entry.content.lower()
        )[:limit]

    async def delete_for_workspace(self, workspace_id: uuid.UUID) -> int:
        before = len(self._entries)
        self._entries = [entry for entry in self._entries if entry.workspace_id != workspace_id]
        self.deleted += before - len(self._entries)
        return before - len(self._entries)


def _service(entries: list[MemoryEntry] | None = None) -> MemoryService:
    return MemoryService(_FakeRepository(entries or []))


@pytest.mark.asyncio
async def test_remember_persists_entry_with_defaults() -> None:
    repo = _FakeRepository([])
    service = MemoryService(repo)

    entry = await service.remember(_WORKSPACE_ID, content="  pin to asyncpg  ")

    assert entry.content == "pin to asyncpg"
    assert entry.kind == MemoryKind.FACT
    assert entry.source == "cli"
    assert repo.added == [entry]


@pytest.mark.asyncio
async def test_remember_rejects_empty_content() -> None:
    with pytest.raises(ValueError, match="empty"):
        await _service().remember(_WORKSPACE_ID, content="   ")


@pytest.mark.asyncio
async def test_remember_caps_source_length() -> None:
    entry = await _service().remember(
        _WORKSPACE_ID,
        content="a durable fact",
        source="x" * 500,
    )
    assert len(entry.source) == 120


@pytest.mark.asyncio
async def test_recall_ranks_most_specific_first() -> None:
    pinned = _entry(content="The auth layer pins to asyncpg with a pinned version.")
    generic = _entry(content="asyncpg is also used by the worker service.")
    service = _service([generic, pinned])

    hits = await service.recall(_WORKSPACE_ID, "why asyncpg is pinned")

    assert hits == (pinned, generic)


@pytest.mark.asyncio
async def test_recall_dedupes_across_terms() -> None:
    entry = _entry(content="asyncpg pins are pinned in the auth layer.")
    service = _service([entry])

    hits = await service.recall(_WORKSPACE_ID, "asyncpg pinned")

    assert hits == (entry,)


@pytest.mark.asyncio
async def test_recall_returns_empty_for_noisy_query() -> None:
    entry = _entry(content="asyncpg is the driver.")
    service = _service([entry])

    assert await service.recall(_WORKSPACE_ID, "please a an the") == ()


@pytest.mark.asyncio
async def test_recall_respects_limit() -> None:
    entries = [_entry(content=f"fact about topic {index}") for index in range(5)]
    service = _service(entries)

    hits = await service.recall(_WORKSPACE_ID, "topic", limit=2)

    assert len(hits) == 2


@pytest.mark.asyncio
async def test_list_filters_by_kind() -> None:
    fact = _entry(content="factual note", kind=MemoryKind.FACT)
    decision = _entry(content="decision note", kind=MemoryKind.DECISION)
    service = _service([fact, decision])

    assert await service.list(_WORKSPACE_ID, kind=MemoryKind.DECISION) == (decision,)


@pytest.mark.asyncio
async def test_clear_deletes_workspace_entries() -> None:
    other_workspace = uuid.uuid4()
    repo = _FakeRepository(
        [
            _entry(content="one"),
            _entry(content="two"),
            _entry(content="other workspace", workspace_id=other_workspace),
        ]
    )

    deleted = await MemoryService(repo).clear(_WORKSPACE_ID)

    assert deleted == 2


def test_format_memory_block_renders_entries() -> None:
    entry = _entry(
        content="The auth layer pins to asyncpg.",
        kind=MemoryKind.DECISION,
    )
    block = format_memory_block([entry])

    assert "Project memory relevant to this task" in block
    assert "[decision" in block
    assert "The auth layer pins to asyncpg." in block


def test_format_memory_block_empty_for_no_entries() -> None:
    assert format_memory_block([]) == ""


def test_parser_memory_subcommands_dispatch() -> None:
    parser = build_parser()

    add = parser.parse_args(["memory", "add", "pins", "are", "async"])
    assert add.command == "memory"
    assert add.memory_command == "add"
    assert add.kind == "fact"
    assert add.content == ["pins", "are", "async"]
    assert add.memory_handler.__name__ == "_cmd_memory_add"

    decision = parser.parse_args(["memory", "add", "--kind", "decision", "go"])
    assert decision.kind == "decision"

    listing = parser.parse_args(["memory", "list", "--kind", "preference", "--limit", "7"])
    assert listing.memory_command == "list"
    assert listing.kind == "preference"
    assert listing.limit == 7

    recall = parser.parse_args(["memory", "recall", "why", "asyncpg"])
    assert recall.memory_command == "recall"
    assert recall.query == ["why", "asyncpg"]

    clear = parser.parse_args(["memory", "clear", "-y"])
    assert clear.memory_command == "clear"
    assert clear.yes is True

    with pytest.raises(SystemExit):
        parser.parse_args(["memory", "bogus"])
