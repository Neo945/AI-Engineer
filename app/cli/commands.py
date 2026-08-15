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
import shutil
import tempfile
import uuid
from collections.abc import Sequence
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
from app.database.models.memory import MemoryEntry
from app.database.models.session import Session
from app.database.models.task import Task
from app.database.models.workspace import Workspace
from app.database.repositories.task import TaskRepository
from app.database.repositories.workspace import WorkspaceRepository
from app.evals import (
    BENCHMARK_TASKS,
    EvalResultRecord,
    EvalRunner,
    ResultStore,
    summarize,
    task_by_id,
)
from app.executor.executor import ToolExecutor, _detect_test_command
from app.executor.git import GitOutput, run_git
from app.executor.test_parser import TestReport, format_report
from app.git.commit import generate_commit_message
from app.git.pr import PRDescription, generate_pr_description, render_pr
from app.llm.factory import build_llm_client
from app.llm.messages import ChatMessage, ChatRole
from app.llm.protocol import LLMProvider
from app.memory.service import MemoryService
from app.orchestrator.orchestrator import Orchestrator, OrchestratorEvent
from app.retrieval.indexer import RepositoryIndexer
from app.review import ReviewFinding, extract_verdict, parse_report, sort_findings
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


def _llm_failure_hint(exc: Exception) -> str:
    detail = _short(str(exc).replace("\n", " ") or type(exc).__name__, 180)
    return f"the model request failed: {detail}"


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
        from app.monitoring.tracing import InstrumentedLLM

        return InstrumentedLLM(build_llm_client(ctx.settings))
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
    """Return the token from the first ``VERDICT:`` line, if present."""
    return extract_verdict(answer)


async def _reask_verdict(
    agent: LoopAgent,
    seed: ChatMessage,
    answer: str,
) -> str:
    """Re-ask the reviewer for a verdict when it omitted one.

    The follow-up seeds the same conversation plus the assistant's prior
    answer and a terse instruction to restate the verdict, so the loop can
    finish with a parseable ``VERDICT:`` line instead of silently exiting 1.
    """
    reask = ChatMessage(
        role=ChatRole.USER,
        content=(
            "Your review above did not state a verdict. Reply now with "
            "exactly one line first: 'VERDICT: PASS' or 'VERDICT: "
            "CHANGES_NEEDED', then one short sentence of feedback."
        ),
    )
    retry = await agent.run_from(
        [
            seed,
            ChatMessage(role=ChatRole.ASSISTANT, content=answer),
            reask,
        ]
    )
    return retry.answer


def _verdict_passed(answer: str) -> bool:
    return _final_verdict(answer) == "PASS"


