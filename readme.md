# jb-ai-orchestrator

**A deployable agent harness.** Point it at a source holding an AI playbook —
`.cursor/`, `.claude/`, or a plain `AGENTS.md` — and it provisions an isolated
workspace, discovers the instructions, rules, skills and subagents inside, and
runs a full tool loop against any model LiteLLM can reach. It is an HTTP
microservice, so one deployment serves many callers, and a paused run can be
resumed by a different replica.

Python 3.11 · FastAPI · Apache-2.0

Designed and built by [Joseph Benraz](https://www.linkedin.com/in/josephbenraz)

## Contents

- [What makes it different](#what-makes-it-different)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Features](#features)
- [How it compares](#how-it-compares)
- [API reference](#api-reference)
- [Configuration](#configuration)
- [Workspaces, input and durable output](#workspaces-input-and-durable-output)
- [Running many instances](#running-many-instances)
- [Development](#development)
- [Limitations and open work](#limitations-and-open-work)
- [Documentation](#documentation)
- [License](#license)

## What makes it different

Agent harnesses usually ship as a CLI or an IDE feature that runs on one
developer's machine. This one is a service, which changes what you can build on
it:

- **The playbook is an input, not a build artifact.** Callers hand over a source
  location in the `folder` field: a git URL, an HTTP(S) URL to a `.zip`/`.tar.gz`
  archive — including a blob or bucket object, provided it points straight at the
  file — or a path the service can reach. The service detects which layout it is
  looking at, discovers the [primitives](docs/ORCHESTRATOR.md#1-concepts) inside —
  rules, skills, commands and subagents — and composes the system prompt from what
  it finds; `initiate` returns that catalog. Nothing is inlined by the caller and
  nothing is baked into the image.
- **Pauses survive the process.** When a run needs a human — a UI tool, the
  built-in `ask_user`, or a hook asking permission — it persists its state to
  Postgres and returns. Any replica can resume it. There are no sticky sessions
  and no in-process waiters.
- **Human interaction is a protocol, not a prompt.** The service speaks
  [AG-UI](https://github.com/ag-ui-protocol/ag-ui) over SSE, streams tokens and
  tool calls, and accepts batch interrupt resolution, so a UI can resolve or
  cancel several open interrupts in one request.
- **The UI can push its own tools.** Frontend tool catalogs are supplied per run,
  so many applications can share one deployment without clobbering each other's
  tool lists.
- **Many MCP servers, unambiguous names.** Every tool is suffixed with the name
  of the server that provides it (`search_google`, `weather_general`), so
  collisions across servers are impossible and every call is traceable.
- **Provider-neutral.** Model choice is a per-request string resolved by LiteLLM.
  Nothing in the harness is tied to one vendor.

## Quick start

**Prerequisites:** Docker Desktop, or — if you bring your own infrastructure —
Postgres 16+ and a [LiteLLM](https://docs.litellm.ai/) endpoint reachable from
the service. At least one MCP server is needed either way.

Configuration is per environment: `ENV` selects the directory under `src/.env/`,
and that directory's `.env` is the only one loaded. Docker and local runs
therefore read **different files**, so follow one path below, not both.

### 1a. Docker, with the shared stack

The shared stack deploys what the agent depends on — Postgres, LiteLLM and Ollama
— as its own Compose project, so the agent joins infrastructure instead of
carrying it. Copy the compose-layer example, then start both:

```bash
cp .env.example .env      # host ports, POSTGRES_*, LITELLM_MASTER_KEY, provider keys
./start-shared-mac.sh     # macOS: Ollama stays on the host for the Metal GPU
./start-agent-mac.sh      # or ./start-agent-mac.sh --ensure-shared to do both
```

On Windows use `start-shared-windows.bat` and `start-agent-windows.bat`; there
Ollama runs in a container and the default model is pulled for you.

The launcher never deploys infrastructure. It joins a running `jb-ai-shared`
stack, and if that is not up it stops and prints the command to run.
Since the agent then reaches services by name, `docker-compose.stack.yml`
overrides `DATABASE_URL`, `LITELLM_BASE_URL` and `LITELLM_API_KEY`; every other
setting still comes from `src/.env/docker/.env`.

Two environment files, two jobs: `.env` at the root configures the stack itself
and is git-ignored, while `src/.env/docker/.env` configures the agent process.

### 1b. Docker, against your own Postgres and LiteLLM

The container sets `ENV=docker`, and Compose loads `src/.env/docker/.env`. Edit
that file — it already points at `host.docker.internal` to reach Postgres,
LiteLLM and MCP servers on your host:

```env
DATABASE_URL=postgresql+asyncpg://postgres:<password>@host.docker.internal:5432/pyagent
LITELLM_BASE_URL=http://host.docker.internal:4000
LITELLM_API_KEY=<your-litellm-key>
MCP_SERVER_URLS=[{"name": "local", "url": "http://host.docker.internal:8001/mcp"}]
```

```bash
docker compose up --build -d
```

`./start-agent-mac.sh --no-shared` (or `start-agent-windows.bat --no-shared`) runs
exactly this, skipping shared-stack detection — the mode to use when Postgres and
LiteLLM are managed elsewhere.

> `src/.env/docker/.env` is currently tracked in this repository and ships with
> populated values. Treat it as your own local configuration, replace those
> values, and do not commit your credentials. It is being converted to a
> placeholder example.

### 1c. Local, without Docker

Create the localhost environment file from the tracked example and fill in the
values marked `CHANGE_ME`:

```bash
cp src/.env/localhost/.env.example src/.env/localhost/.env
```

```env
DATABASE_URL=postgresql+asyncpg://postgres:<password>@localhost:5432/pyagent
LITELLM_BASE_URL=http://localhost:4000
LITELLM_API_KEY=<your-litellm-key>
MCP_SERVER_URLS=[{"name": "local", "url": "http://localhost:8001/mcp"}]
```

```bash
pip install -r src/requirements.txt
ENV=localhost alembic upgrade head
cd src && ENV=localhost python -m app.main
```

Run `alembic` from the repository root, where `alembic.ini` lives; `ENV` tells it
which `src/.env/<env>/.env` to read `DATABASE_URL` from. On Windows PowerShell,
set `$env:ENV='localhost'` once, then run `alembic upgrade head` and
`cd src; python -m app.main`.

Either way, `DATABASE_URL` must use the `postgresql+asyncpg://` scheme, not plain
`postgresql://`, and `MCP_SERVER_URLS` must be an array of `{name, url}` objects.
A bare list of URL strings is ignored rather than rejected, so unless you also set
the numbered `MCP_SERVER_URL_{n}` variables, startup fails with no servers
configured. [MCP servers](#mcp-servers) below covers each malformed shape.

### 2. Verify

```bash
curl http://localhost:8000/isalive
```

Interactive API docs are at [http://localhost:8000/swagger](http://localhost:8000/swagger).
List the tools your MCP servers actually exposed, then run a request:

```bash
curl http://localhost:8000/v1/agent/getTools
```

```bash
curl -X POST "http://localhost:8000/v1/agent/executeRequest" \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Search for technology updates and check the weather",
    "tools": ["search_google", "weather_general"],
    "model": "gpt-4o"
  }'
```

> Tool names are whatever your servers expose, suffixed with the server name —
> replace the `tools` above with names from `/getTools`. Omit `tools` entirely to
> let the agent use everything available.

### Database schema

The container image runs `alembic upgrade head` before serving, which is why the
Docker path above has no migration step. Running outside Docker you apply them
yourself, as in the local sequence above:

```bash
ENV=localhost alembic upgrade head
```

Startup also calls `init_db()`, which creates the database if it is absent and
issues `create_all`, so an empty database works without a manual step. The
migrations additionally adopt databases that were first created by `create_all`.
See [docs/ORCHESTRATOR.md](docs/ORCHESTRATOR.md) §12 for migration and backfill
detail, and §13 for the schema itself.

## How it works

```mermaid
flowchart LR
    A[Caller] -->|folder + prompt| B[Bind workspace]
    B --> C[Scan playbook<br/>AGENTS.md · rules · skill catalog]
    C --> D[Compose system prompt]
    D --> E[Tool loop]
    E -->|local · MCP · frontend tools| E
    E -->|needs a human| F[(Persist state<br/>Postgres)]
    F -->|resume, any replica| E
    E -->|SSE tokens + tool calls| A
```

A run binds a workspace, scans it once per segment, composes an authoritative
system prompt from the discovered instructions plus a lazily-loaded catalog of
skills, commands and subagents, then loops: the model picks tools, the service
executes them, oversized results are offloaded to `.agent/offload/`, and older
history is summarized and compacted as the context grows. If a tool needs user
input the loop persists and returns rather than blocking.
[docs/ORCHESTRATOR.md](docs/ORCHESTRATOR.md) is the source of truth for every
step.

## Features

- **Harness discovery** — Cursor, Claude Code and generic adapters; eager loading
  of `AGENTS.md` (including `.cursor/AGENTS.md`), `CLAUDE.md` and always-on rules,
  with progressive disclosure for skills, commands and subagents. Project hooks
  run when enabled, and a `permission: ask` hook surfaces as an interrupt. An
  adapter is two methods — score a detection, return a manifest — over shared
  primitive discovery, so covering another tool's layout is around a hundred
  lines rather than a new subsystem.
- **Built-in coding tools**, namespaced `*_local` on workspace-backed runs:
  `list_files`, `read_file` (text plus images and PDFs), `write_file`,
  `edit_file`, `create_file`, `create_folder`, `glob`, `grep`, `execute`,
  `write_todos`, `task` for subagents, git helpers and `ask_user`. Binding
  durable output adds `write_output_local`, `edit_output_local`,
  `read_output_local` and `list_output_local`.
- **Context management** — token streaming, oversized tool results offloaded to
  disk and referenced by handle, LLM summarization of older turns, and tool
  message compaction on a character or token budget.
- **Human interaction** — AG-UI SSE runs, per-run frontend tool catalogs,
  canonical batch `resume[]` with resolve and cancel, a fan-out event stream, and
  Postgres-persisted holds resumable from any replica.
- **Multiple MCP servers** — named servers aggregated into one tool list with
  universal attribution, per-server fault isolation, and optional filtering of
  remote tools that collide with a built-in.
- **Operational safety** — path and shell allow/deny policy, workspace root
  restriction, size caps and clone timeouts, close endpoints with a
  **200**/**202** contract, run heartbeats and stale-run reconciliation.

## How it compares

The harness follows patterns established by
[Claude Code](https://code.claude.com/docs/en/features-overview) and
[LangChain Deep Agents](https://docs.langchain.com/oss/python/deepagents/harness):
root instructions and always-on rules for stable context, progressive disclosure
so skills cost a catalog entry until they are needed, subagents to keep detailed
work out of the parent context, and filesystem tools plus summarization and
large-result offload for long-running work. None of that is unique, and neither
is connecting several MCP servers at once.

What differs is the delivery model:

| | Claude Code | Cursor agent¹ | Deep Agents | This service |
|---|---|---|---|---|
| Form factor | CLI / IDE session | IDE / local agent | Python library you embed | HTTP microservice |
| Instruction source | `CLAUDE.md`, skills | `.cursor/rules`, `AGENTS.md` | Code you write | Any bound folder, scanned per run |
| Who supplies the playbook | The developer at the keyboard | The developer at the keyboard | The application author | The caller, per request |
| Multiple MCP servers | Yes | Yes | Via adapters | Yes, with universal name attribution |
| Human-in-the-loop | Interactive prompts | In-editor approvals | Framework interrupts | AG-UI protocol over SSE, batch resolve/cancel |
| Resume after restart | Local session resume | Local session resume | Application-selected checkpointer or store | Built in: Postgres-backed, any replica, no sticky sessions |
| UI-supplied tools | — | — | Application-defined | Per-run frontend tool catalog |
| Model routing | Anthropic models | Multiple providers | Your choice | Any LiteLLM model, per request |

Durable resume is not the distinguishing part — Deep Agents runs on LangGraph,
whose checkpointers and stores can be external and durable, and its hosted server
persists state too. The difference is that here it is not something you assemble:
the HTTP resume contract, the ownership rules that let a different replica claim a
paused run, and the persistence behind them ship as the service's public
behaviour.

¹ Cursor is an IDE product and this is a deployable service, so this compares
delivery models rather than feature parity — it consumes the same playbook
formats (`.cursor/`, `AGENTS.md`) that Cursor users already write. All four
projects move quickly; check their own documentation for current behaviour.

## API reference

Interactive documentation is served at `/swagger`.

### Health

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/isalive` | Health check |

### Agent

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/agent/executeRequest` | Execute a request with optional MCP and AG-UI tools |
| `GET` | `/v1/agent/getTools` | List available MCP tools (plus legacy globally-bound frontend tools) |
| `POST` | `/v1/agent/resumeRun` | Resume a paused execution with an AG-UI tool result |
| `GET` | `/v1/agent/execution/{executionGuid}` | Latest execution status and result metadata |

### Orchestrator

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/orchestrator/initiate` | Bind a folder to a new session, provision the workspace, detect the orchestration type, return `orchestratorGuid` plus discovered agents and skills. No LLM call. |
| `POST` | `/v1/orchestrator/execute` | Run a prompt against an initiated session. Returns the result, or `awaitsResponse` plus `stateGuid` when input is needed. Send the complete current `frontendTools` list: their names are named in the system prompt so the model knows an interaction tool exists. |
| `POST` | `/v1/orchestrator/resume` | Continue an awaiting run with `{ orchestratorGuid, stateGuid, toolCallId, result }`. Pod-agnostic. |
| `GET` | `/v1/orchestrator/{orchestratorGuid}` | Session and run status. While awaiting input, also replays canonical `interrupts` and `pendingToolCallIds` from persisted state so authorized callers can rebuild interaction UI. |
| `POST` | `/v1/orchestrator/{orchestratorGuid}/close` | Close the session. **200** `closed` or **202** `closing`; retry after active runs finish or go stale. Removes service-owned runtime and workspace; never touches in-place caller input or durable output. |

### AG-UI runtime

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/ag-ui/run` | SSE run: bind `frontendTools`, stream output, receive tool-call callbacks |
| `GET` | `/api/ag-ui/events` | Fan-out SSE stream of all tool-call events |
| `POST` | `/api/ag-ui/threads/{threadId}/close` | Close thread lifecycle. **200** `closed` or **202** `closing`. Cancels holds and removes thread runtime and AG-UI-owned sandboxes only. |
| `POST` | `/api/ag-ui/threads/{threadId}/abandon` | Idempotently discard active holds. Non-destructive; leaves runtime and workspaces in place. |

`/api/ag-ui/events` emits the same `TOOL_CALL_START`, `TOOL_CALL_ARGS` and
`TOOL_CALL_END` payloads the AG-UI runtime produces, for every run rather than
one caller's. Multiple subscribers are supported, each holding its own SSE
connection and filtering client-side on `toolCallName` or `toolCallId`. This is
how a service that drives `/v1/agent/executeRequest` over plain HTTP can still
observe UI callbacks in real time.

`POST /api/ag-ui/run` accepts an optional `workspacePath` (plus `inPlace`) to
bind the built-in `*_local` tools to an existing directory. That requires
`ALLOW_INPLACE_WORKSPACE=true`, and `WORKSPACE_ALLOWED_ROOTS` can restrict which
paths are bindable. When a `workspacePath` is bound on a fresh run the folder is
scanned exactly as the orchestrator scans it, and its orchestration context plus
a lazy-load skill/command/agent catalog become the system prompt — callers hand
over the folder instead of inlining `AGENTS.md`. Project hooks and path/shell
policy apply when enabled. `read_file_local` on an image or PDF attaches
multimodal parts to the next turn if the model is vision-capable. Resume patches
the persisted run-binding block without re-scanning.

### Recommended client sequence

1. **Register frontend tools (optional).** `POST /api/ag-ui/run` with a
   `threadId`, your `frontendTools` and empty `messages` returns a short
   bind-only stream (`RunStarted` + `RunFinished`). Interactive runs use the
   `frontendTools` sent **with each run**, so this only refreshes the legacy
   global cache behind `/v1/agent/getTools`.
2. **Subscribe to global events (optional).** `GET /api/ag-ui/events` for a
   fan-out view of tool activity, useful for dashboards.
3. **Start an interactive run.** `POST /api/ag-ui/run` with `messages`,
   `threadId`, an optional `runId`, and the current `frontendTools`. Keep the SSE
   stream open for Run/Tool/Text events.
4. **Handle tool calls.** On `TOOL_CALL_START` + `TOOL_CALL_ARGS`, render the
   component described by the tool `parameters` schema.
5. **Resolve interrupts.** If a run ends with
   `RUN_FINISHED.outcome.type="interrupt"`, collect every open interrupt and
   resume with `POST /api/ag-ui/run` carrying
   `resume: [{ "interruptId": "<interrupt.id>", "status": "resolved", "payload": ... }]`.
   Use `status: "cancelled"` without a payload to cancel. Several entries may be
   resolved in one request.

Always copy `interruptId` from each `Interrupt.id`. Frontend-tool interrupts
normally reuse the originating `tool_call_id`; hook-permission interrupts carry
their own permission ID and reference the deferred tool ID in metadata. Legacy
`state.toolResponse` is still accepted but is not the preferred contract.

Include the complete current `frontendTools` list on every interactive run. If
they are absent, the model cannot see them. If `tools` explicitly names an AG-UI
tool that is not present, the request fails with a "Requested tools not found"
error.

### Execution lifecycle (`awaitsResponse`)

When a tool pauses a run, `/v1/agent/executeRequest` responds with:

```json
{
  "awaitsResponse": true,
  "executionGuid": "2f8e7c1b-...",
  "stateGuid": "0ad6c9f0-...",
  "agui_tool_calls": [
    {
      "tool_call_id": "call_123",
      "tool_name": "Weather-Gate",
      "arguments": {"city": "New York"}
    }
  ],
  "result": null
}
```

`agui_tool_calls[0].tool_call_id` is the same ID emitted on the SSE stream.
Collect the user's answer, then either post it through AG-UI with `resume[]`, or
call `POST /v1/agent/resumeRun` with `{executionGuid, stateGuid, toolCallId,
result/error}` for a pure API flow. `GET /v1/agent/execution/{executionGuid}`
reports the latest status and the currently-waiting `stateGuid`. Every execution
ends `completed` or `failed`, and final responses are persisted, so dashboards
can poll without replaying the run.

### Error codes

A failed `initiate`, `execute` or `resume` may carry a stable `errorCode`
alongside the human-readable `error`. Provider failures use `QUOTA`,
`RATE_LIMIT`, `AUTH` or `UNAVAILABLE`; raw provider response bodies are never
returned.

Two codes describe a workspace whose root instructions (`AGENTS.md` /
`CLAUDE.md`) cannot be loaded. They are separate because they need different
fixes:

| Code | Meaning | Fix |
| --- | --- | --- |
| `ROOT_INSTRUCTIONS_TOO_LARGE` | The file loaded but exceeds the eager budget | Split the file, or raise `HARNESS_EAGER_BUDGET_CHARS` |
| `ROOT_INSTRUCTIONS_UNREADABLE` | The file could not be read at all — permissions, I/O, or a path discovery refuses such as a symlink or a non-regular file | Check the file's permissions and type |

Both fail the call rather than silently running against partial or stale
context, and `initiate` and `execute` report the same code for the same
condition.

### Request and response shapes

```json
{
  "role": "string (optional)",
  "task": "string (required)",
  "context": "string (optional)",
  "outputInstruction": "string (optional)",
  "tools": ["tool1", "tool2"],
  "model": "gpt-4o",
  "max_tool_calls": 10
}
```

```json
{
  "success": true,
  "response": "AI response content",
  "error": null,
  "tool_calls_made": 2,
  "total_tokens": 150,
  "partial_response": null,
  "tool_calls_info": [
    {
      "tool_index": 1,
      "tool_name": "google_search_general",
      "llm_tool_interaction_index": 1,
      "mcp_server_id": "general",
      "mcp_server_url": "https://server1.com/mcp"
    }
  ]
}
```

`GET /v1/agent/getTools` returns each tool with its attributed `name`,
bracketed server description, `input_schema`, `server_url`, `server_id`, and
`original_name`.

### Tool selection

- `tools` filters MCP and AG-UI tools only. Workspace-backed `*_local` built-ins
  are added independently.
- Omitted or `null` selects all available MCP tools and the run's AG-UI tools.
- An empty list selects no MCP or AG-UI tools.
- A non-empty list selects exactly those names; an unknown name is a
  bad-request error.
- Use the full attributed name (`search_google`, not `search`).
- One model turn may select several tools, including tools from different
  servers.

## Configuration

### MCP servers

Two forms are supported, both taking `{name, url}` objects. The name becomes the
suffix on every tool from that server.

```env
# Recommended: JSON array
MCP_SERVER_URLS=[{"name": "general", "url": "https://server1.com/mcp"}, {"name": "google", "url": "https://mcp.google.com/mcp"}]
```

```env
# Numbered objects, discovered from _1 upwards until the first gap
MCP_SERVER_URL_1={"name": "general", "url": "https://server1.com/mcp"}
MCP_SERVER_URL_2={"name": "google", "url": "https://mcp.google.com/mcp"}
```

`MCP_SERVER_URLS` is preferred when both are set, but only if its first element
is an object — otherwise parsing falls through to the numbered variables. A blank
or missing name becomes `default`.

The two forms handle bad input differently, so it is worth knowing which one you
are using:

| Input | Result |
|---|---|
| Numbered entry that is not an object with both keys | Skipped with a warning; discovery continues at the next index |
| Numbered index missing | Discovery stops there, so `_1` and `_3` yields only `_1` |
| Array entry missing `url` | Kept, then rejected at startup: `MCP server at index N must be an object with 'name' and 'url' properties` |
| Array mixing objects and bare strings | **Fails with `AttributeError`** rather than a configuration error |
| Array of only bare strings | Ignored — falls through to the numbered variables, or leaves the list empty |
| Empty array (`[]`) | Ignored the same way, so the numbered variables decide |
| `MCP_SERVER_URL` (no suffix) | Never read |

> **Not supported:** an array of bare URL strings
> (`MCP_SERVER_URLS=["https://a/mcp"]`) and the single-server
> `MCP_SERVER_URL=...` variable, both of which earlier documentation described as
> backward-compatible. With no numbered variables set, either leaves the server
> list empty and startup fails with
> `At least one MCP server URL must be configured`.

Every tool is renamed `{original_name}_{server_name}` and its description is
prefixed `[server_name]`, whether or not the base name collides. Original names
are preserved for the call to the server, and execution logs carry the server
name and URL. See
[docs/MULTI_SERVER_IMPLEMENTATION.md](docs/MULTI_SERVER_IMPLEMENTATION.md).

### Core

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | — | Required. Async SQLAlchemy URL, e.g. `postgresql+asyncpg://pyagent:pyagent@localhost:5432/pyagent` |
| `LITELLM_BASE_URL` | `http://localhost:4000` | LiteLLM endpoint |
| `LITELLM_API_KEY` | `sk-1234` | LiteLLM key |
| `LITELLM_REQUEST_TIMEOUT_IN_SEC` | `300` | HTTP idle/network timeout for each LiteLLM request |
| `LITELLM_MODEL_DEADLINE_SEC` | `240` | Absolute wall-clock deadline for one complete LiteLLM call, including streamed response consumption |
| `LITELLM_MAX_COMPLETION_TOKENS` | `0` (off) | Optional operator resource guard; when positive, sends a completion-token cap and accepts possible truncation |
| `LITELLM_DROP_PARAMS` | `True` | Drop parameters a model does not accept |
| `MAX_TOOL_CALLS` | `10` | Tool calls per request |
| `ServiceName` | `jb-ai-orchestrator-service` | Service name |
| `Environment` | `DEV` | Environment name |
| `Region` | `USC1` | Region |
| `SwaggerBasePath` | empty | Base path when served behind a gateway |
| `General_LogFolder` | `./Logs` | Log directory |
| `Logging_LogLevel_Default` | `Information` | Log level |

### Harness

| Variable | Default | Purpose |
|---|---|---|
| `HARNESS_EAGER_BUDGET_CHARS` | `24000` | Total characters of eagerly-injected context. Root `AGENTS.md`/`CLAUDE.md` are one mandatory allocation of this budget and fail explicitly when they exceed it, rather than being silently truncated; raise this instead of splitting a large playbook. Optional rules take what remains, whole or not at all. A non-numeric or non-positive value is logged and ignored. |

### Lifecycle

| Variable | Default | Purpose |
|---|---|---|
| `RESUME_CLAIM_TIMEOUT_SEC` | `300` | Age at which a stale in-progress resume claim is restored for retry |
| `RUN_HEARTBEAT_INTERVAL_SEC` | `5` | Heartbeat interval for active runs; must be ≥ 1 |
| `RUN_HEARTBEAT_STALE_SEC` | `300` | Runs without a newer heartbeat are stale and reconciled before close or a replacement claim; must exceed the interval |
| `RUN_SEGMENT_DEADLINE_SEC` | `270` | Absolute wall-clock deadline for workspace/binding preparation plus the complete model/tool loop |
| `RUN_CANCELLATION_WARN_SEC` | `5` | Emit a structured diagnostic if a cancelled worker has not stopped after this many seconds |
| `CLOSE_WAIT_TIMEOUT_SEC` | `10` | Wait before close returns **202** `closing` instead of **200** `closed` |

The default ordering is model call **240s**, complete segment **270s**, caller
**300s**, and reverse proxy **310s** or more. A deadline cancels and awaits the
worker before its `execution_runs` claim becomes terminal. A new claim may
reconcile a crash-orphaned row only after its heartbeat is stale; a fresh row
still returns `RUN_CONFLICT`.

The service is a generic execution engine: it enforces containment, isolation,
bounded execution, safe claim release, and retryable session state. It does not
set workflow policy for model output or behavior. Failed attempt records remain
diagnostic history, while the execution, pending interaction, or conversation
returns to its real runnable state unless the caller explicitly closed it.

### Workspaces and bindings

| Variable | Default | Purpose |
|---|---|---|
| `WORKSPACES_ROOT` | `<tmp>/jb-agent-workspaces` | Root for per-execution sandboxes |
| `ALLOW_INPLACE_WORKSPACE` | `false` | Allow `inPlace=true` to run on a local path without copying |
| `WORKSPACE_ALLOWED_ROOTS` | empty | Absolute roots bindable in place or via AG-UI `workspacePath` |
| `GIT_CLONE_TIMEOUT_SEC` | `120` | Timeout for `git clone` and archive downloads |
| `MAX_WORKSPACE_MB` | `0` | Soft cap on provisioned workspace size; `0` disables |
| `OUTPUT_BINDINGS_ENABLED` | `false` | Allow non-null output bindings and durable output tools |
| `OUTPUT_READ_MAX_BYTES` | `5242880` | Durable output read cap |
| `OUTPUT_WRITE_MAX_BYTES` | `5242880` | Durable output write cap |
| `OUTPUT_LIST_MAX_ENTRIES` | `1000` | Durable output listing page cap |
| `AZURE_BLOB_TIMEOUT_SEC` | `60` | Per-operation Azure Blob timeout |
| `PROMPTS_DIR` | empty | Override directory for YAML prompt templates |

### Tools

| Variable | Default | Purpose |
|---|---|---|
| `LOCAL_TOOLS_ENABLED` | `true` | Expose the built-in local coding tools |
| `LOCAL_TOOLS_NAMESPACE` | `local` | Suffix marking local tools, e.g. `read_file_local` |
| `FILTER_MCP_TOOLS_CONFLICTING_WITH_LOCAL` | `true` | Drop remote MCP tools whose base name collides with a built-in |
| `LOCAL_SHELL_ENABLED` | `true` | Expose `execute_local`; when `false` the tool is hidden |
| `LOCAL_SHELL_TIMEOUT_SEC` | `60` | Default shell timeout |
| `LOCAL_SHELL_MAX_OUTPUT_BYTES` | `100000` | Captured bytes per output stream |
| `SUBAGENT_ENABLED` | `true` | Expose the `task_local` subagent tool |
| `SUBAGENT_MAX_TOOL_CALLS` | `8` | Tool calls inside one subagent |
| `SUBAGENT_MAX_DEPTH` | `2` | Maximum `task_local` nesting depth |

### Context management

| Variable | Default | Purpose |
|---|---|---|
| `CONTEXT_COMPACTION_ENABLED` | `true` | Offload oversized tool results and compact older tool messages |
| `TOOL_RESULT_OFFLOAD_CHARS` | `8000` | Offload a tool result above this JSON size |
| `CONTEXT_COMPACTION_CHARS` | `120000` | Compact when total message characters exceed this |
| `CONTEXT_COMPACTION_TOKENS` | `0` | When above `0`, use a token budget instead of characters |
| `CONTEXT_COMPACTION_KEEP_RECENT_TOOL_MSGS` | `8` | Recent tool messages kept intact |
| `CONTEXT_COMPACTION_KEEP_RECENT_TURNS` | `4` | Recent turns kept during summarization |
| `CONTEXT_SUMMARIZATION_ENABLED` | `true` | Summarize older turns before tool compaction |
| `CONTEXT_SUMMARIZATION_MODEL` | run model | Model used for summarization |
| `LLM_STREAMING_ENABLED` | `true` | Stream tokens to AG-UI when a UI channel exists |

### Hooks, policy and multimodal

| Variable | Default | Purpose |
|---|---|---|
| `HOOKS_ENABLED` | `true` | Run project Cursor/Claude hooks |
| `HOOKS_FAIL_CLOSED` | `false` | Block the action when a hook errors or times out |
| `HOOKS_TIMEOUT_SEC` | `30` | Per-hook subprocess timeout |
| `HOOKS_MAX_OUTPUT_BYTES` | `100000` | Cap on hook output |
| `PATH_POLICY_ENABLED` | `true` | Enforce path and shell allow/deny lists |
| `PATH_DENYLIST` / `PATH_ALLOWLIST` | empty | Comma-separated globs under the workspace |
| `SHELL_COMMAND_DENYLIST` / `SHELL_COMMAND_ALLOWLIST` | empty | Regex filters for `execute_local` |
| `MULTIMODAL_ENABLED` | `true` | Attach images and PDFs read by `read_file_local` |
| `MULTIMODAL_MAX_BYTES` | `5242880` | Maximum attachment size |
| `MULTIMODAL_MODEL_ALLOWLIST` | empty | When set, only matching model ids receive attachments |
| `MULTIMODAL_MODEL_DENYLIST` | `gpt-3.5-turbo`, embeddings, … | Model ids that never receive attachments |
| `MULTIMODAL_VISION_MARKERS` | `gpt-4o`, Claude, Gemini, … | Vision-capable markers used when no allowlist is set |

Hook configuration keeps Cursor-first precedence, followed by Claude
`hooks.json` and `settings.json`. Files are opened within the workspace without
following symlinks and are limited to 64,000 characters. Unsafe, malformed,
excessively nested, or oversized hook configuration is ignored and no hooks
from it execute; discovery reports the reason in manifest notes. Primitive
discovery also refuses symlinks and reports one manifest note with the number of
unique symlinked workspace paths skipped during that collection.

Full orchestrator and harness knobs are documented in
[docs/ORCHESTRATOR.md](docs/ORCHESTRATOR.md) §11, and every key above appears in
`src/.env/localhost/.env.example`.

## Workspaces, input and durable output

A workspace is the input playbook or working copy. It is distinct from optional
durable output and from service-owned `.agent` runtime files. Two entry points
create one:

| Endpoint | Field | What you pass | How the workspace is created |
|---|---|---|---|
| `POST /api/ag-ui/run` | `workspacePath` (+ `inPlace`) | an **existing container path** only, no git or URL | **In place** — binds that exact directory, no copy |
| `POST /v1/orchestrator/initiate` | `folder` (+ `inPlace`) | a **git URL**, an **archive URL** (`.zip`/`.tar.gz`/`.tgz`/`.tar` over HTTP(S)), a **`file://` or UNC folder**, or a **local path** | Provisioned per the source kind below |

`initiate` source kinds (`workspace_manager.classify_source`):

| Source (`folder`) | Handling | Workspace location | Output persisted to host? |
|---|---|---|---|
| **Local path** + `inPlace: true` | Runs in place, no copy. Needs `ALLOW_INPLACE_WORKSPACE=true` and a path under `WORKSPACE_ALLOWED_ROOTS` | your folder itself | **Yes** — written straight back |
| **Local path** + `inPlace: false` | Copied into a sandbox | `WORKSPACES_ROOT/{executionId}/workspace` | **No host target** — kept in service storage until operator cleanup |
| **Git URL** (`*.git`, `git@…`, github/gitlab/…) | `git clone --depth 1`; an optional `input.branch` adds `--branch --single-branch` | `WORKSPACES_ROOT/{executionId}/workspace` | **No host target** |
| **Archive / shared folder** (`.zip`, `.tar.gz`, `.tgz`, `.tar`, `file://`, UNC `\\host\share`) | Downloaded or copied, then extracted | `WORKSPACES_ROOT/{executionId}/workspace` | **No host target** |

There is no object-store *input* provider: nothing lists a bucket or container.
A remote source must be a single archive fetched over HTTP(S), so a blob or S3
URL only works when it addresses the `.zip`/`.tar.gz` object itself — either
pre-signed, or with `credentials.inputAccessToken`, which is sent as an
`Authorization` header (`Bearer` is added unless the value already carries a
scheme). Object storage appears on the **output** side only, where the providers
are `shared_folder` and `azure_blob`.

With `OUTPUT_BINDINGS_ENABLED=true`, callers may bind `shared_folder` or
`azure_blob` output, and the model gains `write_output_local`,
`edit_output_local`, `read_output_local` and `list_output_local`. Targeted
edits use exact string replacement and fail on missing or ambiguous text rather
than silently rewriting the file. For Azure,
`credentials.outputAccessToken` is a SAS supplied fresh with each execute or
resume; it must stay valid for that segment and is never persisted.

New-contract sessions default to `workflow`, which is observation-only for input
even when `output` is null. Use `working_copy` for writable input, or bind
durable output for workflow state. Output paths are logical relative paths, and
`.agent/**` is service runtime that cannot be written as output. Input, runtime
and output roots stay separate; output is never removed by session lifecycle.
Use the `/close` endpoints for teardown — `/abandon` only discards holds. AG-UI
does not enable git or URL input.

### Sharing a folder between host and container

To let in-place runs read and write a folder on your machine, bind-mount it at
`/workspaces`. `docker-compose.yml` is already wired for this through
`WORKSPACES_HOST_DIR`:

```yaml
volumes:
  - ${WORKSPACES_HOST_DIR:-./workspaces}:/workspaces
```

Allow in-place binding and restrict it to that mount in `src/.env/docker/.env`:

```env
ALLOW_INPLACE_WORKSPACE=true
WORKSPACE_ALLOWED_ROOTS=/workspaces
OUTPUT_BINDINGS_ENABLED=false
```

Then pass the host path when bringing the stack up:

```bash
# macOS / Linux
WORKSPACES_HOST_DIR=/Users/you/workflows docker compose up --build -d
# Windows PowerShell
$env:WORKSPACES_HOST_DIR="c:/git/workflows"; docker compose up --build -d
```

Callers now reference a workflow by absolute container path under the mount —
`workspacePath: "/workspaces/<folder>"` on `/api/ag-ui/run`, or
`folder: "/workspaces/<folder>"` with `inPlace: true` on
`/v1/orchestrator/initiate`. The path is literal, so every caller sharing the
mount must map the **same host folder to the same container path**. Durable
output uses a separate `shared_folder` binding under a root listed in
`WORKSPACE_ALLOWED_ROOTS`; close never deletes caller-owned input or output, so
the caller owns output retention.

Deployments that send non-null output bindings must opt into
`OUTPUT_BINDINGS_ENABLED=true` in their own compose or env, and then
`WORKSPACE_ALLOWED_ROOTS` must cover every workflow **and** output mount. Local
`shared_folder` output needs no credentials.

> **Private git input (planned).** Fetching a workflow from a git URL is the
> service's job: as a deploy-once orchestrator serving many teams it would hold
> its **own** service-level git credential rather than accepting tokens per
> request. Today `initiate` clones **public** git URLs; private-repo auth and a
> durable output target for cloned input are planned.

### Caller examples

`ag-ui-demo/` is a protocol sandbox, not production web code. It demonstrates
env-driven bindings on fresh AG-UI runs and thread close with bounded **202**
retry; see [ag-ui-demo/README.md](ag-ui-demo/README.md). Production integration
belongs in the calling application; this service stays a generic orchestrator.

## Running many instances

Interactive state is durable, so the service scales horizontally without sticky
sessions:

- **Frontend tools are per run.** The agent loop uses the `frontend_tools` passed
  with each request, so concurrent sessions and separate applications sharing one
  deployment never overwrite each other. The process-global cache exists only for
  the `getTools` listing and legacy bind-only runs. Tool definitions needed for
  resume are persisted in `LLMState.state_payload`.
- **Nothing waits in memory.** When a tool awaits a user response — an AG-UI tool
  or the built-in `ask_user` — the loop persists to `LLMState` and returns. Any
  instance serves the follow-up through `/api/ag-ui/run` (`resume[]`),
  `/v1/orchestrator/resume`, or `/v1/agent/resumeRun`. AG-UI locates the hold by
  interrupt or tool ID; orchestrator and direct-agent resumes load the supplied
  `stateGuid` and claim it atomically.
- **The only per-pod resource is the SSE socket**, which needs no stickiness
  because resume arrives as a fresh request.

`GET /api/ag-ui/events` subscribers remain per-process queues. Correctness does
not depend on them; cross-pod dashboard fan-out would need shared pub/sub.

## Development

Install the runtime requirements plus `pytest` and `pytest-asyncio`, then run the
suite from the repository root:

```bash
pytest
```

`pytest.ini` sets `testpaths = tests` and `asyncio_mode = auto`, so async tests
need no marker. Tests marked `live` require a separately running service and are
excluded by default. The suite needs no database and no network.

`scripts/` holds checks that need a running service, so they are not part of the
suite:

```bash
python scripts/smoke_service.py        # agent endpoints and tool attribution
python scripts/smoke_agui.py           # AG-UI bind, stream, interrupt and resume
python scripts/orchestrator_continuity_exercise.py --help
```

Set `PY_MAIN_AGENT_BASE_URL` to target something other than
`http://localhost:8000`, and `PY_MAIN_AGENT_SMOKE_MODEL` to change the model
(default `gpt-4o`). Both smoke checks exit non-zero on the first failure. Because
tool names depend on which MCP servers you configured, `smoke_service.py` takes
them from `/getTools` unless you set `PY_MAIN_AGENT_SMOKE_TOOLS`, and
`smoke_agui.py` exercises only its own UI tools unless you name an MCP tool in
`PY_MAIN_AGENT_SMOKE_MCP_TOOL`.

`orchestrator_continuity_exercise.py` is dry-run by default; use
`--run --simulate-execute` when no LLM is configured, and pass
`--host-shared-root` (or set `WORKSPACES_HOST_DIR`) for live Docker runs.

`scripts/smoke_agui.py` and `ag-ui-demo/` demonstrate compatibility paths. Do
not treat them as references for canonical `resume[]`, cancellation,
multi-interrupt, thread-abandon or typed-history behaviour — those contracts are
covered by `tests/test_agui_controller_resume.py` and
`tests/test_agui_batch_resume.py`.

The service depends on FastAPI and uvicorn, the `mcp` client, SQLAlchemy with
asyncpg, Alembic, `ag-ui-protocol`, and `azure-storage-blob` for the optional
blob output provider. It talks to LiteLLM over HTTP with `httpx` rather than
importing the LiteLLM SDK. `src/requirements.txt` is the authoritative list.

## Limitations and open work

- **Serial tool loops.** Independent reads are not always batched, so a workflow
  can need many model round trips. Batch or programmatic tool execution would
  cut latency and context churn.
- **Context thresholds.** Token-aware compaction exists, but character
  thresholds remain the default. A model-window fraction trigger would be safer
  across providers.
- **Offload lifecycle.** `.agent/offload/` preserves oversized results; fresh
  AG-UI runs prune unreferenced files when discarding stale awaits, and thread
  close removes runtime. Durable output is never session-cleaned.
- **Prompt caching.** Static harness sections are not explicitly segmented for
  provider prompt-cache reuse.
- **Diagnostics.** There is no `/context`-style endpoint exposing the loaded root
  instructions, catalogs, tools and context budget.
- **Harness coverage.** Only Cursor and Claude Code have dedicated adapters.
  Anything else falls back to the generic one, which reads `AGENTS.md` (or a
  README as a last resort), so a repository organised for another tool — GitHub
  Copilot's `.github/copilot-instructions.md`, for instance — contributes nothing
  beyond that. The adapter contract is deliberately small, so these are additions
  rather than redesigns.
- **MCP scopes.** Named multi-server configuration has no local/project/user
  precedence or managed policy layer.
- **Event fan-out.** `/api/ag-ui/events` subscribers are process-local.
- **Pending-state visibility.** There are no metrics for rows stuck in
  `awaiting_response`, which would surface abandoned interactions.

## Documentation

- **[docs/ORCHESTRATOR.md](docs/ORCHESTRATOR.md)** — source of truth for the
  harness: workspace and instruction loading, prompt composition, the tool loop,
  compaction, resume, lifecycle and close, persistence, and roadmap
- **[docs/MULTI_SERVER_IMPLEMENTATION.md](docs/MULTI_SERVER_IMPLEMENTATION.md)** —
  multiple MCP servers and universal tool attribution
- **[docs/DOCUMENTATION_INDEX.md](docs/DOCUMENTATION_INDEX.md)** — navigation
- **[ag-ui-demo/README.md](ag-ui-demo/README.md)** — protocol sandbox caller
- `changes/` — what shipped, per change

## License

First-party code in this repository is licensed under the Apache License,
Version 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Third-party material
remains subject to its own license and notices.
