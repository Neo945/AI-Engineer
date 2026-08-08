# Low-Level Design (LLD) — coding-agent

Version 1.2 · applies to the current implementation (Phases 1–8, streaming
events over SSE, retries/cancellation, multi-agent pipeline)

Companion to [HLD.md](HLD.md). This document specifies the concrete
structure: packages, classes with their public interfaces, the database
schema, API contracts, key algorithms, configuration, error handling, and
test strategy.

---

## 1. Package structure

```
app/
├── gateway/                  # FastAPI: factory, routes, dependencies, schemas
│   ├── main.py               #   create_app(), lifespan wiring, router registration
│   ├── dependencies.py       #   get_container, get_db_session, get_orchestrator
│   ├── schemas.py            #   TaskCreateRequest, TaskResponse, MessageResponse, TaskDetailResponse
│   └── routes/
│       ├── health.py         #   /healthz, /readyz
│       └── tasks.py          #   task run/list/detail + get_session_or_404
├── core/
│   ├── config.py             #   Settings (pydantic-settings)
│   ├── logging.py            #   structlog configuration
│   └── container.py          #   Container (composition root), lazy orchestrator()
├── database/
│   ├── engine.py             #   build_async_engine (asyncpg, echo/debug)
│   ├── session.py            #   create_session_factory (expire_on_commit=False)
│   ├── base.py               #   DeclarativeBase
│   ├── models/               #   user, workspace, session, task, message,
│   │                         #   code_chunk, enums, mixins
│   └── repositories/         #   base, user, workspace, session, task, message
├── orchestrator/
│   └── orchestrator.py       #   Orchestrator, OrchestratorEvent
├── agents/
│   ├── base.py                #   LoopAgent (shared loop), LoopResult, format_tool_result
│   ├── coder.py               #   CoderAgent (LoopAgent subclass), coder system prompt
│   └── pipeline.py            #   PipelineAgent (planner→coder→reviewer→tester), parse_verdict
├── llm/
│   ├── messages.py           #   ChatMessage, ChatRole, ToolRequest
│   ├── protocol.py           #   LLMProvider, LLMResponse, LLMStreamEvent, LLMUsage
│   ├── factory.py            #   build_llm_client(settings)
│   └── clients/
│       ├── anthropic.py      #   AnthropicClient
│       └── openai.py         #   OpenAIClient (+ OpenAI-compatible local backends)
├── tools/
│   ├── schemas.py            #   ToolName, ToolCall, ToolResult, ToolSpec
│   ├── registry.py           #   ToolRegistry, Handler
│   └── specs/                #   filesystem.py, terminal.py, git.py (+ __init__ aggregation)
├── executor/
│   ├── executor.py           #   ToolExecutor, GitOutput
│   ├── paths.py              #   resolve_within, PathTraversalError
│   └── sandbox.py            #   SandboxManager, Sandbox, SandboxLimits, SandboxOutput
├── retrieval/  memory/  evals/  monitoring/   # reserved placeholder packages
alembic/versions/                            # 0001..0004
tests/
├── unit/        # config, models, paths, executor_fs, tool_registry, llm_*, coder_agent
├── integration/ # health, repositories, orchestrator, tasks_api
└── sandbox/     # real Docker sandbox execution
```

## 2. Database schema

Native PG enums store **values** (e.g. `'completed'`), created via
`native_enum(cls, name)` with `values_callable`. All PKs are UUIDs
(`UUIDPrimaryKeyMixin`); `TimestampMixin` adds `created_at`/`updated_at`.

```
users
  id UUID PK | email varchar(320) UNIQUE IX | hashed_password varchar(255)?
  full_name varchar(120) | is_active bool | is_superuser bool | created_at | updated_at

workspaces
  id UUID PK | owner_id FK→users (CASCADE) IX | name varchar(120)
  repo_url varchar(2000)? | repo_path varchar(500) | default_branch varchar(255) | created_at | updated_at

sessions
  id UUID PK | workspace_id FK→workspaces (CASCADE) IX | user_id FK→users (CASCADE) IX
  title varchar(255) | status session_status (idle|running|waiting|completed|failed|cancelled)
  meta JSONB | created_at | updated_at

tasks
  id UUID PK | session_id FK→sessions (CASCADE) IX | parent_task_id FK→tasks (SET NULL) IX
  agent_type varchar(50) | status task_status (pending|planning|running|reviewing|testing|
                                             completed|failed|cancelled)
  goal TEXT | result TEXT? | error TEXT?
  attempt int (0) | max_attempts int (3)             # durable retry checkpoint
  input_tokens int (0) | output_tokens int (0)
  started_at timestamptz? | finished_at timestamptz? | created_at | updated_at

messages                                 # append-only (no updated_at)
  id UUID PK | session_id FK→sessions (CASCADE) IX | task_id FK→tasks (SET NULL) IX
  role message_role (system|user|assistant|tool)
  content TEXT | ordinal int (0) IX
  tool_call_id varchar(100)? | tool_calls JSONB? | tool_name varchar(100)?
  token_count int (0) | created_at

code_chunks                                # retrieval-scaffolded (Phase 9)
  id UUID PK | workspace_id FK→workspaces (CASCADE) IX
  file_path varchar(2000) | start_line int | end_line int | content TEXT
  language varchar(50)? | embedding Vector(1536)? | meta JSONB | created_at
  IX (workspace_id, file_path)
```

