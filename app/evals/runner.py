"""Headless benchmark runner.

The :class:`EvalRunner` drives one benchmark task end to end: it scaffolds
the fixture repository, provisions a database workspace/session/task, runs
the goal through the orchestrator, and grades the outcome by running the
task's test command against the resulting tree. Everything is injected so
unit tests run without PostgreSQL, Docker, or a real LLM.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.database.models.session import Session
from app.database.models.task import Task
from app.database.models.user import User
from app.database.models.workspace import Workspace
from app.database.repositories.user import UserRepository
from app.evals.results import EvalResultRecord, ResultStore, truncate_tail, utcnow
from app.evals.tasks import EvalTask, scaffold
from app.executor.executor import ToolExecutor
from app.executor.git import GitOutput, run_git
from app.executor.test_parser import TestReport
from app.llm.protocol import LLMProvider
from app.orchestrator.orchestrator import Orchestrator
from app.tools.schemas import ToolCall, ToolName

_CLI_USER_EMAIL = "cli@local"


@dataclass(frozen=True)
class EvalProvision:
    """Database identity backing one benchmark run.

    Attributes:
        workspace_id: The workspace row the run is bound to.
        session_id: The session row tasks are recorded against.
        task_id: The task row the orchestrator drives.
    """

    workspace_id: uuid.UUID
    session_id: uuid.UUID
    task_id: uuid.UUID


class OrchestratorLike(Protocol):
    """Minimal orchestrator surface the runner depends on."""

    async def run_task(self, task_id: uuid.UUID) -> Task: ...


class EvalVerifier(Protocol):
    """Grades a scaffolded workspace after the agent run."""

    async def verify(self, task: EvalTask, workspace_dir: Path) -> TestReport: ...


class SandboxVerifier:
    """Default verifier: runs the task's test command through the sandbox.

    This mirrors exactly what the agent's ``test_run`` tool sees, so a pass
    here means the tests genuinely pass in the same environment.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def verify(self, task: EvalTask, workspace_dir: Path) -> TestReport:
        executor = ToolExecutor.build(workspace_dir=workspace_dir, settings=self._settings)
        try:
            result = await executor.execute(
                ToolCall(
                    tool=ToolName.TEST_RUN,
                    arguments={
                        "command": task.test_command,
                        "framework": "generic",
                    },
                )
            )
            payload = result.data.get("report")
            if payload:
                return TestReport.from_dict(payload)
            return TestReport(
                framework="generic",
                command=task.test_command,
                failed=0 if result.ok else 1,
                output=result.output,
                exit_code=result.exit_code,
            )
        finally:
            await executor.sandboxes.close()


async def _run_git_checked(workspace_dir: Path, args: list[str]) -> GitOutput:
    return await run_git(workspace_dir, args)


async def _init_git(workspace_dir: Path) -> None:
    """Initialize a git repository with one commit so git tools work."""
    identity = ["-c", "user.email=eval@local", "-c", "user.name=eval"]
    for args in (
        [*identity, "init", "-q"],
        ["add", "-A"],
        [*identity, "commit", "-q", "-m", "initial"],
    ):
        await _run_git_checked(workspace_dir, args)


