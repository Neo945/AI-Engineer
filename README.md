# coding-agent

An autonomous AI software engineering agent: understands a Git repository,
plans work, edits files, runs commands and tests, reviews its own output,
and deploys — the architecture lineage of Cursor Agent, Claude Code, Devin,
and GitHub Copilot Workspace.

**Status:** Phase 8 (multi-agent pipeline) complete. The provider-agnostic
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
for rework (bounded by `pipeline_max_passes`).

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