### Ordering rules

- Tasks listed per session: `created_at ASC, id ASC`.
- Messages: `ordinal ASC, created_at ASC, id ASC`. The orchestrator assigns
  ordinals starting at `MessageRepository.max_ordinal(session_id) + 1`
  (max is `-1` for an empty session, so the first message is ordinal `0`).
  This is required because a task persists all its messages in a single
  flush → identical `created_at`.

### Migrations

| Rev | Change |
|---|---|
| 0001 | Enable pgvector |
| 0002 | Core schema: users, workspaces, sessions, tasks, messages, code_chunks, enums |
| 0003 | `messages.tool_call_id`, `messages.tool_calls` (JSONB) |
| 0004 | `messages.ordinal` int + index |
| 0005 | `tasks.attempt`, `tasks.max_attempts` (retry/checkpoint metadata) |

## 3. Class design

### 3.1 Core composition root — `app/core/container.py`

```
@dataclass Container
  settings: Settings
  engine: AsyncEngine
  session_factory: async_sessionmaker[AsyncSession]
  redis: Redis
  event_broker: EventBroker = field(default_factory=EventBroker)
  cancellations: CancellationRegistry = field(default_factory=CancellationRegistry)
  _orchestrator: Orchestrator | None = field(init=False, default=None, repr=False)

  @classmethod build(settings=None) -> Container     # engine + session factory + redis
  async aclose() -> None                             # dispose engine, close redis (idempotent)
  orchestrator() -> Orchestrator                     # lazy: build_llm_client(settings)
                                                     #   on first call; cached in _orchestrator
```

Lazy LLM construction means `Container.build()` never requires LLM
credentials at startup; the failure surfaces only when a task is run.

### 3.2 Settings — `app/core/config.py`

`Settings(BaseSettings)`; env-driven, `.env` file, case-insensitive,
`extra="ignore"`. Key namespaces: `database_*`, `redis_*`, `sandbox_*`,
`executor_*`, `llm_*` (see §6 for the full table). Validated by Pydantic
fields (`ge`/`le` ranges), cached via `get_settings()` (lru_cache).

### 3.3 Repositories

```
BaseRepository[ModelT: Base, IdT]  (AsyncSession)
  async get(id) -> ModelT | None
  async add(entity) -> ModelT          # flush + RETURNING population
  async delete(entity) -> None

TaskRepository
  async get_for_run(task_id) -> Task | None   # select + joinedload(session.workspace)
                                              # (eager, to avoid async lazy-load)
  async list_by_session(session_id, limit=100, offset=0) -> Sequence[Task]
  async list_children(parent_task_id) -> Sequence[Task]

MessageRepository
  async max_ordinal(session_id) -> int        # -1 when empty
  async list_by_session(session_id, limit=1000, offset=0) -> Sequence[Message]
  async list_by_task(task_id, limit=1000, offset=0) -> Sequence[Message]
  async add_many(messages) -> None            # add_all + flush

SessionRepository
  async list_by_workspace(workspace_id, limit, offset) -> Sequence[Session]
  async count_by_status(workspace_id, status) -> int
```

### 3.4 LLM layer — `app/llm/`

```
class ChatRole(StrEnum): SYSTEM|USER|ASSISTANT|TOOL   # values: system|user|assistant|tool

class ToolRequest(BaseModel): id: str = uuid4-hex | name: str | arguments: dict = {}

class ChatMessage(BaseModel):
  role: ChatRole | content: str = "" | tool_call_id: str|None | tool_requests: list[ToolRequest]

@runtime_checkable class LLMProvider(Protocol):
  name: str; model: str
  async complete(messages, *, tools, system=None, max_tokens, temperature) -> LLMResponse
  stream(...) -> AsyncIterator[LLMStreamEvent]

class LLMResponse(BaseModel): content | tool_requests | stop_reason | usage | model
class LLMStreamEvent(BaseModel): type: text|tool_request|usage | text? | tool_request? | usage? | model
class LLMUsage(BaseModel): input_tokens | output_tokens

build_llm_client(settings) -> LLMProvider      # "anthropic" | "openai" else ValueError
```

**AnthropicClient** (`clients/anthropic.py`):
- system passed as top-level `system`; `SYSTEM`-role entries in messages are
  skipped; TOOL → `tool_result` user block (`tool_use_id`).
- `complete()`: `client.messages.create(...)`, maps `TextBlock`→content,
  `ToolUseBlock`→ToolRequest, stop_reason via `_STOP_REASON_MAP`
  (`end_turn`, `tool_use`, `max_tokens`, `stop_sequence→end_turn`), usage
  from `response.usage.{input,output}_tokens`.
- `stream()`: `client.messages.stream(**kwargs)`; per `TextEvent` yield
  `text`; on message → tool_request events; final → `usage` event.

