# AG-UI Demo — Plan

## Purpose

This document describes the implementation plan for a lightweight AG-UI demo React application located at `services/jb-ai-orchestrator/ag-ui-demo`.
Primary purpose: demonstrate the AG-UI runtime where the LLM drives UI updates and the UI responds to LLM tool-calls (i.e., show full round-trip where the LLM requests UI actions and the frontend supplies responses). The demo will showcase UI components the LLM can drive: a checklist with active/spinning step and completed checkmarks, output messages (info/warning), input messages (yes/no, multi-checkbox, table), and approval/decline prompts.
The app will integrate with the `jb-ai-orchestrator` service to register frontend tools and to call `executeRequest` / AG-UI endpoints for simulated LLM-driven flows.

This implementation will align with the AG-UI protocol (https://github.com/ag-ui-protocol/ag-ui) for event shapes and tool-call payloads; the demo will use the protocol's canonical event names (`RUN_STARTED`, `RUN_FINISHED`, `RUN_ERROR`, `TEXT_MESSAGE_START/CONTENT/END`, `TOOL_CALL_START`, `TOOL_CALL_ARGS`, `TOOL_CALL_END`) and tool registration format when interacting with the `jb-ai-orchestrator` controller.

Frontend tools that need to pause the LLM run and wait for user input must include `extensions: { awaitsResponse: true }` in the tool definition. The server emits the full `TOOL_CALL_START` → `TOOL_CALL_ARGS` → `TOOL_CALL_END` sequence for all tools (per the AG-UI protocol, `TOOL_CALL_END` signals arguments are complete, not that execution is finished). For `awaitsResponse` tools, the demo renders an interactive component, collects user input, and submits the response via `state: { toolCallId, result }` to resume the paused LLM run.

## Goals & Deliverables

- A Vite + React + TypeScript demo app that runs locally with `npm install` + `npm run dev`.
- Reusable, well-typed components: `Checklist`, `OutputMessage`, `InputMessage`, `ApprovalPrompt`.
- API helper that demonstrates registering frontend tools and calling the orchestrator endpoints (`/api/ag-ui/run`, `/v1/agent/executeRequest`) with example payloads.
- A demo page (`DemoPage`) that simulates an LLM-driven flow: advance planning steps, pause for input, resume after user response.
- `README.md` and this `PLAN.md` in the `ag-ui-demo` folder.
- Minimal tests for critical component behavior (Vitest) and linting configs.

## High-level Architecture

- Frontend (React) — Single Page Demo that connects to `jb-ai-orchestrator` using HTTP and SSE.
- Integration points:
  - Register frontend tools: POST `/api/ag-ui/run` (bind-only with `frontendTools`) to advertise available UI components.
  - Start interactive run: POST `/api/ag-ui/run` with `messages` and open the returned SSE stream to receive `TOOL_CALL_*` events.
  - Resume/persist flows: Support calling `/v1/agent/executeRequest` for API-driven flows that may pause, and use `/v1/agent/resumeRun` to resume if the server returns `awaitsResponse`.

Diagram (simple):

Frontend (ag-ui-demo) <--> jb-ai-orchestrator (AG-UI controller, ToolHub) <--> LiteLLM / MCP servers

The frontend will perform both bind-only registration and live runs. SSE streaming will be used to receive `TOOL_CALL_*` events for rendering interactive UI widgets.

## Project Structure

services/jb-ai-orchestrator/ag-ui-demo/

- index.html                # Vite entry (root level)
- src/
  - main.tsx                # App entry
  - App.tsx                 # Router / Demo shell
  - demo/
    - DemoPage.tsx          # orchestrates LLM flow simulation
  - components/
    - Checklist/
      - Checklist.tsx
      - Checklist.module.css
      - types.ts
    - OutputMessage/
      - OutputMessage.tsx
      - styles.css
    - InputMessage/
      - InputMessage.tsx
      - CheckboxGroup.tsx
      - TableInput.tsx
    - ApprovalPrompt/
      - ApprovalPrompt.tsx
  - api/
    - client.ts             # fetch-based helpers + baseURL from env
    - agui.ts               # helpers to register tools, start run, post responses
  - hooks/
    - useSSE.ts             # small SSE hook to subscribe to /api/ag-ui/events or a run stream
  - types/
    - agui.ts               # shared types & enums: EventType, ToolCall, Event shapes
  - styles/
    - theme.css
  - index.css
- package.json
- tsconfig.json
- vite.config.ts
- README.md
- PLAN.md (this file)

## Component Design (concise)

- Checklist
  - Props: `steps: { id:string; title:string; status: 'pending'|'active'|'done' }[]`
  - Visuals: active step shows spinner + highlight; done shows green check; others show number or idle icon.
  - Emits `onStepClicked(stepId)` for manual navigation in demo.

- OutputMessage
  - Props: `type: 'info'|'warning'`, `title?:string`, `body:string`.
  - Accessible ARIA roles for alerts.

- InputMessage
  - Modes: `yesno` (two buttons), `checkbox` (multi-select), `table` (simple inputs represented as rows/cols).
  - Emits `onSubmit(result)` with standardized payload.

- ApprovalPrompt
  - Simple approve/decline with optional reason textbox on decline.
  - Emits `onDecision({ approved: boolean, reason?: string })`.

All components will be small, typed, well-documented, and export default for easy extension.

## Integration & Example Flows

The demo will implement the full API call sequence so reviewers can validate behavior end-to-end. Sequence summary (the app will execute these calls programmatically in the demo flows):

1) Bind-only registration (advertise tools)

