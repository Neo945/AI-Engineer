"""Command implementations for the engineer CLI.

Each command is a thin adapter over the shared agent core (repositories,
orchestrator, git runner). They are plain async functions taking a
:class:`CliContext` plus explicit parameters so they are easy to test without
a TTY.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table

from app.agents.base import DEFAULT_MAX_STEPS, LoopAgent
from app.agents.pipeline import REVIEWER_PROMPT
from app.agents.planning import TaskPlan, format_plan
from app.agents.repair import RepairAgent
from app.cli.context import (
    LLM_UNCONFIGURED_HINT,
    CliContext,
    CliError,
    WorkspaceState,
    ensure_cli_user,
    save_state,
)
from app.database.models.enums import TaskStatus
from app.database.models.session import Session
from app.database.models.task import Task
from app.database.models.workspace import Workspace
from app.database.repositories.task import TaskRepository
from app.database.repositories.workspace import WorkspaceRepository
from app.executor.executor import ToolExecutor, _detect_test_command
from app.executor.git import GitOutput, run_git
from app.executor.test_parser import TestReport, format_report
from app.llm.factory import build_llm_client
from app.llm.messages import ChatMessage, ChatRole
from app.llm.protocol import LLMProvider
from app.orchestrator.orchestrator import Orchestrator, OrchestratorEvent
from app.retrieval.indexer import RepositoryIndexer
from app.tools.schemas import ToolCall, ToolName

_TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})
_TERMINAL_EVENT_KINDS = frozenset({"completed", "failed", "cancelled"})
_STREAM_KEEPALIVE_SECONDS = 15.0
_MAX_SNIPPET = 300


def _short(text: str, limit: int = 60) -> str:
    """Truncate a single-line string to ``limit`` characters."""
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _snippet(text: str) -> str:
    text = text.strip()
    if len(text) <= _MAX_SNIPPET:
        return text
    return text[: _MAX_SNIPPET - 3] + "..."


async def _git(
    ctx: CliContext,
    repo: Path,
    args: list[str],
    *,
    stdin_data: str | None = None,
) -> GitOutput:
    try:
        return await run_git(repo, args, stdin_data=stdin_data)
    except FileNotFoundError as exc:
        raise CliError("git is required but was not found on PATH") from exc


def _build_llm(ctx: CliContext) -> LLMProvider:
    """Build the configured LLM client, or raise a friendly error."""
    try:
        return build_llm_client(ctx.settings)
    except Exception as exc:
        raise CliError(LLM_UNCONFIGURED_HINT) from exc


def _build_executor(ctx: CliContext, repo: Path) -> ToolExecutor:
    """Build a sandbox executor bound to ``repo`` from settings."""
    return ToolExecutor.build(workspace_dir=repo, settings=ctx.settings)


def _resolve_test_run(
    repo: Path,
    command: str | None,
    framework: str | None,
) -> tuple[str, str]:
    """Resolve the test command and runner family, auto-detecting omitted ones."""
    detected_command, detected_framework = _detect_test_command(repo)
    return command or detected_command, framework or detected_framework


def _final_verdict(answer: str) -> str | None:
    """Return the token after the last ``VERDICT:`` line, if present."""
    for line in reversed(answer.strip().splitlines()):
        stripped = line.strip()
        if stripped.upper().startswith("VERDICT:"):
            token = stripped.split(":", 1)[1].strip().upper()
            return token
    return None


def _verdict_passed(answer: str) -> bool:
    return _final_verdict(answer) == "PASS"


async def cmd_init(ctx: CliContext, *, repo: Path, name: str | None) -> int:
    """Bind a git repository to a workspace + session and persist state."""
    repo = Path(os.path.realpath(repo))
    if not repo.is_dir():
        raise CliError(f"not a directory: {repo}")
    check = await _git(ctx, repo, ["rev-parse", "--is-inside-work-tree"])
    if check.exit_code != 0:
        raise CliError(f"{repo} is not a git repository; run `git init` first")

    async with ctx.db_session() as session:
        user = await ensure_cli_user(session)
        workspace = Workspace(
            owner_id=user.id,
            name=name or repo.name or "workspace",
            repo_path=str(repo),
        )
        session.add(workspace)
        await session.flush()
        agent_session = Session(workspace_id=workspace.id, user_id=user.id, title="default")
        session.add(agent_session)
        await session.commit()
        state = WorkspaceState(
            repo_path=str(repo),
            workspace_id=workspace.id,
            session_id=agent_session.id,
        )
    path = save_state(repo, state)
    ctx.console.print(
        Panel(
            f"[bold]{repo.name}[/]\n"
            f"workspace: {state.workspace_id}\n"
            f"session:   {state.session_id}\n"
            f"state:     {path}",
            title="engineer init",
            border_style="cyan",
        )
    )
    return 0


async def cmd_status(ctx: CliContext, *, repo: Path, state: WorkspaceState) -> int:
    """Show the bound workspace, branch, and recent task activity."""
    branch = await _git(ctx, repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch_name = branch.stdout.strip() if branch.exit_code == 0 else "(detached)"
    working = await _git(ctx, repo, ["status", "--short", "--branch"])
    dirty = any(not line.startswith("## ") for line in working.stdout.splitlines())
    working_state = "dirty" if dirty else "clean"

    async with ctx.db_session() as session:
        workspace = await WorkspaceRepository(session).get(state.workspace_id)
        tasks = await TaskRepository(session).list_by_session(state.session_id, limit=100)

    workspace_name = workspace.name if workspace is not None else repo.name
    lines = [
        ("Repository", repo.name),
        ("Branch", branch_name),
        ("Working tree", working_state),
        ("Workspace", workspace_name),
        ("Session", str(state.session_id)),
        ("Tasks", str(len(tasks))),
    ]
    if tasks:
        latest = tasks[-1]
        lines.append(("Last task", f"{latest.status.value} — {_short(latest.goal)}"))
    grid = Table.grid(padding=(0, 2))
    for key, value in lines:
        grid.add_row(f"[bold]{key}[/]", value)
    ctx.console.print(Panel(grid, title="engineer status", border_style="cyan"))
    return 0


async def cmd_index(
    ctx: CliContext,
    *,
    repo: Path,
    state: WorkspaceState,
    show_progress: bool = True,
) -> int:
    """Index the bound workspace's source files into ``code_chunks``.

    Re-indexing is idempotent: the workspace's prior index is replaced in
    the same transaction, so running ``engineer index`` repeatedly converges.
    """
    async with ctx.db_session() as session:
        summary = await RepositoryIndexer(session).index(
            workspace_id=state.workspace_id,
            repo_path=repo,
        )
    lines = [
        ("Repository", repo.name),
        ("Files indexed", str(summary.files_indexed)),
        ("Files skipped", str(summary.files_skipped)),
        ("Chunks created", str(summary.chunks_created)),
        ("Symbols indexed", str(summary.symbols_indexed)),
    ]
    if show_progress:
        grid = Table.grid(padding=(0, 2))
        for key, value in lines:
            grid.add_row(f"[bold]{key}[/]", value)
        ctx.console.print(Panel(grid, title="engineer index", border_style="cyan"))
    return 0


async def cmd_run(
    ctx: CliContext,
    *,
    repo: Path,
    state: WorkspaceState,
    goal: str,
    agent_type: str,
    yes: bool = False,
) -> int:
    """Plan a goal, gate it on human approval, then run it, streaming live.

    The planner produces a structured plan; if it writes files or touches
    destructive operations the run pauses until the plan is approved.
    ``yes`` approves automatically (non-interactive / scripted use).
    """
    goal = goal.strip()
    if not goal:
        raise CliError("goal is empty")
    if agent_type not in {"coder", "pipeline"}:
        raise CliError(f"unsupported agent_type: {agent_type}")

    async with ctx.db_session() as session:
        task = await TaskRepository(session).add(
            Task(
                session_id=state.session_id,
                agent_type=agent_type,
                goal=goal,
                max_attempts=ctx.settings.task_max_attempts,
            )
        )
        await session.commit()
        task_id = task.id

    ctx.console.print(f"[bold cyan]▶[/] {goal}  [dim]({agent_type})[/]")
    orchestrator = ctx.make_orchestrator()

    planned = await _plan_task_streaming(ctx, orchestrator, task_id)
    plan = TaskPlan.from_dict(planned.plan) if planned.plan is not None else None
    if plan is not None:
        ctx.console.print(Panel(format_plan(plan), title="plan", border_style="cyan"))
    if plan is not None and plan.needs_approval:
        if not yes and not Confirm.ask("Approve this plan and run the task?", default=False):
            ctx.console.print(f"[yellow]aborted[/] task {task_id} awaits approval")
            return 1
        await orchestrator.approve_task(task_id)
        ctx.console.print("[green]plan approved[/]")

    final = await _run_task_streaming(ctx, orchestrator, task_id)
    border = "green" if final.status == TaskStatus.COMPLETED else "red"
    ctx.console.print(
        Panel(
            f"status: [bold]{final.status.value}[/]\n"
            f"tokens: {final.input_tokens} in / {final.output_tokens} out\n"
            f"attempt: {final.attempt}",
            title=f"task {task_id}",
            border_style=border,
        )
    )
    return 0 if final.status == TaskStatus.COMPLETED else 1


async def _plan_task_streaming(
    ctx: CliContext,
    orchestrator: Orchestrator,
    task_id: uuid.UUID,
) -> Task:
    """Run the planner while rendering its broker events as they arrive."""
    container = ctx.container
    if container is None:
        return await orchestrator.plan_task(task_id)

    queue = await container.event_broker.subscribe(task_id)

    async def _render() -> None:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_STREAM_KEEPALIVE_SECONDS)
                except TimeoutError:
                    continue
                render_event(ctx.console, event)
                if event.kind == "planned":
                    return
        finally:
            await container.event_broker.unsubscribe(task_id, queue)

    render_task = asyncio.create_task(_render())
    try:
        result = await orchestrator.plan_task(task_id)
    except BaseException:
        render_task.cancel()
        await asyncio.gather(render_task, return_exceptions=True)
        raise
    await render_task
    return result


async def _run_task_streaming(
    ctx: CliContext,
    orchestrator: Orchestrator,
    task_id: uuid.UUID,
) -> Task:
    """Run ``task_id`` while rendering its broker events as they arrive."""
    container = ctx.container
    if container is None:
        return await orchestrator.run_task(task_id)

    queue = await container.event_broker.subscribe(task_id)

    async def _render() -> None:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_STREAM_KEEPALIVE_SECONDS)
                except TimeoutError:
                    continue
                render_event(ctx.console, event)
                if event.kind in _TERMINAL_EVENT_KINDS:
                    return
        finally:
            await container.event_broker.unsubscribe(task_id, queue)

    render_task = asyncio.create_task(_render())
    try:
        result = await orchestrator.run_task(task_id)
    except BaseException:
        render_task.cancel()
        await asyncio.gather(render_task, return_exceptions=True)
        raise
    await render_task
    return result


async def cmd_tasks(ctx: CliContext, *, repo: Path, state: WorkspaceState, limit: int) -> int:
    """List the bound session's tasks, newest first."""
    async with ctx.db_session() as session:
        tasks = await TaskRepository(session).list_by_session(state.session_id, limit=limit)
    if not tasks:
        ctx.console.print("[dim]no tasks yet[/]")
        return 0
    table = Table(title="tasks")
    table.add_column("id", style="dim")
    table.add_column("status")
    table.add_column("agent")
    table.add_column("goal")
    table.add_column("attempt")
    table.add_column("tokens")
    for task in reversed(tasks):
        table.add_row(
            str(task.id)[:8],
            task.status.value,
            task.agent_type,
            _short(task.goal, 50),
            f"{task.attempt}/{task.max_attempts}",
            str(task.input_tokens + task.output_tokens),
        )
    ctx.console.print(table)
    return 0