class _TokenSink:
    """Render streamed tokens live, closing the line on the next event.

    The first delta of an assistant turn prints a ``◆`` prefix; subsequent
    deltas append without newlines so the console fills in as the model
    generates. ``finish`` returns whether a turn was active so callers can
    avoid re-printing content that was already streamed.
    """

    def __init__(self, console: Console) -> None:
        self._console = console
        self._active = False

    def feed(self, text: str) -> None:
        if not text:
            return
        if not self._active:
            self._console.print("[green]◆[/] ", end="")
            self._active = True
        self._console.print(text, end="")

    def finish(self) -> bool:
        active = self._active
        if active:
            self._console.print()
            self._active = False
        return active


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
    sink = _TokenSink(ctx.console)
    orchestrator = ctx.make_orchestrator(
        on_token=sink.feed,
        stream=ctx.settings.cli_stream_tokens,
    )

    planned = await _plan_task_streaming(ctx, orchestrator, task_id, sink)
    plan = TaskPlan.from_dict(planned.plan) if planned.plan is not None else None
    if plan is not None:
        ctx.console.print(Panel(format_plan(plan), title="plan", border_style="cyan"))
    if plan is not None and plan.needs_approval:
        if not yes and not Confirm.ask("Approve this plan and run the task?", default=False):
            ctx.console.print(f"[yellow]aborted[/] task {task_id} awaits approval")
            return 1
        await orchestrator.approve_task(task_id)
        ctx.console.print("[green]plan approved[/]")

    final = await _run_task_streaming(ctx, orchestrator, task_id, sink)
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
    sink: _TokenSink | None = None,
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
                render_event(ctx.console, event, sink)
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
    sink: _TokenSink | None = None,
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
                render_event(ctx.console, event, sink)
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
    generate: bool = False,
    llm: LLMProvider | None = None,
) -> int:
    """Stage the working tree and create a commit after confirmation.

    With ``generate`` the commit message is drafted by the LLM from the
    working-tree diff (falling back to a prompt when the model is
    unconfigured); an explicit ``message`` always wins.
    """
    stat = await _git(ctx, repo, ["diff", "--stat"])
    if stat.exit_code != 0:
        raise CliError(stat.stderr.strip() or "git diff --stat failed")
    llm_owned = False
    if message is None and generate:
        if llm is None:
            try:
                llm = _build_llm(ctx)
                llm_owned = True
            except CliError:
                llm = None
        if llm is not None:
            diff = await _git(ctx, repo, ["diff", "--no-color", "HEAD"])
            if diff.exit_code != 0:
                raise CliError(diff.stderr.strip() or "git diff failed")
            try:
                message = await generate_commit_message(llm, diff=diff.stdout)
            except Exception as exc:
                raise CliError(_llm_failure_hint(exc)) from exc
    if message is None:
        message = ctx.console.input("commit message: ").strip()
        if not message:
            raise CliError("empty commit message")
    if not yes:
        if stat.stdout.strip():
            ctx.console.print(stat.stdout.strip())
        if message is not None and generate and llm is not None:
            ctx.console.print(f"[dim]message:[/] {_short(message, 160)}")
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
    if llm_owned:
        assert llm is not None
        await llm.close()
    return 0


async def cmd_pr(
    ctx: CliContext,
    *,
    repo: Path,
    state: WorkspaceState | None,
    base: str | None = None,
    branch: str | None = None,
    remote: str = "origin",
    draft: bool = False,
    yes: bool = False,
    title: str | None = None,
    llm: LLMProvider | None = None,
) -> int:
    """Prepare a pull request for the current branch and try to open it.

    Generates a title/body from the committed diff (LLM-drafted when
    configured), pushes the branch to ``remote``, and opens the PR with
    ``gh`` when available. The description is always saved under
    ``.engineer/pr-<branch>.md`` so it can be opened manually.
    """
    base_ref = await _default_branch(ctx, repo) if base is None else base
    current = await _git(ctx, repo, ["symbolic-ref", "--short", "HEAD"])
    if current.exit_code != 0:
        raise CliError(current.stderr.strip() or "not on a branch")
    branch_name = current.stdout.strip()
    if branch is not None:
        if branch != branch_name:
            raise CliError(
                f"branch {branch!r} is not checked out; run `engineer git checkout "
                f"{branch}` or omit --branch"
            )
        branch_name = branch
    if branch_name == base_ref:
        raise CliError(
            f"on the base branch {base_ref!r}; create a feature branch first (`--branch <name>`)"
        )

    log = await _git(ctx, repo, ["log", "--oneline", f"{base_ref}..HEAD"])
    if log.exit_code != 0:
        raise CliError(log.stderr.strip() or f"cannot compare {branch_name} against {base_ref}")
    commit_lines = [line for line in log.stdout.splitlines() if line.strip()]
    if not commit_lines:
        raise CliError(f"no commits on {branch_name} beyond {base_ref}")

    diff = await _git(ctx, repo, ["diff", "--no-color", f"{base_ref}...HEAD"])
    if diff.exit_code != 0:
        raise CliError(diff.stderr.strip() or f"cannot diff {branch_name} against {base_ref}")

    description = await _pr_description(
        ctx,
        llm,
        diff=diff.stdout,
        commits=commit_lines,
        base=base_ref,
        branch=branch_name,
        title=title,
    )
    body = render_pr(description)
    title = f"[bold]{description.title}[/]"
    ctx.console.print(Panel(title, title="pull request", border_style="cyan"))
    if body:
        ctx.console.print(Panel(body, border_style="dim"))
    if not yes and not Confirm.ask("Open this pull request?", default=False):
        ctx.console.print("aborted")
        return 1

    remotes = await _git(ctx, repo, ["remote"])
    if remotes.exit_code != 0:
        raise CliError(remotes.stderr.strip() or "git remote failed")
    if remotes.stdout.strip():
        pushed = await _git(ctx, repo, ["push", "-u", remote, branch_name])
        if pushed.exit_code != 0:
            ctx.console.print(f"[yellow]push failed[/] {pushed.stderr.strip()}")
        else:
            ctx.console.print(f"[green]pushed[/] {remote}/{branch_name}")
    else:
        ctx.console.print("[yellow]no git remote configured; skipping push[/]")

    pr_body_path = repo / ".engineer" / f"pr-{branch_name}.md"
    pr_body_path.parent.mkdir(parents=True, exist_ok=True)
    pr_body_path.write_text(f"# {description.title}\n\n{body}\n", encoding="utf-8")

    created = await _gh_pr_create(ctx, repo, base_ref, branch_name, description, draft=draft)
    if created:
        ctx.console.print("[green]pull request opened[/]")
        return 0
    ctx.console.print(f"[dim]PR body saved to {pr_body_path}[/]")
    ctx.console.print(
        f"[dim]open it manually: gh pr create --base {base_ref} --head {branch_name} "
        f"--title {description.title!r} --body-file {pr_body_path}[/]"
    )
    return 0


