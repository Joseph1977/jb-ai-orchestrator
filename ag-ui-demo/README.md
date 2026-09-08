# AG-UI Demo

A lightweight React + TypeScript **protocol sandbox** that exercises the [AG-UI protocol](https://github.com/ag-ui-protocol/ag-ui) against the generic `jb-ai-orchestrator` orchestrator microservice.

> **Scope:** this app demonstrates caller-side integration patterns (bind-only registration, SSE runs, optional input/output bindings, thread close with **202** retry). It is **not** authoritative for production web behavior — use the root `readme.md` / `docs/ORCHESTRATOR.md` for the full contract. Resume via tool response omits bindings (the service persists them per thread).

## What it does

- **Registers frontend tools** with the agent via `POST /api/ag-ui/run` (bind-only)
- **Streams AG-UI events** (SSE) from the agent and renders interactive UI widgets in real time
- **Collects user input** and submits tool responses to resume paused LLM runs (bindings omitted on resume)
- **Optional configured bindings** on fresh runs when `VITE_BINDING_INPUT_URI` is set: workflow `shared_folder` input (`in_place_read_only`), optional local `shared_folder` output (no credentials), `mode: workflow`. Plain chat omits bindings entirely.
- **Closes AG-UI threads** via `POST /api/ag-ui/threads/{threadId}/close` with bounded **202** retry
- Demonstrates all four component types the LLM can drive:
  - **Checklist** — progress steps with spinner/checkmarks
  - **OutputMessage** — info/warning banners
  - **InputMessage** — yes/no, multi-checkbox, and table inputs
  - **ApprovalPrompt** — approve/decline with optional reason

## Prerequisites

- Node.js 18+
- A running `jb-ai-orchestrator` service (default `http://localhost:8000`)
- For binding examples with output: operator flags `ALLOW_INPLACE_WORKSPACE=true`, `OUTPUT_BINDINGS_ENABLED=true` (web deployments opt in; the standalone orchestrator defaults to `false`), and `WORKSPACE_ALLOWED_ROOTS` covering mounts (see root `readme.md`)

## Quick Start

```bash
cd ag-ui-demo
npm install
npm run dev
```

The app opens at `http://localhost:5173`.

## Configuration

Copy `.env.example` to `.env` and adjust:

| Variable | Default | Description |
|---|---|---|
| `VITE_MAIN_AGENT_BASE_URL` | `http://localhost:8000` | jb-ai-orchestrator base URL |
| `VITE_THREAD_ID` | `ag-ui-demo-thread` | Default thread ID for runs and close |
| `VITE_BINDING_INPUT_URI` | _(unset)_ | When set, fresh runs send workflow `input` (`shared_folder`, `in_place_read_only`). Required for any binding example. |
| `VITE_BINDING_INPUT_RELATIVE_PATH` | `.` | Relative path within input URI |
| `VITE_BINDING_OUTPUT_URI` | _(unset)_ | Optional local `shared_folder` output (no credentials); only valid with `VITE_BINDING_INPUT_URI` |
| `VITE_BINDING_OUTPUT_RELATIVE_PATH` | `.` | Relative path within output URI |
| `VITE_CLOSE_RETRY_MAX_ATTEMPTS` | `5` | Max close POST attempts when server returns **202** `closing` (invalid values clamp to default; max 20) |
| `VITE_CLOSE_RETRY_BACKOFF_MS` | `500` | Delay between close retries (invalid values clamp to default) |

When `VITE_BINDING_INPUT_URI` is unset, fresh runs omit bindings (plain chat). Output requires workflow input in this demo — configuring `VITE_BINDING_OUTPUT_URI` without input is rejected before any request is sent. Do not hardcode host paths — use container-visible paths under your deployment mount (e.g. `/workspaces/...`).

## Usage

1. **Edit the Prompt** — the prompt textarea is pre-filled with a demo message. Change it to whatever you want the LLM to do. You can also change the **Model** field (e.g. `gpt-4.1-mini`, `gpt-4o`, `claude-3-5-sonnet`).
2. Click **"1 — Register Tools"** to bind the four frontend tools with the agent (bind-only run). The log shows exactly what's sent to `POST /api/ag-ui/run`.
3. Click **"2 — Start Run"** to send the prompt and open an SSE stream. If binding env vars are set, the payload includes the generic three-root contract (`input`, optional `output`, `mode: workflow`). The LLM will call the registered tools, driving the UI.
4. When the LLM calls an `awaitsResponse` tool (Ask-User or Request-Approval), the corresponding input widget appears. Submit your response to resume the run (bindings are **not** re-sent).
5. Click **"Close Thread"** to call `POST /api/ag-ui/threads/{threadId}/close`. **200** `closed` completes; **202** `closing` is retried with bounded backoff. Errors appear in the event log.

### Toggles

| Toggle | What it does |
|---|---|
| **Global SSE** | Opens an `EventSource` to `GET /api/ag-ui/events` — a server-wide fan-out stream that mirrors all tool-call events from any run. Useful for debugging. |
| **Full Log** | When enabled, the Event Log shows full JSON payloads for every request sent and every SSE event received. When off, it shows concise one-line summaries. |

The **Event Log** panel (right side) shows all activity in real time — requests sent (blue), events received (green), and errors (red). Use the **Clear** button to reset it.

## Phase 4 caller adoption (demo)

| Concern | Demo behavior |
|---|---|
| Demo binding policy | Fresh runs send `input` (+ optional `output`, `mode: workflow`) only when `VITE_BINDING_INPUT_URI` is set; plain chat has no bindings; resume omits bindings |
| Close lifecycle | `closeThread()` in `src/api/agui.ts`; **200** closed, retry **202** with clamped `VITE_CLOSE_RETRY_*`. Use close for lifecycle teardown; `/abandon` is non-destructive hold discard only (production web compatibility) |
| Output retention | Web/caller owns durable output; close never deletes in-place input or bound output |
| Production web | Your own gateway remains the production caller; this demo is a protocol sandbox only |

**Migration (caller integration):** Phase 4 demo/docs alignment is complete for optional bindings and thread close. No orchestrator web-specific behavior is implied.

## Frontend Tools Registered

| Tool | Description | awaitsResponse |
|---|---|---|
| `Checklist-Update` | Update the progress checklist | No |
| `Show-Message` | Display info/warning messages | No |
| `Ask-User` | Prompt for yes/no, checkbox, or table input | **Yes** |
| `Request-Approval` | Ask for approve/decline | **Yes** |

## Project Structure

```
ag-ui-demo/
├── index.html
├── src/
│   ├── main.tsx              # Entry point
│   ├── App.tsx               # App shell
│   ├── demo/
│   │   └── DemoPage.tsx      # Main orchestration page
│   ├── config/
│   │   └── bindings.ts       # Optional env-driven run bindings
│   ├── components/
│   │   ├── Checklist/        # Step list with spinner/check
│   │   ├── OutputMessage/    # Info/warning banner
│   │   ├── InputMessage/     # Yes-no, checkbox, table
│   │   └── ApprovalPrompt/   # Approve/decline
│   ├── api/
│   │   ├── client.ts         # fetch helpers + base URL
│   │   └── agui.ts           # AG-UI endpoint wrappers (run, close)
│   ├── hooks/
│   │   └── useSSE.ts         # Global SSE hook
│   ├── types/
│   │   └── agui.ts           # Shared TypeScript types
│   └── styles/
│       └── theme.css         # CSS variables / dark mode
├── package.json
├── tsconfig.json
└── vite.config.ts
```

## Architecture Notes

The demo uses **fetch + ReadableStream** to consume SSE from `POST /api/ag-ui/run` (the AG-UI protocol uses POST for runs, not GET). The global events endpoint (`GET /api/ag-ui/events`) uses the standard `EventSource` API via the `useSSE` hook.

## Tests

```bash
npm test
npm run build
```

Tests cover configured binding payloads (no credentials), and close **200** / **202** retry / exhaustion / failure paths.
