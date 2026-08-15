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
from app.monitoring.telemetry import init_telemetry, shutdown_telemetry

_EPILOG = """\
examples:
  engineer init                    bind this git checkout to a workspace
  engineer index                   index the workspace's source files
  engineer run fix the failing test
  engineer run --agent-type pipeline refactor the auth service
  engineer run -y drop the migration (approve the plan automatically)
  engineer test                     run the project's test suite (sandboxed)
  engineer test --fix               run tests and repair failures, then re-run
  engineer review                   structured review of the working tree
  engineer audit                    staff-engineer production-readiness audit
  engineer memory add "pins are async" --kind decision
  engineer memory list --limit 20
  engineer memory recall "why sqlite?"
  engineer memory clear
  engineer eval list
  engineer eval run fix-auth-bug
  engineer eval results
  engineer eval compare
  engineer tasks --limit 20
  engineer diff HEAD~1
  engineer commit -m "fix: ..."
  engineer commit --generate          LLM-draft the commit message from the diff
  engineer pr                         prepare a PR, push the branch, and open it with gh
  engineer pr --draft --title "wip"   open a draft PR with an explicit title
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

    index = subparsers.add_parser("index", help="index the workspace's source files")
    index.set_defaults(handler=_cmd_index)

    run = subparsers.add_parser("run", help="plan a goal, then run it after approval")
    run.add_argument(
        "--agent-type",
        choices=("coder", "pipeline"),
        default="coder",
        help="which agent to run (default: coder)",
    )
    run.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="approve the plan without prompting",
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
    commit.add_argument(
        "--generate",
        action="store_true",
        help="draft the commit message from the diff using the LLM",
    )
    commit.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    commit.set_defaults(handler=_cmd_commit)

    pr = subparsers.add_parser("pr", help="prepare a pull request and open it with gh")
    pr.add_argument("--base", help="base branch (auto-detected from the remote when omitted)")
    pr.add_argument("--branch", help="head branch (defaults to the checked-out branch)")
    pr.add_argument("--remote", default="origin", help="push target remote (default: origin)")
    pr.add_argument("--draft", action="store_true", help="open the PR as a draft")
    pr.add_argument("--title", help="override the generated PR title")
    pr.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    pr.set_defaults(handler=_cmd_pr)

    cancel = subparsers.add_parser("cancel", help="cancel a running task")
    cancel.add_argument("task_id", help="the task id")
    cancel.set_defaults(handler=_cmd_cancel)

    review = subparsers.add_parser("review", help="review the working tree's changes")
    review.add_argument("--ref", help="review the diff against this revision instead")
    review.add_argument(
        "--max-steps",
        type=int,
        default=8,
        help="upper bound on reviewer LLM calls (default: 8)",
    )
    review.set_defaults(handler=_cmd_review)

    audit = subparsers.add_parser("audit", help="staff-engineer production-readiness audit")
    audit.add_argument("--ref", help="audit the diff against this revision instead")
    audit.add_argument(
        "--max-steps",
        type=int,
        default=8,
        help="upper bound on auditor LLM calls (default: 8)",
    )
    audit.set_defaults(handler=_cmd_audit)

    test = subparsers.add_parser("test", help="run the project's test suite")
    test.add_argument(
        "--command",
        dest="test_command",
        help="test command override (auto-detected when omitted)",
    )
    test.add_argument(
        "--framework",
        choices=("pytest", "jest", "generic"),
        help="runner family override (auto-detected when omitted)",
    )
    test.add_argument(
        "--fix",
        action="store_true",
        help="fix failing tests and re-run (test-and-repair loop)",
    )
    test.add_argument(
        "--repairs",
        type=int,
        help="max fix->re-run iterations with --fix (default: TEST_MAX_REPAIRS)",
    )
    test.set_defaults(handler=_cmd_test)

    memory = subparsers.add_parser("memory", help="inspect and edit the workspace's durable memory")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True, metavar="subcommand")

    memory_add = memory_sub.add_parser("add", help="remember a fact, decision, or preference")
    memory_add.add_argument(
        "--kind",
        choices=("fact", "decision", "preference", "conversation"),
        default="fact",
        help="what kind of memory to record (default: fact)",
    )
    memory_add.add_argument("content", nargs="+", help="the memory to remember")
    memory_add.set_defaults(memory_handler=_cmd_memory_add)

    memory_list = memory_sub.add_parser("list", help="list remembered entries (newest first)")
    memory_list.add_argument(
        "--kind",
        choices=("fact", "decision", "preference", "conversation"),
        help="only entries of this kind",
    )
    memory_list.add_argument("--limit", type=int, default=100, help="how many entries to show")
    memory_list.set_defaults(memory_handler=_cmd_memory_list)

    memory_recall = memory_sub.add_parser("recall", help="search remembered entries for a topic")
    memory_recall.add_argument("query", nargs="+", help="the topic to recall")
    memory_recall.add_argument("--limit", type=int, default=20, help="how many entries to show")
    memory_recall.set_defaults(memory_handler=_cmd_memory_recall)

    memory_clear = memory_sub.add_parser("clear", help="delete every remembered entry")
    memory_clear.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the confirmation prompt",
    )
    memory_clear.set_defaults(memory_handler=_cmd_memory_clear)

    eval_cmd = subparsers.add_parser(
        "eval",
        help="run headless benchmark evaluations against the LLM",
    )
    eval_sub = eval_cmd.add_subparsers(dest="eval_command", required=True, metavar="subcommand")

    eval_list = eval_sub.add_parser("list", help="list the benchmark tasks")
    eval_list.set_defaults(eval_handler=_cmd_eval_list)

    eval_run = eval_sub.add_parser(
        "run",
        help="run one benchmark task headlessly and record the result",
    )
    eval_run.add_argument("task_id", help="benchmark task id (see `engineer eval list`)")
    eval_run.add_argument(
        "--workspace",
        help="scaffold into this directory instead of a temporary one",
    )
    eval_run.add_argument(
        "--keep",
        action="store_true",
        help="keep the temporary workspace after the run",
    )
    eval_run.add_argument(
        "--timeout",
        type=int,
        help="wall-clock cap for this run in seconds (default: task timeout)",
    )
    eval_run.add_argument(
        "--results",
        help="result store path (default: EVAL_RESULTS_PATH)",
    )
    eval_run.set_defaults(eval_handler=_cmd_eval_run)

    eval_results = eval_sub.add_parser("results", help="show recorded evaluation results")
    eval_results.add_argument(
        "--model",
        help="only results for this model identifier",
    )
    eval_results.add_argument(
        "--results",
        help="result store path (default: EVAL_RESULTS_PATH)",
    )
    eval_results.set_defaults(eval_handler=_cmd_eval_results)

    eval_compare = eval_sub.add_parser("compare", help="compare pass rates across models")
    eval_compare.add_argument(
        "--results",
        help="result store path (default: EVAL_RESULTS_PATH)",
    )
    eval_compare.set_defaults(eval_handler=_cmd_eval_compare)
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


async def _cmd_index(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    assert state is not None
    return await commands.cmd_index(ctx, repo=repo, state=state)


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
        yes=args.yes,
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
        generate=args.generate,
    )


async def _cmd_pr(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    return await commands.cmd_pr(
        ctx,
        repo=repo,
        state=state,
        base=args.base,
        branch=args.branch,
        remote=args.remote,
        draft=args.draft,
        yes=args.yes,
        title=args.title,
    )


async def _cmd_cancel(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    assert state is not None
    return await commands.cmd_cancel(ctx, repo=repo, state=state, task_id=args.task_id)


async def _cmd_review(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    return await commands.cmd_review(
        ctx,
        repo=repo,
        state=state,
        ref=args.ref,
        max_steps=args.max_steps,
    )


async def _cmd_audit(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    return await commands.cmd_audit(
        ctx,
        repo=repo,
        state=state,
        ref=args.ref,
        max_steps=args.max_steps,
    )


async def _cmd_test(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    return await commands.cmd_test(
        ctx,
        repo=repo,
        state=state,
        command=args.test_command,
        framework=args.framework,
        fix=args.fix,
        repairs=args.repairs,
    )


async def _cmd_memory_add(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    assert state is not None
    return await commands.cmd_memory_add(
        ctx,
        repo=repo,
        state=state,
        content=" ".join(args.content),
        kind=args.kind,
    )


async def _cmd_memory_list(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    assert state is not None
    return await commands.cmd_memory_list(
        ctx,
        repo=repo,
        state=state,
        kind=args.kind,
        limit=args.limit,
    )


async def _cmd_memory_recall(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    assert state is not None
    return await commands.cmd_memory_recall(
        ctx,
        repo=repo,
        state=state,
        query=" ".join(args.query),
        limit=args.limit,
    )


async def _cmd_memory_clear(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    assert state is not None
    return await commands.cmd_memory_clear(ctx, repo=repo, state=state, yes=args.yes)


async def _cmd_eval_list(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    return await commands.cmd_eval_list(ctx, repo=repo)


async def _cmd_eval_run(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    return await commands.cmd_eval_run(
        ctx,
        task_id=args.task_id,
        workspace=args.workspace,
        keep=args.keep,
        timeout=args.timeout,
        results_path=args.results,
    )


async def _cmd_eval_results(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    return await commands.cmd_eval_results(
        ctx,
        model=args.model,
        results_path=args.results,
    )


async def _cmd_eval_compare(
    ctx: CliContext, args: Namespace, repo: Path, state: WorkspaceState | None
) -> int:
    return await commands.cmd_eval_compare(ctx, results_path=args.results)


async def arun(argv: Sequence[str] | None, ctx: CliContext) -> int:
    """Run the CLI with ``argv`` against ``ctx``, returning the exit code.

    Never raises for user-facing failures: :class:`CliError` is rendered as a
    ``error:`` line and mapped to exit code 1.
    """
    args = build_parser().parse_args(argv)
    init_telemetry(ctx.settings)
    try:
        if args.command == "eval":
            repo = Path.cwd()
            state = None
        else:
            repo = await find_repo_root(Path.cwd())
            state = None if args.command == "init" else load_state(repo)
            if state is None and args.command != "init":
                raise CliError(f"no binding for {repo} — run `engineer init` first")
        handler = (
            getattr(args, "handler", None)
            or getattr(args, "memory_handler", None)
            or getattr(args, "eval_handler", None)
        )
        assert handler is not None, "every subcommand must set a handler"
        return await handler(ctx, args, repo, state)
    except CliError as exc:
        ctx.console.print(f"[red]error[/] {exc}")
        return 1
    finally:
        await ctx.aclose()
        shutdown_telemetry()


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
