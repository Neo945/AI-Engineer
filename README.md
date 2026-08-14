# coding-agent

An autonomous AI software engineering agent: understands a Git repository,
plans work, edits files, runs commands and tests, reviews its own output,
and deploys — the architecture lineage of Cursor Agent, Claude Code, Devin,
and GitHub Copilot Workspace.

**Status:** Phase 11 (evaluation framework) complete. The provider-agnostic
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
records JSONL results, and compares pass rates across models.

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
| `monitoring`     | OTel, metrics, logging integration                   | 11    |