- POST `/api/ag-ui/run` (bind-only): send `threadId` and `frontendTools` describing components and parameter schemas in the AG-UI protocol format. Expect short SSE responses (`RunStarted`, `RunFinished`).

2) Subscribe to global events (optional)

- Open `GET /api/ag-ui/events` SSE to observe `TOOL_CALL_*` events emitted by the server (fan-out channel). Useful for verifying that UI tool calls from `executeRequest` are published.

3) Start an interactive run (SSE-driven)

- POST `/api/ag-ui/run` with `messages`, `threadId`, optional `runId`, and `frontendTools` to ensure the server has latest tool definitions.
- Optional Phase 4: when `VITE_BINDING_INPUT_URI` is set, fresh runs send `input` (`shared_folder` + `in_place_read_only`), optional `output` (`shared_folder`, no credentials), and `mode: workflow`. Plain chat omits bindings. Output without input is invalid demo configuration.
- Keep the returned SSE stream open and handle events: `TOOL_CALL_START`, `TOOL_CALL_ARGS` (render component), `TOOL_CALL_END`.
- When receiving a tool call with `awaitsResponse=true`, the demo will collect user input and POST back to the same `/api/ag-ui/run` endpoint with `state: { toolCallId, result }` to resume the LLM run (bindings omitted on resume).

5) Close thread lifecycle

- POST `/api/ag-ui/threads/{threadId}/close` returns **200** `closed` or **202** `closing`. The demo retries **202** with bounded backoff (`VITE_CLOSE_RETRY_*`); errors are surfaced in the event log.

4) API-driven run (executeRequest + resume)

- POST `/v1/agent/executeRequest` for purely API-driven flows. If response contains `awaitsResponse: true`, the server will provide `executionGuid` and `stateGuid`.
- The demo will persist these identifiers in-memory (for demo) and will resume via `POST /v1/agent/resumeRun` with `{ executionGuid, stateGuid, toolCallId, result }` to complete the run.

Notes: the demo will include explicit example payloads and a sample `frontendTools` descriptor that follows the AG-UI protocol schema, so the `jb-ai-orchestrator` sees the same tool definitions the LLM expects.

## Env Variables and Configuration

The demo will read runtime configuration from environment variables (via `import.meta.env` for Vite):

