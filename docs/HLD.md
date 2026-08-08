# High-Level Design (HLD) — coding-agent

Version 1.1 · applies to the current implementation (Phases 1–6, streaming
events over SSE)

---

## 1. Document purpose

This document describes the system at an architectural level: what it is,
the components it is built from, how data flows through it, the key design
decisions, and its security model. Implementation detail belongs in
[LLD.md](LLD.md).

## 2. What the system is

An **autonomous AI software engineering agent** backend. A user gives it a
natural-language goal about a Git repository (fix a bug, add a feature,
refactor); the agent reads the code, edits files, runs commands and tests,
and returns a summary of what it changed. It follows the architecture
lineage of Cursor Agent, Claude Code, Devin, and Copilot Workspace.

### Goals

- Execute agentic coding work on a user's repository **without ever running
  untrusted commands on the host**.
- Abstract away LLM vendors so the same agent loop works with Anthropic,
  OpenAI, and OpenAI-compatible local backends.
- Persist every task and its full transcript durably (PostgreSQL), so work
  survives restarts and is auditable.
- Be a clean **modular monolith**: well-separated feature packages that can
  be split into services later if load demands.

### Non-goals (for now)

- Frontend, authentication, multi-tenancy, payment/billing.
- Long-running orchestration across many tasks (no durable checkpoints,
  retries, or cancellation yet).
- Multi-agent planning/review/testing pipeline (single coder agent today).
- Retrieval (AST indexing, embeddings) and long-term memory.

## 3. System context

```
                         ┌───────────────────────────────────────────────┐
   User / HTTP client    │                 coding-agent backend          │
 ┌───────────────┐       │                                               │
 │ curl / browser│──────▶│  Gateway (FastAPI)  ──▶  Orchestrator         │
 │ (Swagger /docs)│      │        │                    │                 │
 └───────────────┘       │        │                    ▼                 │
                         │        │              CoderAgent (LangGraph)  │
                         │        │                    │                 │
                         │        │                    ▼                 │
                         │        │      LLM provider (Anthropic/OpenAI) │
                         │        │                    │                 │
                         │        │                    ▼                 │
                         │        │         ToolExecutor ──▶ sandbox     │
                         │        │                        (Docker)      │
                         │        │                    │                 │
                         │        └──────▶ PostgreSQL / Redis            │
                         └───────────────────────────────────────────────┘
```

External dependencies:

| Dependency        | Role                                                              |
|-------------------|-------------------------------------------------------------------|
| PostgreSQL 16     | Primary store: users, workspaces, sessions, tasks, messages, vectors |
| Redis 7           | Cache / pub-sub / rate-limit backend (reserved, wired via DI)       |
| Docker daemon     | Runs sandbox containers for terminal commands                      |
| LLM API           | Anthropic Messages API or OpenAI Chat Completions (or local vLLM/Ollama) |

## 4. Architecture style

**Modular monolith** — one deployable process containing strictly separated
feature packages (`gateway`, `core`, `database`, `orchestrator`, `agents`,
`tools`, `executor`, `llm`, plus stubs `retrieval`, `memory`, `evals`,
`monitoring`). Rationale:

- **Simplicity of deployment and operations** during rapid development.
- **Clear seam for extraction**: packages communicate through explicit
  interfaces (protocols, repositories), so a package can be promoted to a
  service with controlled effort.
- Python's GIL and the process model favor in-process composition; a
  service mesh is only justified when load demands it.

## 5. Component architecture

```
Layer                 Package           Responsibility
────────────────────────────────────────────────────────────────────────
API / composition     gateway           FastAPI app factory, routers,
                                        request-scoped dependencies
Core                 core              Settings, structured logging, DI
                                       container (composition root)
Persistence          database          async engine, ORM models,
                                       repositories, Alembic migrations
Orchestration        orchestrator      per-task state machine, transcript
                                       persistence, lifecycle events
Agents               agents            LangGraph agent loops (coder today)
LLM                  llm               provider-agnostic protocol + client
                                       adapters (Anthropic/OpenAI/local)
Tools                tools             typed tool contracts, registry,
                                       tool specs (pure layer, no I/O)
Execution            executor          ToolExecutor dispatch, path
                                       confinement, Docker sandbox
```

### 5.1 Dependency rule