async def _default_branch(ctx: CliContext, repo: Path) -> str:
    """Return the repository's default branch (remote HEAD, then main/master)."""
    head = await _git(ctx, repo, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if head.exit_code == 0 and head.stdout.strip():
        candidate = head.stdout.strip()
        return candidate.split("/", 1)[1] if candidate.startswith("origin/") else candidate
    for candidate in ("main", "master"):
        check = await _git(ctx, repo, ["rev-parse", "--verify", "--quiet", candidate])
        if check.exit_code == 0:
            return candidate
    raise CliError("cannot determine the default branch (expected 'main' or 'master')")


async def _pr_description(
    ctx: CliContext,
    llm: LLMProvider | None,
    *,
    diff: str,
    commits: list[str],
    base: str,
    branch: str,
    title: str | None,
) -> PRDescription:
    """Generate the PR description, falling back to the commit list."""
    llm_owned = llm is None
    if llm is None:
        try:
            llm = _build_llm(ctx)
            llm_owned = True
        except CliError:
            llm = None
    if llm is not None:
        try:
            description = await generate_pr_description(
                llm,
                diff=diff,
                commits=commits,
                base=base,
                branch=branch,
            )
        except Exception as exc:
            raise CliError(_llm_failure_hint(exc)) from exc
        if llm_owned:
            await llm.close()
        if title is not None:
            return description.model_copy(update={"title": title})
        return description
    subject = commits[0].split(" ", 1)[-1] if commits else "changes"
    summary = "\n".join(f"- {line}" for line in commits)
    return PRDescription(
        title=title or subject,
        summary=f"{len(commits)} commit(s) on {branch} against {base}.\n\n{summary}",
        tests="Not run",
    )


async def _gh_pr_create(
    ctx: CliContext,
    repo: Path,
    base: str,
    head: str,
    description: PRDescription,
    *,
    draft: bool,
) -> bool:
    """Open the PR with ``gh``; return whether it was created."""
    command = [
        "gh",
        "pr",
        "create",
        "--base",
        base,
        "--head",
        head,
        "--title",
        description.title,
        "--body",
        render_pr(description),
    ]
    if draft:
        command.append("--draft")
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    except FileNotFoundError:
        ctx.console.print("[yellow]gh not found on PATH; PR not opened[/]")
        return False
    except TimeoutError:
        process.kill()
        await process.wait()
        ctx.console.print("[yellow]gh timed out; PR not opened[/]")
        return False
    if process.returncode != 0:
        detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
        ctx.console.print(f"[yellow]gh pr create failed[/] {detail or 'unknown error'}")
        return False
    ctx.console.print(stdout.decode("utf-8", errors="replace").strip())
    return True


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
    llm_owned = llm is None
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
                "git diff tools (and file_read) to inspect the changes. "
                "Begin your final reply with exactly one line: 'VERDICT: PASS' "
                "or 'VERDICT: CHANGES_NEEDED', then give your findings and "
                "actionable feedback, and finish with your findings as a JSON "
                "array in a ```json``` fenced block as described in your "
                "instructions."
            ),
        )
        sink = _TokenSink(ctx.console)
        agent = LoopAgent(
            llm=llm,
            executor=executor,
            system_prompt=REVIEWER_PROMPT,
            max_steps=max_steps,
            max_tokens=ctx.settings.llm_max_tokens,
            temperature=ctx.settings.llm_temperature,
            on_token=sink.feed,
            stream=ctx.settings.cli_stream_tokens,
        )
        try:
            result = await agent.run_from([seed])
            answer = result.answer
            if _final_verdict(answer) is None:
                sink.finish()
                answer = await _reask_verdict(agent, seed, answer)
        except Exception as exc:
            raise CliError(_llm_failure_hint(exc)) from exc
        if not sink.finish():
            ctx.console.print(answer)
        verdict = _final_verdict(answer)
        if verdict is None:
            ctx.console.print(
                "[yellow]no verdict produced; treating the review as CHANGES_NEEDED[/]"
            )
            return 1
        report = parse_report(answer)
        if report.findings:
            _render_findings_table(ctx.console, report.findings)
        else:
            ctx.console.print("[dim]no structured findings parsed[/]")
        return 0 if verdict == "PASS" else 1
    finally:
        await executor.sandboxes.close()
        if llm_owned:
            await llm.close()


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
    llm_owned = False
    if executor is None:
        executor = _build_executor(ctx, repo)
    try:
        if fix:
            if llm is None:
                llm = _build_llm(ctx)
                llm_owned = True
            max_repairs = repairs if repairs is not None else ctx.settings.test_max_repairs
            sink = _TokenSink(ctx.console)
            agent = RepairAgent(
                llm=llm,
                executor=executor,
                max_repairs=max_repairs,
                test_command=command,
                test_framework=framework,
                on_token=sink.feed,
                stream=ctx.settings.cli_stream_tokens,
            )
            try:
                repair = await agent.run("Run the project's test suite and fix any failing tests.")
            except Exception as exc:
                raise CliError(_llm_failure_hint(exc)) from exc
            sink.finish()
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
        if llm_owned:
            assert llm is not None
            await llm.close()