class EvalRunner:
    """Runs a single benchmark task and records the outcome.

    Args:
        session_factory: Database session factory for provisioning.
        llm: LLM the orchestrator drives the goal with.
        settings: Application settings (sandbox, timeouts, model identity).
        timeout_seconds: Wall-clock cap per run; overrides the task default.
        provisioner: Optional async ``(task, workspace_dir) -> EvalProvision``
            used instead of real database provisioning (tests).
        orchestrator_factory: Optional ``(EvalProvision) -> OrchestratorLike``
            used instead of a real :class:`Orchestrator` (tests).
        verify_factory: Optional ``(Settings) -> EvalVerifier`` used instead
            of the sandbox verifier (tests).
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        llm: LLMProvider,
        settings: Settings,
        timeout_seconds: int | None = None,
        provisioner: Callable[[EvalTask, Path], Awaitable[EvalProvision]] | None = None,
        orchestrator_factory: Callable[[EvalProvision], OrchestratorLike] | None = None,
        verify_factory: Callable[[Settings], EvalVerifier] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._llm = llm
        self._settings = settings
        self._timeout_seconds = timeout_seconds
        self._provisioner = provisioner or self._provision
        self._orchestrator_factory = orchestrator_factory or self._make_orchestrator
        self._verify_factory = verify_factory or (lambda settings: SandboxVerifier(settings))

    async def run(
        self,
        task: EvalTask,
        workspace_dir: Path,
        *,
        store: ResultStore | None = None,
    ) -> EvalResultRecord:
        """Run ``task`` in ``workspace_dir`` and return the recorded outcome.

        The workspace is scaffolded with the task's fixtures and turned into
        a git repository, then the goal is run through the orchestrator, and
        finally the test command is executed to grade the result. When ``store``
        is given the record is appended before being returned.
        """
        started = utcnow()
        scaffold(task, workspace_dir)
        await _init_git(workspace_dir)

        provision = await self._provisioner(task, workspace_dir)
        orchestrator = self._orchestrator_factory(provision)
        timeout = self._timeout_seconds or task.timeout_seconds

        task_status = "errored"
        attempts = 0
        tokens = 0
        error: str | None = None
        try:
            final = await asyncio.wait_for(
                orchestrator.run_task(provision.task_id), timeout=timeout
            )
            task_status = final.status.value
            attempts = final.attempt
            tokens = (final.input_tokens or 0) + (final.output_tokens or 0)
            if final.error:
                error = final.error
        except TimeoutError:
            error = f"timed out after {timeout}s"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        report = None
        if task_status == "completed" and error is None:
            verifier = self._verify_factory(self._settings)
            try:
                report = await verifier.verify(task, workspace_dir)
            except Exception as exc:
                error = f"verification failed: {type(exc).__name__}: {exc}"

        tests_passed = report is not None and report.ok and not report.timed_out
        if error is not None:
            summary = error
        elif report is not None:
            summary = f"{report.passed} passed, {report.failed} failed"
        else:
            summary = "task did not complete"

        finished = utcnow()
        record = EvalResultRecord(
            task_id=task.id,
            task_name=task.name,
            category=task.category,
            model=self._settings.llm_model,
            provider=self._settings.llm_provider,
            passed=task_status == "completed" and tests_passed,
            task_status=task_status,
            tests_passed=tests_passed,
            test_summary=summary,
            output_tail=truncate_tail(report.output if report else ""),
            attempts=attempts,
            tokens=tokens,
            started_at=started,
            finished_at=finished,
            duration_seconds=(finished - started).total_seconds(),
        )
        if store is not None:
            store.append(record)
        return record

    async def _provision(self, task: EvalTask, workspace_dir: Path) -> EvalProvision:
        """Create the workspace/session/task rows the run is bound to."""
        async with self._session_factory() as session:
            user = await _ensure_cli_user(session)
            workspace = Workspace(
                owner_id=user.id,
                name=f"eval:{task.id}",
                repo_path=str(workspace_dir),
            )
            session.add(workspace)
            await session.flush()
            agent_session = Session(
                workspace_id=workspace.id,
                user_id=user.id,
                title=f"eval:{task.id}",
            )
            session.add(agent_session)
            await session.flush()
            task_row = Task(
                session_id=agent_session.id,
                agent_type="coder",
                goal=task.goal,
                max_attempts=1,
            )
            session.add(task_row)
            await session.commit()
            return EvalProvision(
                workspace_id=workspace.id,
                session_id=agent_session.id,
                task_id=task_row.id,
            )

    def _make_orchestrator(self, provision: EvalProvision) -> Orchestrator:
        """Build a real orchestrator over the configured session factory."""
        return Orchestrator(
            session_factory=self._session_factory,
            llm=self._llm,
            settings=self._settings,
        )


async def _ensure_cli_user(session: AsyncSession) -> User:
    """Return the shared local CLI user, creating it on first use."""
    user = await UserRepository(session).get_by_email(_CLI_USER_EMAIL)
    if user is None:
        user = User(email=_CLI_USER_EMAIL, full_name="Local CLI user")
        session.add(user)
        await session.flush()
    return user