**OpenAIClient** (`clients/openai.py`):
- system → leading `system` message; assistant tool_calls with
  `function.arguments = json.dumps(args)`; TOOL → `tool` message with
  `tool_call_id`.
- constructor resolves key: `api_key → OPENAI_API_KEY env → "local"` (the
  placeholder is only used when `base_url` is set, because the SDK raises at
  construction without a key and local backends ignore auth).
- `complete()`: `client.chat.completions.create(...)`; `_FINISH_REASON_MAP`
  (`stop→end_turn`, `tool_calls→tool_use`, `length|content_filter→max_tokens`);
  `_parse_arguments()` returns `{}` on non-dict/invalid JSON.
- `stream()`: accumulate per-chunk `delta.content` and `delta.tool_calls`
  slots keyed by `index`; final chunk yields usage; emits `text`,
  `tool_request`, `usage` events.

### 3.5 Agents — `app/agents/`

**Shared loop — `base.py`**

```
LoopState(TypedDict, total=False)
  goal: str | messages: Annotated[list[ChatMessage], add] | step: int
  max_steps: int | continue_loop: bool | final_answer: str
  input_tokens: Annotated[int, add] | output_tokens: Annotated[int, add]

LoopResult(answer, messages, input_tokens, output_tokens, steps)

RunResult(Protocol)  # answer | messages | input_tokens | output_tokens
AgentLike(Protocol)  # async run(goal) -> RunResult

LoopAgent(llm, executor, system_prompt, max_steps=8, max_tokens=4096,
          temperature=0.0, on_message=None, should_cancel=None)
  async run(goal, initial_messages=()) -> LoopResult
    # seeds ChatMessage(USER, goal) + initial_messages, streams all of them
    # through on_message, then delegates to run_from(seeded)
  async run_from(messages) -> LoopResult
    # runs the loop over an existing conversation WITHOUT re-emitting it via
    # on_message — composed pipelines pass each stage the accumulated
    # transcript and append only the delta
  # graph: START → step(_step) → {continue: step, end: END} via _route
  async _step(state) -> dict   # LLM call; branches on tool_requests
  async _execute_tool(request) -> ChatMessage   # unknown tool → TOOL msg
  _route(state) -> "continue" | "end"
  _invoke_on_message(message)   # fires on_message (sync or async) for every
                                # produced assistant turn and tool result
format_tool_result(result) -> str   # output; "[error] detail"; "(tool X returned no output)"
```

**Coder — `coder.py`**

```
CoderAgent = LoopAgent subclass; same constructor, defaults to the coder
system prompt ("…make focused edits, run commands, and commit your work…").
CoderResult = LoopResult  (alias kept for compatibility)
```

**Pipeline — `pipeline.py`**

```
PipelineState(TypedDict, total=False)
  goal: str
  messages: Annotated[list[ChatMessage], add]      # one shared transcript
  step: Annotated[int, add]                        # total LLM calls, all stages
  max_steps: int                                   # per-stage loop bound
  pass_count: int | max_passes: int                # rework counter / bound
  plan: str | feedback: str | final_answer: str
  input_tokens/output_tokens: Annotated[int, add]

PipelineResult(answer, messages, input_tokens, output_tokens, steps, passes)

parse_verdict(answer) -> bool   # True iff first line contains "PASS"

PipelineAgent(llm, executor, max_passes=2, max_steps=8, max_tokens=4096,
              temperature=0.0, on_message=None, should_cancel=None)
  async run(goal) -> PipelineResult   # emits goal via on_message, then graph
  # graph: START → planner → coder → reviewer →_route_review→
  #            {coder, tester, end}; tester →_route_test→ {coder, end}
  # nodes run one stage each; _run_stage spins up a fresh LoopAgent over the
  #   accumulated messages (run_from) and returns only the delta
  # reviewer/tester set feedback + final_answer; non-PASS increments pass_count
  _route_review(state) -> "coder" | "tester" | "end"
  _route_test(state)   -> "coder" | "end"
```

Loop semantics: `operator.add` reducers accumulate `messages`, `step`, and
token counts across stages; each stage's LLM call sees the running
conversation (transcript grows monotonically). A `PASS` reviewer routes to
the tester; a `PASS` tester ends. A `CHANGES_NEEDED`/`FAIL` verdict routes
back to the coder while `pass_count < max_passes`, otherwise the pipeline
ends with the latest verdict — guaranteed termination. The final answer is
the last stage's message. `should_cancel` is checked inside every stage's
steps and raises `TaskCancelled`, so cooperative cancellation spans the whole
pipeline.

### 3.6 Orchestrator — `app/orchestrator/orchestrator.py`

