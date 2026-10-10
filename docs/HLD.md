# High-Level Design (HLD) — coding-agent

Version 1.4 · describes the system as built through **Phase 21** (CLI, auth,
retrieval/embeddings, memory, pipeline + coordinator, review/audit/arch/design/
analyze/modernize modes, observability, evals, MCP, CI/CD + K8s, Web UI,
testing specialist + intent-based selection).

> **Currency note:** this HLD was originally written at Phase 8. It has been
> updated at a high level through Phase 21, but the authoritative, current
> deep-dive is [`docs/ARCHITECTURE_AND_SETUP.md`](ARCHITECTURE_AND_SETUP.md).
> Where the two disagree, that document wins. See
> [`docs/TARGET_ARCHITECTURE.md`](TARGET_ARCHITECTURE.md) for the roadmap and
> status. The one unbuilt interface is the VS Code extension.

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
  OpenAI, **Gemini**, and OpenAI-compatible local backends.
- Persist every task and its full transcript durably (PostgreSQL), so work
  survives restarts and is auditable.
- Be a clean **modular monolith**: well-separated feature packages that can
  be split into services later if load demands.
- Offer the same agent core through multiple interfaces: **CLI**, **HTTP
  API**, and the **Web UI**.

### Non-goals (for now)

- VS Code extension (the only unbuilt interface; CLI + Web UI exist).
- Payments/billing and full multi-tenant SaaS wiring.
- Long-running orchestration across many tasks with automatic retries on
  transient failures (manual retries and cooperative cancellation are in).

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
`tools`, `executor`, `llm`, `cli`, `retrieval`, `memory`, `monitoring`,
`evals`, `mcp`, `modernization`, plus the mode packages `audit`,
`architecture`, `design`, `analysis`). Rationale:

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
                                        request-scoped dependencies, Web UI
Core                 core              Settings, structured logging, DI
                                       container (composition root)
Persistence          database          async engine, ORM models,
                                       repositories, Alembic migrations
Orchestration        orchestrator      per-task state machine, transcript
                                       persistence, lifecycle events
Agents               agents            LangGraph loops (coder, planner,
                                       reviewer, tester pipeline, coordinator)
LLM                  llm               provider-agnostic protocol + client
                                       adapters (Anthropic/OpenAI/Gemini/local)
Understanding        retrieval         repo indexer (AST chunks + embeddings),
                                       context assembler
Memory               memory            durable session/project/decision memory
Tools                tools             typed tool contracts, registry,
                                       tool specs (pure layer, no I/O)
Execution            executor          ToolExecutor dispatch, path
                                       confinement, Docker sandbox
Interfaces           cli               `engineer` CLI adapter over the core
Modes                audit, arch,       review/audit/graph/arch/design/
                     design, analysis,  analyze/modernize workflows
                     modernization
Observability        monitoring        OpenTelemetry traces/metrics, structlog
Evals                evals             benchmark runner, result store
Integrations         mcp               MCP stdio client + `mcp_call` tool
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
- Routers: `health` (liveness/readiness), `auth` (register/login/me/
  password), `users` (workspaces + sessions), `tasks` (create+run, list,
  detail, plan/approve/reject/cancel/events), and `ui` (the single-page Web
  UI at `/ui`).
- Request-scoped dependencies: DB session, orchestrator (built lazily),
  authenticated user, 404 session lookup.
- Serialization via Pydantic response schemas (`from_attributes`).

### 6.2 Orchestrator

- `run_task(task_id)` is the single entry point for executing a task.
- Owns the lifecycle: `PENDING → RUNNING → COMPLETED | FAILED | CANCELLED`.
- Resolves the workspace, builds an executor + coder agent, runs the loop,
  persists the transcript incrementally (each message committed and
  published as an event), records result/error, token totals and timestamps.
- Emits `OrchestratorEvent`s (`started`/`message`/`completed`/`failed`/
  `cancelled`) to an
  injectable `EventBroker` (and optional callback) — the transport for SSE
  streaming and the seam for Redis pub/sub at scale.

### 6.3 Agents (LangGraph)

- A shared `LoopAgent` base provides the single-node **self-loop** used by
  every stage agent. Each pass:
  1. calls the LLM with the accumulated transcript + tool catalog + system prompt;
  2. if tool requests → execute them through `ToolExecutor`, append
     assistant + tool messages, loop;
  3. if a final answer → terminate.
- Bounded by `max_steps` (default 8) to guarantee termination; accumulates
  token usage via `Annotated[int, operator.add]` reducers.
- An optional `on_message` hook fires for every produced message (goal,
  assistant turns, tool results) in transcript order, which is what lets the
  orchestrator persist and stream live.
- `CoderAgent` is the default (coder system prompt); the multi-agent
  **pipeline** composes `planner` → `coder` → `reviewer` → `tester` in one
  `StateGraph` over a single shared transcript. Each stage runs a fresh
  `LoopAgent` over the accumulated messages and returns only the delta;
  `_route_review`/`_route_test` send non-PASS verdicts back to the coder,
  with rework counted and bounded by `pipeline_max_passes` so the pipeline
  always terminates.