async def _render_memory_entries(ctx: CliContext, entries: Sequence[MemoryEntry]) -> None:
    """Print memory entries as a table with a count summary."""
    rows = list(entries)
    if not rows:
        ctx.console.print("[dim]no memory entries[/]")
        return
    table = Table(title=f"memory ({len(rows)} entries)")
    table.add_column("kind", style="cyan")
    table.add_column("source", style="magenta")
    table.add_column("remembered", style="dim")
    table.add_column("content")
    for entry in rows:
        when = entry.created_at.strftime("%Y-%m-%d %H:%M") if entry.created_at else "?"
        table.add_row(str(entry.kind), entry.source, when, _snippet(entry.content))
    ctx.console.print(table)


_SEVERITY_STYLE = {
    "critical": "bold red",
    "high": "red",
    "medium": "yellow",
    "low": "cyan",
    "nit": "dim",
}


def _render_findings_table(console: Console, findings: Sequence[ReviewFinding]) -> None:
    """Render review findings as a severity-ordered table."""
    ordered = sort_findings(list(findings))
    table = Table(title=f"review findings ({len(ordered)})")
    table.add_column("severity", style="bold")
    table.add_column("location", style="cyan")
    table.add_column("problem")
    table.add_column("suggested fix")
    for finding in ordered:
        location = finding.file
        if finding.line is not None:
            location = f"{location}:{finding.line}"
        style = _SEVERITY_STYLE.get(finding.severity.value, "")
        table.add_row(
            f"[{style}]{finding.severity.value.upper()}[/]",
            location,
            finding.problem,
            finding.fix or "",
        )
    console.print(table)


async def cmd_memory_add(
    ctx: CliContext,
    *,
    repo: Path,
    state: WorkspaceState,
    content: str,
    kind: str,
) -> int:
    """Remember a durable fact, decision, or preference for this workspace."""
    content = content.strip()
    if not content:
        raise CliError("memory content is empty")
    from app.database.models.enums import MemoryKind

    async with ctx.db_session() as session:
        await MemoryService.from_session(session).remember(
            state.workspace_id,
            content=content,
            kind=MemoryKind(kind),
            source="cli",
        )
        await session.commit()
    ctx.console.print(f"[green]remembered[/] [bold]{_short(content)}[/] as {kind}")
    return 0