```
EventKind = Literal["started","message","completed","failed","cancelled"]
OrchestratorEvent(task_id, kind, message: ChatMessage|None = None,
                  detail: str|None = None, ordinal: int|None = None)
  # frozen dataclass; "message" events carry the persisted entry + its ordinal

_TERMINAL_STATUSES = {completed, failed, cancelled}

Orchestrator(session_factory, llm, settings, on_event=None,
             event_broker=None, cancellations=None, executor_factory=None)
  async run_task(task_id) -> Task
  #   1. get_for_run(for_update=True)  # row lock → status guard is atomic
  #        else ValueError; if terminal → return unchanged (noop for retries);
  #        if RUNNING → ValueError ("task already running")
  #   2. status=RUNNING; attempt+=1; started_at=now; commit   # durable checkpoint
  #   3. emit "started"
  #   4. executor = executor_factory(workspace_dir) | ToolExecutor.build(...)
  #   5. ordinal_base = max_ordinal(session)   # -1 for an empty session
  #   6. _append(msg): ordinal_base+=1; persist Message(ordinal); commit;
  #        emit "message" event          ← agent.on_message hook
  #   7. agent = _build_agent(task, executor, _append)
  #        coder → CoderAgent | pipeline → PipelineAgent (max_passes=…)
  #        unknown agent_type → ValueError (surfaced as task FAILED)
  #   8. try: result = await agent.run(task.goal)
  #        except TaskCancelled → _cancel
  #        except Exception     → _fail
  #   9. if cancellation requested → _cancel
  #  10. status=COMPLETED; result=answer; tokens; finished_at; commit; emit "completed"
  async retry_task(task_id) -> Task
  #   get_for_run(for_update=True); must be terminal else ValueError;
  #   attempt >= max_attempts → ValueError; else status=PENDING, clear
  #   result/error/tokens/timestamps, commit, discard cancel state
  _fail(session, task, exc) -> Task   # cancelled? → _cancel; else FAILED+error+emit "failed"
  _cancel(session, task) -> Task      # CANCELLED (keep prior transition) + emit "cancelled"
  _persist_message(session, task, message, ordinal) -> None   # + commit
  _serialize_tool_calls(requests) -> list[dict]|None    # model_dump per request
  _emit(event) -> None   # publish to event_broker (best-effort), then on_event
```

Semantics: failed runs are **not raised** — the task carries the error so
polling clients get a uniform story. The transcript is persisted **per
message with an immediate commit** (rather than batched at the end), so each
entry is durably visible to concurrent readers while the task is still
running; a failure therefore keeps the partial transcript. `MessageRole(message.role.value)`
bridges ChatRole → MessageRole (same values).

Retry/cancel semantics:
- `attempt` is incremented and committed with each RUNNING transition; status
  + transcript are the durable checkpoint, so a crash mid-run is never
  silently lost and `run_task` can be re-invoked safely (terminal → noop).
- `retry_task` keeps the prior transcript (new attempts append ordinals after
  it) and only clears the terminal columns; it is bounded by `max_attempts`.
- Cancellation is cooperative: the cancel endpoint persists `cancelled`
  immediately (crash-safe) and sets the registry flag; the agent checks
  `should_cancel` at each step boundary and raises `TaskCancelled`, which
  `run_task` turns into the final `cancelled` transition (keeping any that
  the endpoint already persisted).

### CancellationRegistry — `app/orchestrator/cancellation.py`

```
TaskCancelled(Exception)          # raised between agent steps on cancel
CancellationRegistry              # in-process stand-in for a distributed flag
  _events: dict[uuid.UUID, asyncio.Event];  _lock: asyncio.Lock
  async reset(task_id)            # drop a prior request for a fresh run (retry)
  async request_cancel(task_id)   # create (if absent) + set the event
  is_requested(task_id) -> bool   # sync; read by the agent's should_cancel hook
  async discard(task_id)          # release state at a terminal status (idempotent)
```

One shared instance lives on the `Container`; the orchestrator reads it and
the cancel route writes it via `CancellationRegistryDep`. Like `EventBroker`,
it is the single-process stand-in for a distributed signal.

### EventBroker — `app/orchestrator/broker.py`

```
EventBroker  # in-process pub/sub, the single-process stand-in for Redis
  _subscribers: dict[uuid.UUID, set[asyncio.Queue[OrchestratorEvent]]]
  _lock: asyncio.Lock
  async subscribe(task_id) -> asyncio.Queue       # new queue for the task
  async unsubscribe(task_id, queue) -> None       # idempotent; cleans empty sets
  async publish(event) -> None  # put_nowait to each subscriber; QueueFull dropped
```

One shared instance lives on the `Container` and is passed to the
orchestrator and exposed to routes via `EventBrokerDep`. Publishing is
best-effort so a lagging subscriber never blocks the agent.

### 3.7 Gateway