async def cmd_diff(
    ctx: CliContext,
    *,
    repo: Path,
    state: WorkspaceState | None,
    ref: str | None = None,
) -> int:
    """Show the working-tree diff (optionally against a revision)."""
    stat_args = ["diff", "--stat"] if ref is None else ["diff", "--stat", ref]
    body_args = ["diff", "--no-color"] if ref is None else ["diff", "--no-color", ref]
    stat = await _git(ctx, repo, stat_args)
    if stat.exit_code != 0:
        raise CliError(stat.stderr.strip() or "git diff --stat failed")
    body = await _git(ctx, repo, body_args)
    if body.exit_code != 0:
        raise CliError(body.stderr.strip() or "git diff failed")
    if not body.stdout.strip():
        ctx.console.print("[dim]no changes[/]")
        return 0
    ctx.console.print(stat.stdout.strip())
    ctx.console.print(Syntax(body.stdout, "diff"))
    return 0


async def cmd_commit(
    ctx: CliContext,
    *,
    repo: Path,
    state: WorkspaceState | None,
    message: str | None,
    yes: bool,
) -> int:
    """Stage the working tree and create a commit after confirmation."""
    stat = await _git(ctx, repo, ["diff", "--stat"])
    if stat.exit_code != 0:
        raise CliError(stat.stderr.strip() or "git diff --stat failed")
    if message is None:
        message = ctx.console.input("commit message: ").strip()
        if not message:
            raise CliError("empty commit message")
    if not yes:
        if stat.stdout.strip():
            ctx.console.print(stat.stdout.strip())
        if not Confirm.ask("Commit these changes?", default=False):
            ctx.console.print("aborted")
            return 1

    staged = await _git(ctx, repo, ["add", "-A"])
    if staged.exit_code != 0:
        raise CliError(staged.stderr.strip() or "git add failed")
    done = await _git(ctx, repo, ["commit", "-F", "-"], stdin_data=message)
    if done.exit_code != 0:
        raise CliError(done.stderr.strip() or "git commit failed")
    ctx.console.print(f"[green]committed[/] {done.stdout.strip()}")
    return 0