Higher layers depend on lower ones; nothing depends upward.

```
gateway ─▶ orchestrator ─▶ agents ─▶ { tools, llm }
    │           │                      │
    │           └──▶ database ─────────┘ (repositories)
    └──▶ core (settings, container)
```

`tools` and `llm` are **provider-free** (pure contracts); the executor and
client adapters implement them. `database` is depended upon by almost
everything through repositories, never via raw SQL.

## 6. Component responsibilities

### 6.1 Gateway (FastAPI)

- App factory with lifespan-managed `Container`.
- Routers: `health` (liveness/readiness) and `tasks`
  (create+run, list, detail).
- Request-scoped dependencies: DB session, orchestrator (built lazily),
  404 session lookup.
- Serialization via Pydantic response schemas (`from_attributes`).

### 6.2 Orchestrator

- `run_task(task_id)` is the single entry point for executing a task.
- Owns the lifecycle: `PENDING → RUNNING → COMPLETED | FAILED`.
- Resolves the workspace, builds an executor + coder agent, runs the loop,
  persists the transcript incrementally (each message committed and
  published as an event), records result/error, token totals and timestamps.
- Emits `OrchestratorEvent`s (`started`/`message`/`completed`/`failed`) to an
  injectable `EventBroker` (and optional callback) — the transport for SSE
  streaming and the seam for Redis pub/sub at scale.

### 6.3 CoderAgent (LangGraph)

- A single-node **self-loop** over a `StateGraph`. Each pass:
  1. calls the LLM with the accumulated transcript + tool catalog + system prompt;
  2. if tool requests → execute them through `ToolExecutor`, append
     assistant + tool messages, loop;
  3. if a final answer → terminate.
- Bounded by `max_steps` (default 8) to guarantee termination; accumulates
  token usage via `Annotated[int, operator.add]` reducers.
- An optional `on_message` hook fires for every produced message (goal,
  assistant turns, tool results) in transcript order, which is what lets the
  orchestrator persist and stream live.

### 6.4 LLM layer

- `LLMProvider` protocol exposes `complete()` (single response) and
  `stream()` (event stream) with normalized messages/tools/usage.
- Adapters: `AnthropicClient` (top-level `system`), `OpenAIClient` (leading
  `system` message, JSON tool-call args, OpenAI-compatible local backends).
- Selected by settings via `build_llm_client`.

### 6.5 Tools + Executor

- `ToolRegistry`: maps tool name → spec + Pydantic arg validator + async
  handler. Pure, no I/O.
- `ToolExecutor`: binds a registry to one workspace; filesystem and git
  tools run on the host **confined to the workspace root** (`paths.py`);
  terminal runs inside the sandbox container.
- `SandboxManager`: ephemeral Docker containers (non-root, read-only rootfs,
  memory/CPU caps, no network, hard timeout), one per workspace, reused
  across calls, auto-recreated after a timeout kill.

### 6.6 Persistence

- Async SQLAlchemy 2.0 + asyncpg. `expire_on_commit=False`.
- Models: `User`, `Workspace`, `Session`, `Task`, `Message`, `CodeChunk`.
- Repositories isolate all `select` statements; services never leak SQL.

## 7. Request lifecycle (task run)

```
1. POST /sessions/{id}/tasks {"goal": "…"}
2. Gateway: 404 if session missing → create Task (PENDING) → commit → 202
3. Background: Orchestrator marks RUNNING + started_at → resolves workspace dir
4. Build ToolExecutor + CoderAgent (on_message hook wires persistence+events)
5. Loop (≤ max_steps):
     a. LLM.complete(transcript, tools, system) → LLMResponse
     b. tool_requests? → ToolExecutor.execute(each) → TOOL messages
     c. no tools → final answer → end
     d. each message → persist + commit (incremental) + publish "message" event
6. Orchestrator: mark COMPLETED (answer, tokens, finished_at) | FAILED (error)
7. Events fan out via EventBroker; clients get 202 and open the SSE stream
8. SSE replays snapshot + transcript, then streams live; client may also
   poll GET /tasks/{id} for the durable transcript
```

## 8. Security model

Security is defense-in-depth, centered on **never trusting model output with
host privileges**.