```
dependencies.py
  get_container(request) -> Container
  async get_db_session(container) -> AsyncIterator[AsyncSession]
  get_event_broker(container) -> EventBroker          # shared broker
  get_cancellation_registry(container) -> CancellationRegistry
  get_orchestrator(container) -> Orchestrator   # 503 "LLM is not configured" on any build error
  ContainerDep / SessionDep / EventBrokerDep / CancellationRegistryDep / OrchestratorDep

routes/tasks.py
  async get_session_or_404(session_id, db) -> Session    # 404 dependency
  POST /sessions/{session_id}/tasks   → 202 TaskResponse
       body TaskCreateRequest{goal 1..4000, agent_type default "coder"}
       create task (PENDING, max_attempts=settings.task_max_attempts) → commit →
         BackgroundTasks.add_task(orchestrator.run_task, task.id) → return
  GET  /sessions/{session_id}/tasks   → list[TaskResponse]  (limit 1..500, offset ≥ 0)
  GET  /tasks/{task_id}               → TaskDetailResponse (task + ordered transcript)
  POST /tasks/{task_id}/retry         → 202 TaskResponse
       must be terminal (completed/failed/cancelled) and attempt < max_attempts
       (else 409); orchestrator.retry_task resets to PENDING (keeps transcript) →
         BackgroundTasks.add_task(run_task, task.id) → return (db.refresh)
  POST /tasks/{task_id}/cancel        → 200 TaskResponse
       terminal → 409; else status=CANCELLED + finished_at commit,
       cancellations.request_cancel(task_id), return (db.refresh)
  GET  /sessions/{session_id}/tasks/{task_id}/events → text/event-stream
       replay (snapshot on status change + persisted messages) then live,
       15s keepalive comments, closes on a terminal status (completed/failed/
       cancelled). DB is the source of truth; broker events only wake the loop.

schemas.py
  TaskCreateRequest; TaskResponse(from_attributes, + attempt/max_attempts);
  MessageResponse(from_attributes);
  TaskDetailResponse(TaskResponse + messages: list[MessageResponse])
```

SSE frame format: `event: snapshot|message|completed|failed|cancelled\n` +
`data: {json}`. `snapshot`/terminal frames carry a full `TaskResponse`;
`message` frames carry a full `MessageResponse`. Headers: `text/event-stream`,
`Cache-Control: no-cache`, `X-Accel-Buffering: no`.

Subtle behaviors (enforced by tests):
- `get_session_or_404` is declared **before** `OrchestratorDep` so a missing
  session returns `404` even when the LLM is unconfigured (dependencies
  resolve in declaration order; otherwise 503 would shadow 404).
- The orchestrator runs in the background with its own session factory, so
  the request-scoped `db` session is never shared across the response
  boundary; `expire_on_commit=False` keeps the created task readable after
  the commit.

### 3.8 Tools — `app/tools/`

```
ToolName(StrEnum): FILE_READ|FILE_WRITE|FILE_LIST|FILE_SEARCH|FILE_DELETE|FILE_MOVE
                  |TERMINAL_RUN|GIT_STATUS|GIT_DIFF|GIT_COMMIT
  # member values are the wire identifiers: "file_read", …, "git_commit"

ToolCall(id=uuid4, tool: ToolName, arguments: dict, timeout_ms: int|None ge=100 le=3_600_000)
ToolResult(call_id, tool, ok, output="", error=None, exit_code=None, duration_ms=None, truncated=False)
ToolSpec(name, description, arguments_schema)

Handler = Callable[[ToolCall, BaseModel], Awaitable[ToolResult]]

ToolRegistry
  register(name, description, arguments_model, handler)
    # builds ToolSpec via model_json_schema(); validator = TypeAdapter(arguments_model)
  unregister(name); __contains__; names -> frozenset[ToolName]
  specs() -> list[ToolSpec]                 # LLM-facing catalog
  async execute(call) -> ToolResult
    # unknown tool / ValidationError / handler exception → ok=False result (never raises)
    # sets duration_ms
```

Tool specs: filesystem (read max_bytes, write, list recursive/max_depth,
search glob/case-sensitive/max_results, delete recursive, move), terminal
(`sh -c` script, workdir, timeout), git (diff ref, commit message/
allow-empty). `ARGUMENT_MODELS`/`ALL_SPECS` aggregated in `specs/__init__.py`.

### 3.9 Executor

```
paths.py
  PathTraversalError(ValueError)
  resolve_within(root, *parts) -> Path   # realpath containment, symlink-safe

executor.py  ToolExecutor(workspace_dir, registry, sandboxes, mount_target="/workspace",
                          default_timeout_ms=30_000)
  @classmethod build(workspace_dir, settings, sandboxes=None)
  registry / sandboxes  (properties)
  async execute(call) -> ToolResult          # delegates to registry
  _register()  # wires all 10 tools to handlers
  # fs/git handlers run on host via asyncio.to_thread / create_subprocess_exec
  #   (git: GIT_TERMINAL_PROMPT=0, GIT_PAGER=cat, GIT_CONFIG_NOSYSTEM=1, hooksPath=/dev/null)
  # terminal → sandbox.get_or_start(workspace).run(script, timeout_ms, workdir)
  # MAX_OUTPUT_CHARS = 200_000 truncation

sandbox.py
  SandboxLimits(memory_mb=512, cpu_nanos=1e9, network_enabled=False)  # .memory_bytes
  SandboxOutput(exit_code|None, stdout, stderr, timed_out=False)
  Sandbox(container, on_destroy=None)       # per-container asyncio.Lock (commands serialized)
    async run(script, *, timeout_ms, workdir=None, on_chunk=None) -> SandboxOutput
      # timeout KILL wrapper + outer async guard (+10s) that kills container
      #   and triggers on_destroy on timeout; DockerError → sandbox error output
  SandboxManager(image, limits, mount_target="/workspace")
    async get_or_start(workspace_dir) -> Sandbox   # lazy per realpath key
    async stop(workspace_dir); async close()
    _container_config(key) -> dict
      # Cmd ["sleep","infinity"], User=host uid:gid, Tmpfs /tmp size=128m,
      #   ReadonlyRootfs=True, NetworkMode none|default, Memory/NanoCpus,
      #   Binds [workspace:/workspace]
```