async def cmd_cancel(ctx: CliContext, *, repo: Path, state: WorkspaceState, task_id: str) -> int:
    """Cancel a pending or running task belonging to the bound session."""
    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError as exc:
        raise CliError(f"invalid task id: {task_id}") from exc
    async with ctx.db_session() as session:
        task = await TaskRepository(session).get(task_uuid)
        if task is None:
            raise CliError("task not found")
        if task.session_id != state.session_id:
            raise CliError("task does not belong to this session")
        if task.status in _TERMINAL_STATUSES:
            raise CliError(f"task already finished ({task.status.value})")
        task.status = TaskStatus.CANCELLED
        task.finished_at = datetime.now(UTC)
        await session.commit()
    await ctx.require_container().cancellations.request_cancel(task_uuid)
    ctx.console.print(f"[yellow]cancelled[/] {task_uuid}")
    return 0


async def cmd_review(
    ctx: CliContext,
    *,
    repo: Path,
    state: WorkspaceState | None,
    ref: str | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    llm: LLMProvider | None = None,
    executor: ToolExecutor | None = None,
) -> int:
    """Run a structured code review of the working tree (or a diff).

    The reviewer inspects the changes with read-only tools (git status, git
    diff, file_read) and ends with ``VERDICT: PASS`` or
    ``VERDICT: CHANGES_NEEDED`` plus actionable feedback. Exit code 0 means
    PASS; any other outcome exits 1.
    """
    if llm is None:
        llm = _build_llm(ctx)
    if executor is None:
        executor = _build_executor(ctx, repo)
    try:
        target = "the working tree" if ref is None else f"the diff against {ref}"
        seed = ChatMessage(
            role=ChatRole.USER,
            content=(
                f"Review {target} in this repository. Use the git status and "
                "git diff tools (and file_read) to inspect the changes. Report "
                "your findings, then end with exactly one line first: "
                "'VERDICT: PASS' or 'VERDICT: CHANGES_NEEDED' followed by "
                "actionable feedback."
            ),
        )
        agent = LoopAgent(
            llm=llm,
            executor=executor,
            system_prompt=REVIEWER_PROMPT,
            max_steps=max_steps,
            max_tokens=ctx.settings.llm_max_tokens,
            temperature=ctx.settings.llm_temperature,
        )
        result = await agent.run_from([seed])
        ctx.console.print(result.answer)
        return 0 if _verdict_passed(result.answer) else 1
    finally:
        await executor.sandboxes.close()


