"""CLI context: workspace state persistence and the in-process agent core.

The CLI records which workspace and session it is bound to in a
``.engineer/state.json`` file at the repository root, mirroring ``.git``. The
:class:`CliContext` lazily wires the application :class:`Container` (and, for
real runs, an :class:`Orchestrator`) so commands share one engine and release
it on exit.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from rich.console import Console

from app.core.config import Settings, get_settings
from app.core.container import Container
from app.database.models.user import User
from app.database.repositories.user import UserRepository
from app.executor.git import run_git
from app.orchestrator.orchestrator import Orchestrator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agents.base import TokenHandler
    from app.orchestrator.orchestrator import TaskEventHandler

STATE_DIR_NAME = ".engineer"
STATE_FILE_NAME = "state.json"
CLI_USER_EMAIL = "cli@local"

LLM_UNCONFIGURED_HINT = (
    "LLM is not configured. Set LLM_PROVIDER, LLM_API_KEY, and/or LLM_BASE_URL "
    "in your environment or .env file."
)


class CliError(Exception):
    """A user-facing CLI failure with a friendly message."""


class WorkspaceState(BaseModel):
    """Persisted CLI binding to a workspace and session.

    Attributes:
        repo_path: Absolute path of the bound repository checkout.
        workspace_id: The workspace row the CLI operates on.
        session_id: The session the CLI runs tasks in.
        created_at: When the binding was created.
    """

    repo_path: str
    workspace_id: uuid.UUID
    session_id: uuid.UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def state_file_for(repo: Path) -> Path:
    """Return the path of the state file for ``repo``."""
    return repo / STATE_DIR_NAME / STATE_FILE_NAME


def load_state(repo: Path) -> WorkspaceState | None:
    """Load the workspace binding for ``repo``, or ``None`` when absent."""
    path = state_file_for(repo)
    if not path.is_file():
        return None
    try:
        return WorkspaceState.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CliError(f"cannot read {path}: {exc}") from exc


def save_state(repo: Path, state: WorkspaceState) -> Path:
    """Write the workspace binding atomically and return its path."""
    path = state_file_for(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(state.model_dump_json(indent=2))
        os.replace(tmp_name, path)
    except Exception:
        os.unlink(tmp_name)
        raise
    return path


async def find_repo_root(start: Path) -> Path:
    """Return the git repository root containing ``start``.

    Walks upward from ``start`` via ``git rev-parse`` so the CLI works from
    any subdirectory of a checkout.
    """
    try:
        out = await run_git(start, ["rev-parse", "--show-toplevel"])
    except FileNotFoundError as exc:
        raise CliError("git is required but was not found on PATH") from exc
    if out.exit_code != 0:
        raise CliError(f"not inside a git repository: {start}")
    return Path(out.stdout.strip())


async def ensure_cli_user(session: AsyncSession) -> User:
    """Return the shared local CLI user, creating it on first use.

    Auth is not wired yet, so ``engineer init`` binds workspaces to one
    local user; ownership checks land with the auth phase.
    """
    user = await UserRepository(session).get_by_email(CLI_USER_EMAIL)
    if user is None:
        user = User(email=CLI_USER_EMAIL, full_name="Local CLI user")
        session.add(user)
        await session.flush()
    return user


@dataclass
class CliContext:
    """Everything a CLI command needs, wired lazily.

    Attributes:
        console: The rich console all output goes to (injectable in tests).
        settings: Application settings.
        container: Lazily built application container; ``None`` until first
            use so pure-git commands never touch PostgreSQL/Redis.
        orchestrator: Optional pre-built orchestrator for tests (e.g. backed
            by a FakeLLM and a stub executor); when set, ``make_orchestrator``
            returns it instead of building a real one.
    """

    console: Console
    settings: Settings
    container: Container | None = None
    orchestrator: Orchestrator | None = None

    def require_container(self) -> Container:
        """Return the shared container, building it on first use."""
        if self.container is None:
            self.container = Container.build(self.settings)
        return self.container

    def db_session(self) -> AsyncSession:
        """Return a fresh ``AsyncSession`` context manager for one command."""
        return self.require_container().session_factory()

    def make_orchestrator(
        self,
        *,
        on_event: TaskEventHandler | None = None,
        on_token: TokenHandler | None = None,
        stream: bool = False,
    ) -> Orchestrator:
        """Return an orchestrator for running a task.

        A pre-injected orchestrator (tests) wins; otherwise one is built over
        the shared container's session factory, broker, and cancellation
        registry. An unconfigured LLM surfaces as a friendly :class:`CliError`
        rather than a raw traceback.
        """
        if self.orchestrator is not None:
            return self.orchestrator
        container = self.require_container()
        from app.llm.factory import build_llm_client

        try:
            llm = build_llm_client(self.settings)
        except Exception as exc:
            raise CliError(LLM_UNCONFIGURED_HINT) from exc
        return Orchestrator(
            session_factory=container.session_factory,
            llm=llm,
            settings=self.settings,
            event_broker=container.event_broker,
            cancellations=container.cancellations,
            on_event=on_event,
            on_token=on_token,
            stream=stream,
        )

    async def aclose(self) -> None:
        """Release container resources. Idempotent."""
        if self.container is not None:
            await self.container.aclose()
            self.container = None


def make_context(
    *,
    settings: Settings | None = None,
    console: Console | None = None,
    container: Container | None = None,
    orchestrator: Orchestrator | None = None,
) -> CliContext:
    """Build a :class:`CliContext`, defaulting settings to the cached values."""
    return CliContext(
        console=console or Console(highlight=False),
        settings=settings or get_settings(),
        container=container,
        orchestrator=orchestrator,
    )