async def cmd_memory_list(
    ctx: CliContext,
    *,
    repo: Path,
    state: WorkspaceState,
    kind: str | None,
    limit: int,
) -> int:
    """List remembered entries, newest first, optionally filtered by kind."""
    from app.database.models.enums import MemoryKind

    async with ctx.db_session() as session:
        entries = await MemoryService.from_session(session).list(
            state.workspace_id,
            kind=MemoryKind(kind) if kind else None,
            limit=limit,
        )
    await _render_memory_entries(ctx, entries)
    return 0


async def cmd_memory_recall(
    ctx: CliContext,
    *,
    repo: Path,
    state: WorkspaceState,
    query: str,
    limit: int,
) -> int:
    """Search remembered entries for the most relevant to ``query``."""
    query = query.strip()
    if not query:
        raise CliError("recall query is empty")
    async with ctx.db_session() as session:
        entries = await MemoryService.from_session(session).recall(
            state.workspace_id,
            query,
            limit=limit,
        )
    await _render_memory_entries(ctx, entries)
    return 0


async def cmd_memory_clear(
    ctx: CliContext,
    *,
    repo: Path,
    state: WorkspaceState,
    yes: bool,
) -> int:
    """Delete every remembered entry for this workspace."""
    async with ctx.db_session() as session:
        count = await MemoryService.from_session(session).count(state.workspace_id)
        if not yes:
            confirmed = Confirm.ask(f"Delete all {count} memory entries? [y/N]", default=False)
            if not confirmed:
                ctx.console.print("[dim]cancelled[/]")
                return 1
        deleted = await MemoryService.from_session(session).clear(state.workspace_id)
        await session.commit()
    ctx.console.print(f"[green]cleared[/] {deleted} memory entries")
    return 0


def render_event(
    console: Console,
    event: OrchestratorEvent,
    sink: _TokenSink | None = None,
) -> None:
    """Render one orchestrator event as a line of CLI output."""
    if event.kind == "started":
        console.print(f"[bold cyan]started[/] {event.detail or ''}")
    elif event.kind == "message":
        _render_message(console, event.message, sink)
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


def _render_message(
    console: Console,
    message: ChatMessage | None,
    sink: _TokenSink | None = None,
) -> None:
    if message is None:
        return
    if message.role == ChatRole.USER:
        console.print(f"[cyan]▶[/] {message.content}")
    elif message.role == ChatRole.ASSISTANT:
        streamed = sink.finish() if sink is not None else False
        if not streamed and message.content:
            console.print(f"[green]◆[/] {message.content}")
        for request in message.tool_requests:
            console.print(f"  [yellow]→ {request.name} {json.dumps(request.arguments)}[/]")
    elif message.role == ChatRole.TOOL:
        console.print(f"  [yellow]←[/] {_snippet(message.content)}")


async def cmd_eval_list(ctx: CliContext, *, repo: Path) -> int:
    """List the registered benchmark tasks."""
    if not BENCHMARK_TASKS:
        ctx.console.print("[dim]no benchmark tasks registered[/]")
        return 0
    table = Table(title=f"benchmark tasks ({len(BENCHMARK_TASKS)})")
    table.add_column("id", style="cyan")
    table.add_column("category", style="magenta")
    table.add_column("name")
    table.add_column("goal")
    for task in BENCHMARK_TASKS:
        table.add_row(task.id, task.category, task.name, _short(task.goal, 80))
    ctx.console.print(table)
    ctx.console.print("[dim]run one with `engineer eval run <id>`[/]")
    return 0


