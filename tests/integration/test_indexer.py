"""Integration tests for the repository indexer against real PostgreSQL.

These verify that indexing persists ``code_chunks`` for a workspace, that a
re-index is idempotent, and that keyword and symbol search over the index
return the expected chunks. They require the local infrastructure (``make
up``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.container import Container
from app.database.models.session import Session
from app.database.models.user import User
from app.database.models.workspace import Workspace
from app.database.repositories.code_chunk import CodeChunkRepository
from app.retrieval.context import ContextAssembler
from app.retrieval.indexer import RepositoryIndexer

pytestmark = pytest.mark.integration


def _build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "greeter.py").write_text(
        "import os\n"
        "class Greeter:\n"
        "    def greet(self, name):\n"
        "        return f'Hello, {name}'\n"
        "def default_greeter():\n"
        "    return Greeter()\n",
        encoding="utf-8",
    )
    (repo / "app" / "main.py").write_text(
        "from app.greeter import default_greeter\n"
        "def main():\n"
        "    print(default_greeter().greet('world'))\n",
        encoding="utf-8",
    )
    (repo / "app" / "notes.ts").write_text(
        "export class Notes {\n"
        "  list() {\n"
        "    return [];\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("not source", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
    return repo


async def _seed_workspace(db_session: AsyncSession, repo_path: str) -> Workspace:
    user = User(email="indexer@example.com")
    db_session.add(user)
    await db_session.flush()
    workspace = Workspace(owner_id=user.id, name="index-repo", repo_path=repo_path)
    db_session.add(workspace)
    await db_session.flush()
    session = Session(workspace_id=workspace.id, user_id=user.id)
    db_session.add(session)
    await db_session.commit()
    return workspace


async def test_indexer_persists_and_searches(
    container: Container,
    db_session: AsyncSession,
    settings: Settings,
    tmp_path: Path,
) -> None:
    repo = _build_repo(tmp_path)
    workspace = await _seed_workspace(db_session, str(repo))

    summary = await RepositoryIndexer(db_session).index(workspace.id, repo)

    assert summary.files_indexed == 3
    assert summary.files_skipped == 0
    assert summary.chunks_created >= 3
    assert summary.symbols_indexed >= 6

    chunks = await CodeChunkRepository(db_session).list_for_workspace(workspace.id)
    assert {chunk.language for chunk in chunks} == {"python", "typescript"}
    assert {chunk.file_path for chunk in chunks} == {
        "app/greeter.py",
        "app/main.py",
        "app/notes.ts",
    }

    keyword = await CodeChunkRepository(db_session).keyword_search(workspace.id, "Hello")
    assert keyword
    assert keyword[0].file_path == "app/greeter.py"

    symbol = await CodeChunkRepository(db_session).symbol_search(workspace.id, "Greeter")
    assert symbol
    assert symbol[0].file_path == "app/greeter.py"

    qualified = await CodeChunkRepository(db_session).symbol_search(workspace.id, "Greeter.greet")
    assert qualified
    assert qualified[0].file_path == "app/greeter.py"

    files = await CodeChunkRepository(db_session).list_files(workspace.id)
    assert set(files) == {"app/greeter.py", "app/main.py", "app/notes.ts"}


async def test_reindex_is_idempotent(
    container: Container,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    repo = _build_repo(tmp_path)
    workspace = await _seed_workspace(db_session, str(repo))
    repository = CodeChunkRepository(db_session)

    first = await RepositoryIndexer(db_session).index(workspace.id, repo)
    first_count = await repository.count_for_workspace(workspace.id)

    second = await RepositoryIndexer(db_session).index(workspace.id, repo)
    second_count = await repository.count_for_workspace(workspace.id)

    assert first.chunks_created == second.chunks_created
    assert first_count == first.chunks_created
    assert second_count == second.chunks_created
    assert await repository.count_for_workspace(workspace.id) > 0


async def test_context_assembler_retrieves_relevant_chunks(
    container: Container,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    repo = _build_repo(tmp_path)
    workspace = await _seed_workspace(db_session, str(repo))
    await RepositoryIndexer(db_session).index(workspace.id, repo)

    assembler = ContextAssembler.from_session(
        db_session,
        max_chunks=10,
        max_chars=12_000,
    )

    window = await assembler.assemble(workspace.id, "fix the greet method in Greeter")
    assert window.items
    assert window.items[0].file_path == "app/greeter.py"
    assert window.items[0].start_line >= 3
    assert "greet" in window.items[0].content
    assert window.total_chars == sum(item.chars for item in window.items)

    qualified = await assembler.assemble(workspace.id, "update app.notes.Notes.list")
    assert qualified.items
    assert qualified.items[0].file_path == "app/notes.ts"
    assert "Notes" in qualified.items[0].content

    none = await assembler.assemble(workspace.id, "no such thing here at all")
    assert none.items == []
    assert none.total_chars == 0
    assert none.truncated is False