async def cmd_test(
    ctx: CliContext,
    *,
    repo: Path,
    state: WorkspaceState | None,
    command: str | None = None,
    framework: str | None = None,
    fix: bool = False,
    repairs: int | None = None,
    llm: LLMProvider | None = None,
    executor: ToolExecutor | None = None,
) -> int:
    """Run the project's test suite in the sandbox, optionally fixing failures.

    Without ``fix`` the suite runs once and the structured report is printed.
    With ``fix`` a test-and-repair loop fixes failing tests and re-runs them,
    bounded by ``repairs`` (defaults to ``test_max_repairs``). Exit code 0
    means all tests passed.
    """
    if executor is None:
        executor = _build_executor(ctx, repo)
    try:
        if fix:
            if llm is None:
                llm = _build_llm(ctx)
            max_repairs = repairs if repairs is not None else ctx.settings.test_max_repairs
            agent = RepairAgent(
                llm=llm,
                executor=executor,
                max_repairs=max_repairs,
                test_command=command,
                test_framework=framework,
            )
            repair = await agent.run(
                "Run the project's test suite and fix any failing tests."
            )
            ctx.console.print(repair.answer)
            return 0 if _verdict_passed(repair.answer) else 1

        resolved_command, resolved_framework = _resolve_test_run(repo, command, framework)
        tool_result = await executor.execute(
            ToolCall(
                tool=ToolName.TEST_RUN,
                arguments={
                    "command": resolved_command,
                    "framework": resolved_framework,
                },
            )
        )
        payload = tool_result.data.get("report")
        report = (
            TestReport.from_dict(payload)
            if payload
            else TestReport(
                framework=resolved_framework,
                command=resolved_command,
                failed=0 if tool_result.ok else 1,
                output=tool_result.output,
            )
        )
        ctx.console.print(format_report(report))
        return 0 if report.ok else 1
    finally:
        await executor.sandboxes.close()