- The **coordinator** (`CoordinatorAgent`) makes a cheap no-tool dispatch
  decision, runs read-only security/performance/testing specialists
  concurrently (each confined to a `READ_ONLY_TOOLS` allowlist via
  `LoopAgent.tool_names`, so parallel reads can never mutate the workspace),
  optionally runs a coder sequentially when changes are needed, then
  synthesizes one answer. An unparseable dispatch reply falls back to
  keyword hints from `app/agents/intent.py`.

### 6.4 LLM layer

- `LLMProvider` protocol exposes `complete()` (single response) and
  `stream()` (event stream) with normalized messages/tools/usage.
- Adapters: `AnthropicClient` (top-level `system`), `OpenAIClient` (leading
  `system` message, JSON tool-call args, OpenAI-compatible local backends),
  and `GeminiClient` (Google Gen AI SDK).
- Selected by settings via `build_llm_client` (providers: `anthropic`,
  `openai`, `gemini`).
- An `InstrumentedLLM` wrapper records OTel spans/metrics (tokens, latency)
  around any provider.

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
- Models: `User`, `Workspace`, `Session`, `Task`, `Message`, `CodeChunk`,
  `MemoryEntry`.
- Repositories isolate all `select` statements; services never leak SQL.

## 7. Request lifecycle (task run)

```
1. POST /sessions/{id}/tasks {"goal": "…", "agent_type": "coder"|"pipeline"|"coordinator"|"auto"}
2. Gateway: 404 if session missing → create Task (PENDING) → commit → 202
3. Background: Orchestrator resolves "auto" from the goal (app/agents/intent.py),
   marks RUNNING + started_at → resolves workspace dir
4. Build ToolExecutor + agent for task.agent_type
   (coder = single loop; pipeline = planner→coder→reviewer→tester graph;
   coordinator = dispatch → parallel read-only specialists → optional coder → synthesis);
   on_message hook wires persistence+events
5. Agent loop (per stage, ≤ max_steps; pipeline rework bounded by max_passes):
     a. LLM.complete(transcript, tools, system) → LLMResponse
     b. tool_requests? → ToolExecutor.execute(each) → TOOL messages
     c. no tools → final answer → end (pipeline: verdict routes to next stage)
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

- **users** — identity (auth wired: register/login/me/password with JWT
  bearer tokens).
- **workspaces** — a repository a user works on; `repo_path` is the host
  checkout path the executor confines itself to.
- **sessions** — durable conversation context bound to a workspace.
- **tasks** — a unit of work; tree via `parent_task_id`; lifecycle state,
  goal, result, error, token totals, timestamps, persisted `plan` for the
  approve-then-run flow.
- **messages** — append-only transcript; `ordinal` for deterministic
  ordering (a task persists all its messages in one flush, so timestamps
  alone cannot order them); `tool_call_id`/`tool_calls` for tool round-trips.
- **code_chunks** — AST/chunk index of the workspace with embeddings for
  semantic retrieval (populated by `engineer index`).
- **memory_entries** — durable session/project/decision memory with an
  embedding for semantic recall.

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
   661 tests (559 unit / 102 integration) across 123 app modules; `mypy app`
   (strict) and `ruff` clean. 7 Windows-only failures (CRLF/git output).
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
   **Done (Phase 7)** — tasks carry a durable `attempt`/`max_attempts`
   counter; `POST /tasks/{id}/retry` re-runs a terminal task (transcript is
   kept and appended to), and `POST /tasks/{id}/cancel` persists `cancelled`
   and cooperatively stops the running agent at its next step boundary via a
   `CancellationRegistry` (`should_cancel` hook). Scale-out swap: the
   in-process registry → a shared signal store, same interface.
3. Multi-agent pipeline: planner → coder → reviewer → tester.
   **Done (Phase 8)** — `Task.agent_type` may be `pipeline`, running all four
   stages in one LangGraph graph over a single shared transcript with
   reviewer/tester rejection routing back to the coder (rework bounded by
   `pipeline_max_passes`). Stages compose the shared `LoopAgent`; per-stage
   isolation and subtask trees (`parent_task_id`) remain future work.
4. Auth + user-facing session/workspace management APIs.
   **Done (Phase 11/16)** — JWT register/login/me/password; user-scoped
   workspaces and sessions with ownership checks.
5. Retrieval (AST + embeddings via pgvector) and memory.
   **Done** — `app/retrieval` (indexer + `ContextAssembler` with semantic
   blend; pluggable `Embedder`) and `app/memory` (durable, semantic recall).
6. Evals harness and monitoring (OTel).
   **Done** — `app/evals` (6 benchmarks, JSONL result store, `engineer eval`)
   and `app/monitoring` (OTel traces/metrics, HTTP middleware).
7. Modes (review/audit/arch/design/analyze/modernize), coordinator,
   MCP tool integration, CI/CD + K8s deployment, and a Web UI.
   **Done (Phases 17–20)** — see `docs/TARGET_ARCHITECTURE.md`. Remaining:
   VS Code extension; optional K8s autoscaling/Helm.