## 4. Sequence diagrams

### 4.1 Create + run task (async, streamed)

```
Client  Gateway  Orchestrator  CoderAgent  LLM  ToolExecutor  Sandbox  Postgres  Broker
  │ POST  │            │           │         │       │          │        │       │
  ├──────▶│ 404 if no session        │         │       │          │        │       │
  │       ├──── add Task(PENDING) ────────────────────────────────────────▶│       │
  │       ├──── commit ───────────────────────────────────────────────────▶│       │
  │       │   BackgroundTasks.run_task(task_id)     │         │          │       │
  │       │   (response sent; client opens SSE)     │         │          │       │
  │◀──────│  202 TaskResponse                       │         │          │       │
  │       │ run_task(task_id)                       │         │          │       │
  │       │──▶│ get_for_run(for_update=True) ───────────────────────────────▶│       │
  │       │   │ status=RUNNING; attempt+=1 ─────────────────────────────────▶│       │
  │       │   │ ordinal_base = max_ordinal ─────────────────────────────────▶│       │
  │       │   │ agent.run(goal) ──────────▶│         │          │         │       │
  │       │   │   on_message(goal)  ──────▶│ persist+commit (ordinal) ───▶│ emit ▶│
  │       │   │   complete(msgs, tools) ─────────▶│       │          │     │       │
  │       │   │   tool_requests ◀──────────────────│       │          │     │       │
  │       │   │   should_cancel? ──(no)──▶        │       │          │     │       │
  │       │   │   execute(ToolCall) ───────────────────▶│  get_or_start/run │     │
  │       │   │   on_message(assistant+tool) ───▶│ persist+commit ────────▶│ emit ▶│
  │       │   │   final answer ◀──────────────────│       │          │     │       │
  │       │   │ status=COMPLETED ─────────────────────────────────────────▶│       │
  │       │   │ emit completed ───────────────────────────────────────────▶│       │
  │       │ SSE stream: snapshot + messages + completed (from DB truth)           │
```

Cancel/retry flows:
```
Client  Gateway  CancellationRegistry  Orchestrator  CoderAgent  Postgres
  POST /tasks/{id}/cancel
  ├──▶│ (running?)  │         │             │            │        │
  │   │ status=CANCELLED + finished_at ───────────────────────────▶│ (commit)
  │   │ request_cancel(task_id) ──▶│(set flag)    │            │
  │   │◀── TaskResponse(cancelled) │         │             │            │
  │   │   (agent at next step boundary)         │            │        │
  │   │   should_cancel() ──▶│(is_requested → True)        │        │
  │   │   raise TaskCancelled ───▶│         │            │        │
  │   │   _cancel (keep CANCELLED) ──────────────────────────▶│ (commit)
  │   │   emit "cancelled" ───────────────────────────────────▶│
  POST /tasks/{id}/retry  (terminal + attempt < max_attempts)
  ├──▶│ status=PENDING; clear result/error/tokens/timestamps ──▶│ (commit)
  │   │ discard(task_id) ──▶│(clear cancel flag)  │            │
  │   │ BackgroundTasks.run_task(task_id) → PENDING→RUNNING (attempt+1)
  │   │◀── 202 TaskResponse(pending)      (transcript retained)
```

### 4.2 Terminal tool call (sandbox)

```
ToolExecutor._terminal_run
  → SandboxManager.get_or_start(workspace)   # create+start "sleep infinity" container
  → Sandbox.run(script, timeout_ms, workdir)
      wrapped = ["timeout","-s","KILL",secs,"sh","-c",script]
      async with container._lock:
        docker exec (stdout/stderr streamed, on_chunk callback)
        outer asyncio.wait_for(exec+inspect, timeout+10s)
      on outer TimeoutError → SIGKILL container + on_destroy (manager drops key)
  → ToolResult(ok = exit_code==0, output≤200k, error=stderr if !ok, timed_out flags)
```

## 5. Key algorithms

### 5.1 Coder loop (LangGraph)

1. Seed state: goal → `ChatMessage(USER, goal)` (+ optional initial messages),
   `step=0`, `continue_loop=True`, `max_steps`.
2. `_step`: `LLM.complete(messages, tools=registry.specs(), system, max_tokens, temperature)`.
3. If `response.tool_requests`: append assistant message (with requests),
   then for each request `_execute_tool` → append TOOL message; `step+1`;
   `continue_loop=True`. Token fields accumulate via `operator.add`.
4. Else: append assistant message with content; `final_answer=content`;
   `continue_loop=False`.
5. `_route`: continue only if `continue_loop and step < max_steps`.
6. Return `CoderResult(answer, messages, input_tokens, output_tokens, step)`.

### 5.2 Orchestrator lifecycle