def render_event(console: Console, event: OrchestratorEvent) -> None:
    """Render one orchestrator event as a line of CLI output."""
    if event.kind == "started":
        console.print(f"[bold cyan]started[/] {event.detail or ''}")
    elif event.kind == "message":
        _render_message(console, event.message)
    elif event.kind == "planned":
        console.print("[bold cyan]planned[/]")
    elif event.kind == "approved":
        console.print("[bold green]plan approved[/]")
    elif event.kind == "rejected":
        console.print("[bold yellow]plan rejected[/]")
    elif event.kind == "completed":
        console.print("\n[bold green]completed[/]")
    elif event.kind == "failed":
        console.print(f"\n[bold red]failed[/] {event.detail or ''}")
    elif event.kind == "cancelled":
        console.print("\n[bold yellow]cancelled[/]")


def _render_message(console: Console, message: ChatMessage | None) -> None:
    if message is None:
        return
    if message.role == ChatRole.USER:
        console.print(f"[cyan]▶[/] {message.content}")
    elif message.role == ChatRole.ASSISTANT:
        if message.tool_requests:
            if message.content:
                console.print(f"[green]◆[/] {message.content}")
            for request in message.tool_requests:
                console.print(f"  [yellow]→ {request.name} {json.dumps(request.arguments)}[/]")
        else:
            console.print(f"[green]◆[/] {message.content}")
    elif message.role == ChatRole.TOOL:
        console.print(f"  [yellow]←[/] {_snippet(message.content)}")
