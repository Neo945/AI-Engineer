# coding-agent

An autonomous AI software engineering agent: understands a Git repository,
plans work, edits files, runs commands and tests, reviews its own output,
and deploys — the architecture lineage of Cursor Agent, Claude Code, Devin,
and GitHub Copilot Workspace.

**Status:** Phase 13 (git workflow) complete. The provider-agnostic
LLM abstraction (Anthropic, OpenAI, OpenAI-compatible local backends),
LangGraph agents, the orchestrator, and the gateway task API exist and are
tested. Tasks run **asynchronously** (`POST /sessions/{id}/tasks` returns
`202`), the orchestrator persists the transcript **incrementally** and
publishes an event per message, and
`GET /sessions/{id}/tasks/{task_id}/events` streams progress as
**Server-Sent Events** (snapshot + live transcript + terminal event). Tasks
track a durable **attempt** counter: `POST /tasks/{id}/retry` re-runs a
terminal task within its budget, and `POST /tasks/{id}/cancel` stops a
pending or running task cooperatively. Beyond the single `coder` agent, a
task can run the **multi-agent pipeline**
(`agent_type: "pipeline"`): planner → coder → reviewer → tester in one
LangGraph graph, with reviewer/tester rejection routing back to the coder
for rework (bounded by `pipeline_max_passes`). Durable per-workspace
**memory** is recalled and injected into each run
(`engineer memory add|list|recall|clear`), and a headless **eval harness**
(`engineer eval`) runs six SWE-bench-style benchmarks against any LLM,
records JSONL results, and compares pass rates across models. OpenTelemetry
**traces and metrics** (LLM calls, tool executions, task runs, HTTP
requests) are emitted through a `monitoring` package that is a no-op by
default — set `OTEL_ENABLED=true` and point `OTEL_EXPORTER_ENDPOINT` at a
collector (e.g. Jaeger/Grafana) to export spans and metrics. Agents get a
full **git workflow**: `git_log`, `git_branch`, `git_checkout`, and
`git_push` join the existing `git_status`/`git_diff`/`git_commit` tools,
and a **pre-modification working-tree check** (on by default) snapshots the
pre-existing uncommitted state on first mutation and refuses to touch or
commit any path that was dirty before the session began. The CLI gains
`engineer commit --generate` (LLM-drafted conventional commit messages)
and `engineer pr`, which generates a PR title/body from the committed
diff, pushes the branch, and opens the PR with `gh` (best-effort — the
description is always saved under `.engineer/pr-<branch>.md`).

## Architecture

A **modular monolith**: one deployable containing strictly separated
feature packages, so individual services can be extracted later if load
demands.

```
React frontend  ->  Gateway (auth, rate-limit, WS)  ->  Orchestrator (LangGraph)
                                                          | agents (planner/coder/reviewer/tester)
                                                          v tools -> Executor (sandboxed containers)
                                                          | retrieval (AST + embeddings)
                                                          | memory
                                                          v
                                            PostgreSQL (pgvector) / Redis
```

See `docs/` for the full architecture and roadmap.

## Prerequisites

- Python 3.13+
- Docker with Compose v2 (for PostgreSQL + Redis)

## Quick start

```bash
# 1. Provision infrastructure (PostgreSQL 16 + pgvector, Redis 7)
make up

# 2. Create a venv and install everything
make venv dev-install

# 3. Apply database migrations
make migrate

# 4. Run the API (http://localhost:8000, /docs for Swagger)
make run
```

The sandbox integration tests additionally require the executor image:

```bash
make executor-image  # builds the container image that runs terminal commands
```

## Code review

`engineer review` runs a structured review of the working tree, or of a diff
against a revision when `--ref` is given:

```bash
engineer review              # review the working tree's uncommitted changes
engineer review --ref main   # review the diff against main
engineer review --max-steps 4
```

The reviewer inspects the changes with read-only tools (git status, git diff,
file_read) and ends its reply with a **verdict line**, a short prose summary,
and a machine-readable **findings list**:

    VERDICT: CHANGES_NEEDED
    The auth handler never validates token expiry.

    ```json
    [
      {
        "severity": "high",
        "file": "app/auth.py",
        "line": 12,
        "problem": "expiry never checked",
        "reason": "expired tokens are accepted",
        "fix": "compare now <= exp"
      }
    ]
    ```

- **Verdict** — exactly one line, `VERDICT: PASS` or `VERDICT: CHANGES_NEEDED`.
  Exit code is `0` for PASS and `1` for CHANGES_NEEDED; if no verdict is
  recovered the review is treated as CHANGES_NEEDED.
- **Findings** — a JSON array in a `json`-labelled fenced code block. Each
  entry maps to a severity (`critical` | `high` | `medium` | `low` | `nit`), a
  file, an optional line, what is wrong, why it matters, and a suggested fix.
  The CLI prints the findings as a severity-ordered table.
- **Degrades gracefully** — the verdict line is still recovered and the reply
  printed even when the findings block is missing or malformed, so a sloppy
  reply never crashes the CLI.

## Git workflow

Agents can drive the repository themselves: `git_log`, `git_branch`,
`git_checkout`, and `git_push` join `git_status`, `git_diff`, and
`git_commit`. A **pre-modification working-tree check** is on by default
(`git_protect_dirty_tree`): the executor snapshots which paths are
uncommitted at the first mutation and refuses to write, edit, delete, move,
or commit anything that was already dirty before the session began, so the
agent can never clobber the user's in-flight work.

```bash
engineer commit -m "fix: ..."      # commit with an explicit message
engineer commit --generate         # LLM-drafts a conventional message from the diff
engineer pr                        # PR description + push + open with gh
engineer pr --draft --title "wip"  # draft PR with an explicit title
```

`engineer pr` diffs the checked-out branch against the base (auto-detected
from `origin/HEAD`, else `main`/`master`), has the LLM draft a title and
body, pushes the branch, and tries to open the PR with `gh`. It degrades
gracefully: without a remote, without `gh`, or without a configured LLM it
prints a hint and saves the description to
`.engineer/pr-<branch>.md` so the PR can be opened manually.

## Tooling

```bash
make lint       # ruff check + format check
make typecheck  # mypy (strict) over app/
make test       # full suite (needs infra + executor image)
make test-unit  # unit tests only, no infra required
```

## Package layout

| Package          | Purpose                                              | Phase |
|------------------|------------------------------------------------------|-------|
| `gateway`        | FastAPI app factory, routes, request dependencies    | done  |
| `core`           | settings, structured logging, DI container           | done  |
| `database`       | async engine, sessions, ORM base, Alembic migrations | done  |
| `orchestrator`   | agent execution graph (LangGraph) + event bus + retry/cancel  | 5-7   |
| `agents`         | shared loop + planner / coder / reviewer / tester pipeline    | 5-8   |
| `tools`          | typed tool contracts                                 | 4     |
| `executor`       | sandboxed tool execution                             | 4     |
| `retrieval`      | AST indexing, embeddings, semantic search            | 9     |
| `memory`         | conversation / repo / preferences / long-term        | 10    |
| `llm`            | provider abstraction + context management            | 5     |
| `evals`          | headless task harness                                | 11    |
| `monitoring`     | OTel traces + metrics, no-op by default              | 12    |
| `git`            | git tools, dirty-tree protection, PR/commit drafting | 13    |