| Threat                       | Control                                                           |
|------------------------------|-------------------------------------------------------------------|
| Path traversal / symlink     | `resolve_within()` realpath confinement; rejected before host I/O   |
| Malicious terminal command   | Runs in Docker container: non-root, read-only rootfs, no network,  |
|                              | memory/CPU caps, hard `timeout` + async kill guard                  |
| LLM key leakage              | Keys only via env/.env; never logged; never committed (`.gitignore`) |
| Workspace escape via git     | `GIT_TERMINAL_PROMPT=0`, hooks disabled, host `git` with bounded args|
| Output blowup                | `MAX_OUTPUT_CHARS` truncation, terminal stdout/stderr captured       |
| Unbounded loops              | `max_steps` bound + per-call `max_tokens` + LLM timeout              |

Sandbox container facts: `User` = numeric host uid/gid of the workspace
owner (so the mount stays writable but non-root), `Tmpfs` `/tmp`,
`ReadonlyRootfs: true`, `NetworkMode: none`, single `Binds` to the
workspace. A timed-out command destroys its container and a fresh one is
spawned on the next call.

## 9. Data model (summary; full detail in LLD)

- **users** — identity (auth not yet wired).
- **workspaces** — a repository a user works on; `repo_path` is the host
  checkout path the executor confines itself to.
- **sessions** — durable conversation context bound to a workspace.
- **tasks** — a unit of work; tree via `parent_task_id`; lifecycle state,
  goal, result, error, token totals, timestamps.
- **messages** — append-only transcript; `ordinal` for deterministic
  ordering (a task persists all its messages in one flush, so timestamps
  alone cannot order them); `tool_call_id`/`tool_calls` for tool round-trips.
- **code_chunks** — reserved for AST/embedding retrieval.

## 10. Key design decisions

| Decision | Rationale |
|---|---|
| Provider-agnostic LLM protocol | One agent loop, many backends; local dev with vLLM/Ollama; vendor lock-in avoided |
| Docker sandbox for terminals | Strongest practical isolation for arbitrary commands |
| Host-side, confined filesystem/git tools | Fast and simple; containment via realpath validation |
| LangGraph for the agent loop | Standard graph abstraction; state reducers; future planner/coder/reviewer/tester composition |
| Durable transcript per task | Auditability + context rebuild for future continuation |
| Value-stored PG enums | Renaming Python enum identifiers never breaks stored rows |
| Lazy LLM client construction | The API boots without LLM credentials; misconfiguration surfaces as 503 at run time |
| `ordinal` column for messages | Correct ordering despite single-flush bulk insert (same `created_at`) |

## 11. Deployment

- Docker Compose for dev: `postgres` (pgvector image), `redis`.
- `infra/Dockerfile` packages the API; `infra/executor/Dockerfile` is the
  sandbox image (`make executor-image`).
- The API needs the Docker socket to spawn sandboxes (currently the host
  daemon; a remote/DIND engine is a production option).
- Config is 100% env-driven (`Settings`); `.env.example` documents every var.

## 12. Quality attributes

- **Testability**: pure contract layers (tools, llm) + injectable
  `FakeLLM` + stub executor; integration tests run against real Postgres.
  98 tests passing; `mypy --strict` and `ruff` clean.
- **Extensibility**: new tools = spec + args model + handler, registered in
  `ToolExecutor._register`; new LLM provider = adapter implementing
  `LLMProvider` + factory branch.
- **Observability**: structlog structured logs; readiness probes; per-task
  token accounting.
- **Maintainability**: repository pattern, dependency inversion, small
  focused modules, type-strict.

## 13. Evolution path

1. ~~Streaming events (SSE/WS) on top of the existing `on_event` hook.~~
   **Done (Phase 6)** — tasks run asynchronously (`202`), the orchestrator
   persists per-message and publishes `message` events through an in-process
   `EventBroker`, and `GET …/tasks/{id}/events` streams an SSE replay + live
   updates. Scale-out swap: `EventBroker` → Redis pub/sub, same interface.
2. Retries, cancellation, durable checkpoints (`Task`/`Session` state).
3. Multi-agent pipeline: planner → coder → reviewer → tester.
4. Auth + user-facing session/workspace management APIs.
5. Retrieval (AST + embeddings via pgvector) and memory.
6. Evals harness and monitoring (OTel).