`PENDING → RUNNING → COMPLETED | FAILED | CANCELLED`, plus
`started_at`/`finished_at`. Each RUNNING transition increments and persists
`attempt` (durable checkpoint). Failure is captured as
`"TypeName: message"` in `task.error`; the exception is not re-raised.
Success writes `result`, `input_tokens`, `output_tokens`.

Retry: `retry_task` moves a terminal task back to `PENDING` (clearing
`result`/`error`/token counts/timestamps, keeping the transcript) for
another `run_task`, guarded by `attempt < max_attempts`. A retry's messages
continue the session ordinals, so the transcript preserves every attempt.

Cancel (cooperative): the endpoint persists `CANCELLED` + `finished_at` and
sets the registry flag; the running agent's `should_cancel` check raises
`TaskCancelled` at the next step boundary; `run_task` finalizes via `_cancel`
(keeping an already-persisted `CANCELLED`).

### 5.3 Transcript persistence (incremental)

`ordinal_base = max_ordinal(session_id)` (max existing, `-1` if none); the
first message produced by a task gets `ordinal = ordinal_base + 1` and each
subsequent message increments from there. Rows carry `session_id`,
`task_id`, `role` (via `MessageRole(role.value)`), `content`, `tool_call_id`,
and `tool_calls = [req.model_dump() for req in tool_requests] or None`.
Each row is flushed **and committed** as it is produced, so it is durable
and visible to concurrent readers immediately — this is what lets SSE
clients replay a running task's transcript. The trade-off (a commit per
message) is acceptable at current message volumes.

### 5.6 SSE streaming (replay + wakeup)

```
_generator(task_id, db, broker):
  last_ordinal = -1; seen_status = None
  loop:
    current = TaskRepository.get(task_id)          # fresh read each pass
    messages = MessageRepository.list_by_task(task_id)
    if current.status != seen_status:
        seen_status = current.status
        yield snapshot(TaskResponse)
    for m in messages with ordinal > last_ordinal:
        yield message(MessageResponse); last_ordinal = m.ordinal
    if current.status is terminal (completed/failed/cancelled):
        yield current.status.value(TaskResponse); return
    db.commit()                                    # close the read snapshot
    await wait_for(broker queue, 15s) else yield ": keepalive"
finally: broker.unsubscribe(task_id, queue)
```

DB state is the source of truth; the broker queue only wakes the loop, so a
subscriber never misses or duplicates a message and reconnects replay the
DB, not the broker.

### 5.4 Path confinement

`resolve_within(root, *parts)` → `realpath(join(root_real, parts))`; reject
unless `candidate == root_real or candidate.startswith(root_real + sep)`.
Applied to every filesystem path and terminal `workdir` before I/O.

### 5.5 Token accounting

Summed across LLM calls (each coder/stage loop) and stored on the task for
per-task cost accounting.

## 6. Configuration reference

| Env var | Default | Notes |
|---|---|---|
| `APP_NAME` / `APP_ENV` / `DEBUG` / `LOG_LEVEL` / `JSON_LOGS` | `coding-agent` / `development` / `false` / `INFO` / `true` | app basics |
| `API_PREFIX` | `/api/v1` | route prefix |
| `DATABASE_URL` | `postgresql+asyncpg://coding:coding@localhost:5432/coding_agent` | async URL |
| `REDIS_URL` | `redis://localhost:6379/0` | |
| `EMBEDDING_DIMENSION` | `1536` | fixed; never change after inserts |
| `EXECUTOR_IMAGE` | `coding-agent-executor:latest` | sandbox image |
| `SANDBOX_MEMORY_MB` | `512` | ≥128 |
| `SANDBOX_CPU_NANOS` | `1_000_000_000` | 1e9 = 1 vCPU |
| `SANDBOX_NETWORK_ENABLED` | `false` | keep off in prod |
| `SANDBOX_DEFAULT_TIMEOUT_MS` | `30_000` | ≥100 |
| `LLM_PROVIDER` | `openai` | `anthropic` \| `openai` |
| `LLM_MODEL` | `gpt-4o-mini` | |
| `LLM_API_KEY` | *(empty)* | falls back to SDK env handling |
| `LLM_BASE_URL` | *(empty)* | set for vLLM/Ollama local backends |
| `LLM_MAX_TOKENS` | `4096` | ≥1 |
| `LLM_TEMPERATURE` | `0` | 0..2 |
| `LLM_TIMEOUT_SECONDS` | `120` | ≥1 |
| `TASK_MAX_ATTEMPTS` | `3` | retry budget per task (≥1) |
| `PIPELINE_MAX_PASSES` | `2` | rework rounds allowed in the multi-agent pipeline (≥0) |

## 7. API contract

| Method | Path | Success | Errors |
|---|---|---|---|
| GET | `/api/v1/healthz` | 200 `{"status":"ok"}` | – |
| GET | `/api/v1/readyz` | 200 `{status, checks:{database,redis}}` | 503 degraded |
| POST | `/api/v1/sessions/{session_id}/tasks` | 202 `TaskResponse` (status `pending`); runs in background | 404 session, 422 body, 503 LLM unconfigured |
| GET | `/api/v1/sessions/{session_id}/tasks?limit&offset` | 200 `[TaskResponse]` | 404 session |
| GET | `/api/v1/tasks/{task_id}` | 200 `TaskDetailResponse` | 404 task |
| POST | `/api/v1/tasks/{task_id}/retry` | 202 `TaskResponse` (status `pending`); reruns in background | 404 task, 409 not terminal / max attempts |
| POST | `/api/v1/tasks/{task_id}/cancel` | 200 `TaskResponse` (status `cancelled`) | 404 task, 409 terminal |
| GET | `/api/v1/sessions/{session_id}/tasks/{task_id}/events` | 200 `text/event-stream` (snapshot + messages + terminal) | 404 session, 404 task |