- `VITE_MAIN_AGENT_BASE_URL` (required) — Base URL for the `jb-ai-orchestrator` service (e.g., `http://localhost:8000`).
- `VITE_THREAD_ID` (optional) — default `ag-ui-demo-thread` used for bind/run/close messages.
- `VITE_BINDING_INPUT_URI` (optional) — when set, fresh runs send workflow `input: { type: shared_folder, materialization: in_place_read_only }`. Required for binding examples; plain chat leaves it unset.
- `VITE_BINDING_OUTPUT_URI` (optional) — local `shared_folder` output without credentials; only valid together with `VITE_BINDING_INPUT_URI`. Requires operator `OUTPUT_BINDINGS_ENABLED=true` on the service when used.
- `VITE_BINDING_*_RELATIVE_PATH` (optional) — relative paths within input/output URIs (default `.`).
- `VITE_CLOSE_RETRY_MAX_ATTEMPTS` / `VITE_CLOSE_RETRY_BACKOFF_MS` — bounded, clamped retry when close returns **202** `closing`.

Example `.env` (in `ag-ui-demo`):

```
VITE_MAIN_AGENT_BASE_URL=http://localhost:8000
VITE_THREAD_ID=ag-ui-demo-thread
# VITE_BINDING_INPUT_URI=/workspaces/my-playbook
# VITE_BINDING_OUTPUT_URI=/workspaces/my-output
```

Notes: The demo will use CORS; if the `jb-ai-orchestrator` runs on a different origin, ensure CORS allows the demo origin (e.g., http://localhost:5173). Resume tool responses omit bindings; the service persists them per thread.

## Phase 4 caller adoption (completed in demo)

- Optional env-driven bindings on **fresh** runs only when `VITE_BINDING_INPUT_URI` is set (`input`, optional `output`; `mode: workflow`). Plain chat has no bindings.
- `closeThread()` helper with **200** closed / **202** retry (env values clamped to safe defaults); no session TTL logic in the demo.
- Web/caller owns durable output retention; close never deletes in-place input or bound output.
- This demo is a protocol sandbox — production web integration belongs in the calling application; the orchestrator stays generic.

## Security & Auth

- The demo will support adding an auth header `Authorization: Bearer <token>` via a simple UI input for `API_KEY` primarily for local testing.
- In production, use secure cookie or OAuth flows — demo will not implement production auth.

## Testing and Linting

- Include basic unit tests for `Checklist` and `InputMessage` behaviors (Vitest). Tests will be minimal but demonstrate approach.
- Add ESLint + Prettier configuration (links) to keep code consistent.

## Extensibility & Best Practices

- Keep components small and focused; each component folder contains component, styles, and types.
- Favor prop-driven behavior and typed callback signatures so the integration layer can wire tool calls cleanly.
- Centralize API interactions in `api/agui.ts` so changes to `jb-ai-orchestrator` endpoints are isolated.

## Development & Run Steps

1. From the `ag-ui-demo` folder:

```bash
npm install
npm run dev
```

2. Configure `.env` with `VITE_MAIN_AGENT_BASE_URL` (default `http://localhost:8000`).

3. Use the demo UI to perform Bind-only registration, start SSE runs, and simulate LLM-driven prompts.

## Branching, Commit & PR Strategy

- Work will be developed on a feature branch `feature/ag-ui-demo` created from `jb-ai-orchestrator` repository root.
- Small, focused commits; open PR against `main` or the repo's default branch. Include `README` and `PLAN.md` in the PR for review.

## Timeline / Milestones (estimate)

- Day 0: Add `PLAN.md` and scaffold package manifest + Vite config.
- Day 1: Implement components (`Checklist`, `OutputMessage`, `InputMessage`, `ApprovalPrompt`) and demo page.
- Day 2: API helpers, SSE hook, wiring, README, and basic tests.
- Day 3: Polish styles, accessibility fixes, unit tests, and open PR.

## Next actions (what I'll do once you approve)

1. Create the `ag-ui-demo` folder and commit `PLAN.md` (this file).
2. Scaffold Vite + React TypeScript app + `package.json`, `vite.config.ts`.
3. Implement core components and demo wiring.
4. Add README, scripts, basic tests, and push branch `feature/ag-ui-demo` for PR.

---

Please review this `PLAN.md`. If it looks good, I will proceed to scaffold the project and implement the components. If you want changes to the architecture, run steps, or variables, tell me which items to update before I scaffold.
