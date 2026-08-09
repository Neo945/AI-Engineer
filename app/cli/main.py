"""Argparse front door for the engineer CLI.

``main`` is the console-script target; ``run_cli`` is the sync wrapper tests
can call from a fresh event loop; ``arun`` is the async core that accepts an
injected :class:`CliContext` (or default) so tests can run commands without a
TTY and against a stubbed orchestrator/container.
"""

from __future__ import annotations

import asyncio
import sys
from argparse import ArgumentParser, HelpFormatter, Namespace
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.cli import commands
from app.cli.context import (
    CliContext,
    CliError,
    WorkspaceState,
    find_repo_root,
    load_state,
    make_context,
)

_EPILOG = """\
examples:
  engineer init                    bind this git checkout to a workspace
  engineer run fix the failing test
  engineer run --agent-type pipeline refactor the auth service
  engineer tasks --limit 20
  engineer diff HEAD~1
  engineer commit -m "fix: ..."
"""


def build_parser() -> ArgumentParser:
    """Return the fully configured argument parser (independent of argv)."""
    parser = ArgumentParser(
        prog="engineer",
        description="Autonomous AI software engineering agent.",
        epilog=_EPILOG,
        formatter_class=_HelpFormatter,
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")

    init = subparsers.add_parser("init", help="bind this git checkout to a workspace + session")
    init.add_argument("--name", help="workspace name (defaults to the repository name)")
    init.set_defaults(handler=_cmd_init)

    status = subparsers.add_parser("status", help="show the bound workspace and recent activity")
    status.set_defaults(handler=_cmd_status)

    run = subparsers.add_parser("run", help="run a goal through the orchestrator")
    run.add_argument(
        "--agent-type",
        choices=("coder", "pipeline"),
        default="coder",
        help="which agent to run (default: coder)",
    )
    run.add_argument("goal", nargs="+", help="the goal; quote it or use multiple words")
    run.set_defaults(handler=_cmd_run)

    tasks = subparsers.add_parser("tasks", help="list the session's tasks")
    tasks.add_argument("--limit", type=int, default=20, help="how many tasks to show (default: 20)")
    tasks.set_defaults(handler=_cmd_tasks)

    diff = subparsers.add_parser("diff", help="show the working-tree diff")
    diff.add_argument("ref", nargs="?", default=None, help="optional revision to diff against")
    diff.set_defaults(handler=_cmd_diff)

    commit = subparsers.add_parser("commit", help="stage the working tree and commit")
    commit.add_argument("-m", "--message", help="commit message (prompts when omitted)")
    commit.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    commit.set_defaults(handler=_cmd_commit)

    cancel = subparsers.add_parser("cancel", help="cancel a running task")
    cancel.add_argument("task_id", help="the task id")
    cancel.set_defaults(handler=_cmd_cancel)

    subparsers.add_parser("review", help="structured code review (skeleton)").set_defaults(
        handler=_cmd_review
    )
    subparsers.add_parser("test", help="test execution (skeleton)").set_defaults(
        handler=_cmd_test
    )
    return parser


class _HelpFormatter(HelpFormatter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("max_help_position", 36)
        super().__init__(*args, **kwargs)


async def _cmd_init(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    return await commands.cmd_init(ctx, repo=repo, name=args.name)


async def _cmd_status(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    assert state is not None
    return await commands.cmd_status(ctx, repo=repo, state=state)


async def _cmd_run(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    assert state is not None
    return await commands.cmd_run(
        ctx,
        repo=repo,
        state=state,
        goal=" ".join(args.goal),
        agent_type=args.agent_type,
    )


async def _cmd_tasks(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    assert state is not None
    return await commands.cmd_tasks(ctx, repo=repo, state=state, limit=args.limit)


async def _cmd_diff(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    return await commands.cmd_diff(ctx, repo=repo, state=state, ref=args.ref)


async def _cmd_commit(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    return await commands.cmd_commit(
        ctx,
        repo=repo,
        state=state,
        message=args.message,
        yes=args.yes,
    )


async def _cmd_cancel(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    assert state is not None
    return await commands.cmd_cancel(ctx, repo=repo, state=state, task_id=args.task_id)


async def _cmd_review(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    return await commands.cmd_review(ctx, repo=repo, state=state)


async def _cmd_test(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    return await commands.cmd_test(ctx, repo=repo, state=state)


async def arun(argv: Sequence[str] | None, ctx: CliContext) -> int:
    """Run the CLI with ``argv`` against ``ctx``, returning the exit code.

    Never raises for user-facing failures: :class:`CliError` is rendered as a
    ``error:`` line and mapped to exit code 1.
    """
    args = build_parser().parse_args(argv)
    try:
        repo = await find_repo_root(Path.cwd())
        state = None if args.command == "init" else load_state(repo)
        if state is None and args.command != "init":
            raise CliError(
                f"no binding for {repo} — run `engineer init` first"
            )
        return await args.handler(ctx, args, repo, state)
    except CliError as exc:
        ctx.console.print(f"[red]error[/] {exc}")
        return 1
    finally:
        await ctx.aclose()


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    context_factory: Callable[[], CliContext] = make_context,
) -> int:
    """Run the CLI synchronously on a fresh event loop."""
    return asyncio.run(arun(argv, context_factory()))


def main() -> None:
    """Console-script entry point: ``engineer``."""
    sys.exit(run_cli(sys.argv[1:]))


if __name__ == "__main__":
    main()