`TaskResponse`: `id, session_id, parent_task_id, agent_type, status, goal,
result, error, attempt, max_attempts, input_tokens, output_tokens,
started_at, finished_at, created_at, updated_at`. Supported `agent_type`
values are `coder` (default, single loop) and `pipeline` (multi-agent
pipeline); an unknown type fails the task with a captured
`ValueError: unsupported agent_type: …`.

`TaskDetailResponse` = TaskResponse + `messages: [{id, session_id, task_id,
role, content, ordinal, tool_call_id, tool_calls, token_count, created_at}]`
ordered by ordinal.

## 8. Error-handling matrix

| Layer | Strategy |
|---|---|
| Config | Pydantic validation at import/startup (fail fast) |
| Repository | `get()` returns `None`; no exceptions on missing rows |
| ToolRegistry | Unknown tool / invalid args / handler exception → `ToolResult(ok=False, error=…)`; never raises |
| Sandbox | Timeout → kill + container destroyed + `timed_out=True`; DockerError → `ok=False` sandbox error |
| Executor | Per-tool errors returned as results; terminal/git timeout → `ok=False` with message |
| CoderAgent | Unknown tool name → TOOL message `"unknown tool: …"`; loop always terminates; `should_cancel` → raises `TaskCancelled` |
| PipelineAgent | Stage loops inherit the coder loop's guarantees; non-PASS verdicts route to coder while `pass_count < max_passes`, else pipeline ends (always terminates) |
| Orchestrator | Agent/exceptions → `task.status=FAILED` + `error`; **not re-raised**; missing task → `ValueError`; running/terminal task → guarded (terminal = noop, running = `ValueError`); `retry_task` → `ValueError` on non-terminal/max-attempts; unknown `agent_type` → `ValueError` |
| CancellationRegistry | No-ops on unknown/terminal tasks (`reset`/`discard` idempotent); `is_requested` reads without raising |
| EventBroker | Best-effort publish: `QueueFull`/slow subscriber dropped, never raises |
| Gateway | 404 session/task, 409 retry/cancel conflicts, 422 validation, 503 LLM unconfigured (dependency), 202/200 otherwise; SSE closes on disconnect via `finally` unsubscribe |

## 9. Test strategy

- **Unit** (no infra): config, model round-trips, path confinement, filesystem
  tool logic, registry validation, LLM message/adapters/factory (with stub
  SDKs), coder agent and pipeline agent (FakeLLM + stub executor, incl.
  `on_message` hooks, verdict parsing, and rework routing/bounds), event
  broker pub/sub. `make test-unit`.
- **Sandbox**: real Docker execution (requires `make executor-image`).
- **Integration** (requires `make up`): repositories, health/readiness,
  orchestrator against real Postgres + FakeLLM + stub executor (incl.
  broker-published message events, partial transcripts on failure, pipeline
  runs, and unsupported-`agent_type` failures), tasks API via ASGI client
  with `app.dependency_overrides[get_orchestrator]` (async `202` create +
  SSE replay/live streams).
- Tooling: `make lint` (ruff), `make typecheck` (mypy strict, pydantic +
  sqlalchemy plugins), `make test`.

Fixtures: `tests/conftest.py` (`settings` → local infra), `FakeLLM`
(scripted responses, records `.calls`), integration `container`/`db_session`
fixtures, autouse table truncation.

## 10. Extension points

- **New tool**: Pydantic args model in a `specs/` module + `SPECS`/`MODELS`
  entry + handler in `ToolExecutor._register`.
- **New LLM provider**: implement `LLMProvider`, add a branch in
  `build_llm_client`, add config keys.
- **Streaming to clients**: the broker already fans out `message` events;
  swap `EventBroker` for Redis pub/sub (same interface) if the gateway and
  orchestrator are split, or add per-LLM-call token/text events.
- **Per-stage isolation / subtask trees**: stages already compose the shared
  `LoopAgent`; to isolate contexts or parallelize work, spawn real child
  `Task` rows (`Task.parent_task_id` already models the tree) and schedule
  them from the pipeline, mirroring `run_task`.
- **Automatic retries**: wire a bounded retry loop over `Orchestrator.run_task`
  for transient (retryable) failures — deferred because a terminal FAILED
  closes the SSE stream; add a retryable-error taxonomy + backoff when
  non-terminal retry markers are wanted.
- **Distributed cancel/signal**: swap `CancellationRegistry` for a shared
  store (Redis) with the same interface when the gateway and orchestrator
  split.
- **Retrieval**: `CodeChunk` + pgvector are already in the schema.