async def cmd_eval_run(
    ctx: CliContext,
    *,
    task_id: str,
    workspace: str | None,
    keep: bool,
    timeout: int | None,
    results_path: str | None,
    llm: LLMProvider | None = None,
    runner: EvalRunner | None = None,
) -> int:
    """Run one benchmark task headlessly against the LLM and record the result.

    Exit code 0 means the task completed and its verification suite passed.
    The scratch workspace is temporary and deleted unless ``keep`` is set or
    ``workspace`` was provided.
    """
    try:
        task = task_by_id(task_id)
    except KeyError:
        raise CliError(f"unknown benchmark task: {task_id} (see `engineer eval list`)") from None

    if llm is None:
        llm = _build_llm(ctx)
        llm_owned = True
    else:
        llm_owned = False

    temp_dir: str | None = None
    try:
        if workspace:
            workspace_dir = Path(workspace)
        else:
            temp_dir = tempfile.mkdtemp(prefix=f"engineer-eval-{task.id}-")
            workspace_dir = Path(temp_dir)

        if runner is None:
            container = ctx.require_container()
            runner = EvalRunner(
                session_factory=container.session_factory,
                llm=llm,
                settings=ctx.settings,
                timeout_seconds=timeout,
            )
        store = ResultStore(Path(results_path or ctx.settings.eval_results_path).expanduser())
        ctx.console.print(
            f"[bold cyan]▶[/] benchmark [bold]{task.id}[/] — {task.name}  [dim]({task.category})[/]"
        )
        record = await runner.run(task, workspace_dir, store=store)
        _render_eval_result(ctx.console, record)
        return 0 if record.passed else 1
    finally:
        if llm_owned:
            await llm.close()
        if temp_dir is not None and not keep and not ctx.settings.eval_keep_workspaces:
            shutil.rmtree(temp_dir, ignore_errors=True)


async def cmd_eval_results(
    ctx: CliContext,
    *,
    model: str | None,
    results_path: str | None,
) -> int:
    """Show recorded evaluation results, newest first."""
    store = ResultStore(Path(results_path or ctx.settings.eval_results_path).expanduser())
    records = [
        record for record in reversed(store.load()) if model is None or record.model == model
    ]
    if not records:
        ctx.console.print(f"[dim]no eval results at {store.path}[/]")
        return 1
    table = Table(title=f"eval results ({len(records)} runs)")
    table.add_column("finished", style="dim")
    table.add_column("task", style="cyan")
    table.add_column("model", style="magenta")
    table.add_column("status")
    table.add_column("tests")
    table.add_column("verdict")
    for record in records:
        when = record.finished_at.strftime("%Y-%m-%d %H:%M")
        status = record.task_status
        tests = record.test_summary
        verdict = "PASS" if record.passed else "FAIL"
        table.add_row(
            when,
            record.task_id,
            record.model,
            status,
            tests,
            f"[{'green' if record.passed else 'red'}]{verdict}[/]",
        )
    ctx.console.print(table)
    passed = sum(1 for record in records if record.passed)
    ctx.console.print(f"[dim]{passed}/{len(records)} passed[/]")
    return 0


async def cmd_eval_compare(ctx: CliContext, *, results_path: str | None) -> int:
    """Compare pass rates across models from the recorded results."""
    store = ResultStore(Path(results_path or ctx.settings.eval_results_path).expanduser())
    records = store.load()
    if not records:
        ctx.console.print(f"[dim]no eval results at {store.path}[/]")
        return 1
    summaries = summarize(records)
    table = Table(title="model comparison")
    table.add_column("model", style="magenta")
    table.add_column("runs")
    table.add_column("passed")
    table.add_column("pass rate")
    for summary in summaries:
        table.add_row(
            summary.model,
            str(summary.runs),
            str(summary.passed),
            f"{summary.pass_rate:.0%}",
        )
    ctx.console.print(table)
    return 0


def _render_eval_result(console: Console, record: EvalResultRecord) -> None:
    """Render one eval run's outcome as a verdict panel."""
    style = "green" if record.passed else "red"
    console.print(
        Panel(
            f"task: [bold]{record.task_id}[/] ({record.task_name})\n"
            f"model: {record.provider}/{record.model}\n"
            f"status: {record.task_status}  ·  tests: "
            f"{'pass' if record.tests_passed else 'fail'}\n"
            f"attempts: {record.attempts}  ·  tokens: {record.tokens}  ·  "
            f"duration: {record.duration_seconds:.1f}s\n"
            f"{record.test_summary}",
            title=f"eval {record.task_id} — {'PASS' if record.passed else 'FAIL'}",
            border_style=style,
        )
    )
    if record.output_tail:
        console.print(Syntax(record.output_tail, "text", theme="ansi_dark"))
