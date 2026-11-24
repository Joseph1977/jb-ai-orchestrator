# Plan — OpenCodeExecutionEngine implementation plan
Version: 2
Model: claude-opus-5-5-medium

## Response to review v1
| Point ID | Decision | Reason / change made |
|---|---|---|
| P001 | accepted | D14 drains the bridge before stop is confirmed: admission closes, per-run in-flight calls are tracked and awaited (MCP cancelled, local tools run to their own bound), then sidecar idle. Per-tool-class cancellation tests added. |
| P002 | accepted | D20 fences every bridge call on run ownership (row active, unfinished, same `run_pk`). Lost ownership denies the call, aborts the session, returns `RUN_LIFECYCLE_FAILED`. Stale-reclaim test with a live worker added. |
| P003 | accepted | A3 now requires per-session MCP credentials, proven by a concurrent two-session fixture. If the spike fails, D21 allows one session per sidecar under a process lock. Concurrent cross-token tests added. |
| P004 | partly accepted | D22 adds a tested subset, `tool_map` validator and rejection of requested unmapped tools, at segment start rather than pre-claim. Evidence: src/app/services/tool_hub.py#_select_tools \| docs/ORCHESTRATOR.md#11 MCP_FETCH_TIMEOUT_SEC \| C-1 |
| P005 | accepted | D10 gates on resolved `input_binding` (new-contract `input` or legacy `workspacePath`) before the claim. A `local_context` that resolves to nothing in-segment fails as an attempt, thread open. Tests cover all four cases. |
| P006 | accepted | D23 `resolve_model_route` covers `litellm` and `native` (native requires `OPENCODE_MODEL`; credentials stay in the sidecar). A9 split into A9a/A9b. Route, config and fixture-replay tests cover both modes. |

## Plan

### What / where / why
Insert an `ExecutionEngine` seam where controllers call `ToolExecutionHub.process_request`. Lifecycle (claims, heartbeat, deadline, close, persistence) stays in the controllers. Persist engine identity per run and per await state. Add a headless `OpenCodeExecutionEngine` whose every tool call returns through an orchestrator-hosted, per-run, ownership-fenced tool bridge. Serves In-1..In-6; Out-1..Out-4 untouched.

### Segment today (Q1)
- One segment is one `process_request(...)` task, in `_managed_hub_process` (`orchestrator_controller.py`) or the AG-UI closures (`ag_ui_controller.py` `_run_fresh_segment`, resume closure), under `session_close_service.manage_run`. That wrapper owns the heartbeat, the `RUN_SEGMENT_DEADLINE_SEC` watchdog, `cancel_event` + `task.cancel()`, and await-worker-before-release, with one `ExecutionRun` from `run_lifecycle.try_create_active_run`.
- Legacy `agent_controller.py` execute/`resumeRun` calls the hub with no `ExecutionRun` claim; unchanged.
- Result is the hub dict: `success, response, error, error_code`, counters, tokens, `tool_calls_info, agui_tool_calls, text_streamed, awaits_response, pending_tools, pending_tool, interrupts, state`.
- Await snapshot is `_build_await_result()["state"]`, stored in `llm_states.state_payload`. Resume uses that payload plus `resume_tool_results`, `resolve_resume_model` and `resolve_resume_max_tool_calls`. `prepare_hook_permission_resume_events` stays internal-only.

### Design decisions
- D1 (In-1) `engines/contract.py`: v1 `SegmentRequest` (current `process_request` kwargs plus `event_sink`), `EngineEvent` (text/tool/usage), `SegmentResult` (TypedDict of the hub keys), `AwaitEnvelope {engine, contractVersion, payload}`; `ENGINE_CONTRACT_VERSION = 1`.
- D2 (In-1) `engines/base.py` defines the `ExecutionEngine` protocol (`name`, `contract_version`, `capabilities()`, `async run_segment`). Failures return `success: False` dicts; `CancelledError` is re-raised only after all engine-owned work has stopped (D14).
- D3 (In-2) `engines/internal.py` forwards kwargs verbatim to the hub with `event_sink=None`; `task_local` stays inside the hub.
- D4 (In-2) `engines/resolver.py`: `resolve_fresh_engine()` reads `ORCHESTRATOR_ENGINE`; `resolve_persisted_engine(name, version)` never reads the environment. Applied at all six hub call sites.
- D5 (Q2, C-5) Typed columns, not the payload: `engine VARCHAR(32) NOT NULL DEFAULT 'internal'` and `engine_contract_version INTEGER NOT NULL DEFAULT 1` on `execution_runs` and `llm_states`. `state_payload` is unchanged (C-6).
- D6 (Q2) Revision `20261124_engine_identity` (down `20250816_phase3_lifecycle`). The server default backfills existing rows as internal/1, using the phase-3 adopt-if-present pattern; ORM models carry the same defaults. Downgrade drops the columns; `executions.config` is not rewritten.
- D7 (Q2, C-5) Mismatch is checked after loading state and before any claim (orchestrator resume, AG-UI hold lookup, legacy `resumeRun`). REST returns HTTP 409 `{"success":false,"errorCode":"ENGINE_MISMATCH","error":"Run state was created by engine '<e>' contract v<n>; this deployment cannot resume it"}`; AG-UI returns `RunErrorEvent("ENGINE_MISMATCH: …")`. State stays `AWAITING_RESPONSE`, no `ExecutionRun` is created, and internal state always resumes on internal.
- D8 (Q3) OpenCode v1 has no resume mode: no awaiting tool, no `llm_states` row, `durable_resume=false`. The first mode to add (Out-3) is the translated snapshot, because a session reference pins resume to one pod's sidecar (D11) and breaks replica independence. Rejection is tested with a crafted `engine='opencode'` row.
- D9 (Q4, In-4) `engines/capabilities.py` names: `headless_segment, workspace_file_ops, mcp_tools, streaming_output, agui_frontend_interrupts, durable_resume, hooks, permission_asks, subagents, context_compaction, workspace_free`. Internal reports all true. OpenCode reports `mcp_tools=subset` (D22), three others true, the rest false.
- D10 (Q4) `require_capabilities(engine, needs)` runs before `try_create_active_run` and before any `Execution` status change. It returns 400 / `RunErrorEvent` `ENGINE_CAPABILITY_UNSUPPORTED` naming the capability, and logs the denial. Gates:
  - non-empty `frontendTools`, or AG-UI frontend tools;
  - `workspace_free`: AG-UI with resolved `input_binding is None` (neither `input` nor `workspacePath`; `ag_ui_controller.py` ~1570–1596, before the claim at ~1632), and legacy `executeRequest`;
  - `HOOKS_ENABLED` with non-empty `load_hooks_for_workspace(...)`.
  - In-segment re-checks fail as an attempt (§12): `local_context is None` (e.g. `workspacePath` ignored when `ALLOW_INPLACE_WORKSPACE=false`), and hooks after `ensure_input_workspace`.
- D11 (C-2) One loopback sidecar per orchestrator process, with no workspace mount. Each session is rooted in an empty per-run scratch directory, and the orchestrator-composed system prompt is sent as the session system prompt (A4).
- D12 (C-2) Bridge `engines/opencode/bridge.py`: an internal MCP route, mounted only when engine=opencode.
  - Tools: registry local + mapped MCP (D22), minus `task_local`, `ask_user_local` and AG-UI tools.
  - Credential: a 256-bit per-run in-memory bearer bound to `(run_pk, LocalToolContext, registry, budget, user MCP token)`.
  - Local calls use `LocalToolProvider.execute` (tool policy, `resolve_within`, path policy, opener). The user MCP token never reaches the sidecar. Bad tokens get 401; built-ins are disabled (A2).
- D13 (C-4) `maxToolCalls` resolves request → segment config → `MAX_TOOL_CALLS` and is counted in the bridge. `OPENCODE_REQUEST_TIMEOUT_SEC` is an infrastructure guard; `OPENCODE_MODEL`/`OPENCODE_PROVIDER` are inert unless engine=opencode (default off).
- D14 (C-1, P001) Stop sequence, run on cancel, `CancelledError`, completion or any failure. It is shielded and swallows the repeated `task.cancel()`.
  1. Close bridge admission; late calls get a "run stopping" error and execute nothing.
  2. Send abort to the sidecar.
  3. Await every in-flight call in the run's `BridgeRunScope`; each executes in a scope-owned task, not the HTTP request task. MCP calls are cancelled, then awaited. Local tools run to their own bound (shell, storage, Azure timeouts), because cancelling `execute_local` orphans its subprocess (`local_tool_provider.py` ~519).
  4. Poll the sidecar until idle (A6).
  5. Revoke the token, then return or re-raise.
  - The worker stays alive until step 5, so the heartbeat stays fresh and `worker_cancellation_slow` fires; there is no release by elapsed time. Only loopback connection-refused counts as sidecar stopped (A7).
- D15 (C-1) Crash orphans: stale recovery is unchanged. The token and scope die with the process. On startup, `sweep.py` aborts and deletes tagged sidecar sessions (A1).
- D16 (Q5, C-3) Every failure runs D14, then returns `success: False`, and controllers apply §12 unchanged. Codes:
  - `UNAVAILABLE`: sidecar down, health or version failure; `TIMEOUT`: request timeout or segment deadline;
  - `ENGINE_PROTOCOL_ERROR`: malformed event or partial stream; `QUOTA`/`RATE_LIMIT`/`AUTH`/`UNAVAILABLE`: provider errors (A10);
  - `RUN_LIFECYCLE_FAILED`: ownership lost (D20); `ENGINE_TOOL_UNSUPPORTED`: requested tool unmapped (D22).
  - Raw sidecar bodies are never returned.
- D17 (In-3) The OpenCode stream supplies text, usage and completion. The bridge supplies authoritative `TOOL_CALL_*` events, `tool_calls_info` and counters. `event_sink` publishes via `agui_event_service` and sets `text_streamed`.
- D18 (In-5) `config.py`: `ORCHESTRATOR_ENGINE` (default `internal`), `OPENCODE_BASE_URL`, `OPENCODE_REQUEST_TIMEOUT_SEC=300`, `OPENCODE_MODEL`, `OPENCODE_PROVIDER=litellm|native`, and infrastructure `OPENCODE_TOOL_BRIDGE_URL` (default `http://127.0.0.1:8000`). `validate_config` checks the engine enum; when opencode, also a base URL without userinfo or query, a positive timeout, the provider enum, and non-empty `OPENCODE_MODEL` if native.
- D19 Out-1: OpenCode does not depend on `ModelClient`. Out-2 is not needed (discovery stays orchestrator-side).
- D20 (C-1, P002) New `RunLifecycleService.is_run_live(session, run_pk)` uses the `heartbeat_run` predicate narrowed to `status == active` (`run_lifecycle.py` ~408).
  - The bridge checks it before executing each call. False denies the call, marks the scope `ownership_lost`, runs D14 and returns `RUN_LIFECYCLE_FAILED`.
  - Residual: a call already running at reclaim time completes, as in the internal engine (§12 "cannot undo arbitrary tool side effects").
- D21 (C-2, P003) Primary: per-session MCP config carrying a per-session credential (A3).
  - Fallback if A3 fails: a process `asyncio.Lock` allows one session per sidecar, and a secret set at sidecar start maps calls only to the lock holder.
  - The lock is released after D14 step 5. Waiting counts toward `RUN_SEGMENT_DEADLINE_SEC` (`TIMEOUT`).
- D22 (C-2, P004) `engines/opencode/tool_map.py` `map_tools(entries) -> (mapped, rejected{name: reason})`: names in OpenCode's accepted charset/length, object-root `inputSchema` with allowlisted keywords only (A12).
  - Explicitly requested unmapped tools fail at segment start, before sidecar contact, with `ENGINE_TOOL_UNSUPPORTED` (as in-segment "Requested tools not found" does today).
  - With `tools` omitted, unmapped tools are dropped and logged (`opencode_tool_unmapped`).
- D23 (In-5, P006) `engines/opencode/model_route.py` `resolve_model_route(provider, opencode_model, caller_model)`: `litellm` uses the sidecar's LiteLLM provider with `OPENCODE_MODEL` or the caller model (A9a); `native` uses `OPENCODE_MODEL` in OpenCode's provider-qualified form (A9b). Native credentials live only in the sidecar environment.

### Unverified OpenCode assumptions (gated by Slice 3 spike, pinned version, fixtures)
- A1 HTTP API for health/version, session create (directory, system prompt, model/provider, tag), prompt, events, abort, status, list, delete. A2 built-ins can be disabled.
- A3 Per-session remote MCP config with a per-session header or URL credential, proven by a concurrent two-session fixture where each session's calls carry only its own credential.
- A4 Replaceable system prompt. A5 Text/usage/completion/error event shapes. A6 Abort plus observable idle. A7 No auto-resume after restart. A8 Subagents/compaction can be disabled.
- A9a LiteLLM OpenAI-compatible provider config and model id format. A9b Native provider-qualified model id format. A10 Provider error classification. A11 Non-interactive permissions.
- A12 MCP tool name and `inputSchema` keyword subset accepted end to end.
- Failures:
  - A2, A6, A11: stop Slices 5–6.
  - A3: use the D21 fallback.
  - A9b: `native` is rejected at config validation, documented.
  - A4, A7, A8, A10, A12: documented and narrowed.

### Delivery slices (each from `origin/on-going-dev`, PR into `on-going-dev`, no attribution; C-8; `internal` fallback after each)
1. Characterization (C-7). Entry: none. Exit: new tests green on unchanged code; no `src/` change.
2. Contract, internal extraction, identity (D1–D7). Entry: Slice 1 merged. Exit: suite green, new rows `internal/1`, mismatch tests pass, migration note written.
3. Spike (A1–A12). Entry: Slice 2 merged. Exit: pinned version, `tests/fixtures/opencode/<version>/` (litellm and native), findings in `docs/PLUGGABLE_EXECUTION_ENGINES.md`; no runtime code.
4. Config and gating (D9, D10, D18, D23 validation). Entry: no stopping Slice 3 failure. Exit: engine=opencode boots, and every run is rejected `ENGINE_CAPABILITY_UNSUPPORTED` (`headless_segment` off).
5. Bridge (D12, D13, D20–D22). Entry: Slice 4 merged. Exit: security, fence, scope, isolation and tool-map tests pass with fakes; route absent under internal.
6. Engine (D11, D14–D17, D23). Entry: Slice 5 merged. Exit: fixture-replay and failure tests pass, `headless_segment` on, headless execute completes.
- Follow-ups only (Out-3): Phase 5 parity per capability, Phase 6 rollout. Out-1/Out-2 ship independently.

### Test matrix (Q6; no DB, no network)
- In-1: `test_engine_contract.py`: result keys equal hub keys, envelope round-trip, unknown version rejected.
- In-2:
  - `test_engine_characterization.py` (Slice 1): orchestrator complete/await/fail/`MAX_TOOL_CALLS`/`TIMEOUT`, AG-UI `text_streamed`, legacy execute/resume, snapshot keys.
  - `test_engine_resolver.py`: fresh follows env, resume follows persisted, `ENGINE_MISMATCH` with no claim on 3 paths, internal resumes under opencode.
  - `test_engine_identity_persistence.py` (columns, revision chain); `test_engine_architecture.py` (import boundaries).
  - Unchanged and green: `test_claim_lifecycle`, `test_stale_claim_recovery`, `test_bounded_run_lifecycle`, `test_agui_cancellation`, `test_resume_idempotency`, `test_orchestrator_claim_lifecycle`, `test_agui_await_resume`, `test_llm_error_contract`.
- In-3:
  - `test_opencode_engine.py`: fixture replay over httpx `MockTransport` for both provider modes (completion, AG-UI TEXT, tool events, usage).
  - `test_opencode_model_route.py`: litellm precedence (caller vs `OPENCODE_MODEL`), native qualified id, native credentials absent from orchestrator requests.
  - `test_opencode_tool_bridge.py`:
    - tokens: scoping; cross-run and revoked tokens get 401;
    - policy: workflow write blocked, traversal and symlinks refused, budget;
    - leakage: user MCP token never sent to the sidecar; `task`/`ask_user`/AG-UI absent;
    - isolation: two concurrent runs with distinct tokens resolve only their own context; D21 lock-holder mode;
    - fence: a fake lifecycle stale-reclaims the run while the worker is live, so the next call is denied, no tool executes, and the result is `RUN_LIFECYCLE_FAILED`;
    - tool map: requested unmapped tool rejected before sidecar contact; unrequested unmapped tool dropped and logged.
  - `test_opencode_failures.py`:
    - sidecar failures: down, timeout, malformed, partial stream, deadline, close;
    - cancel during each tool class: blocking fake MCP (cancelled, then awaited); `execute_local` (completes, no orphan process); file write and shared-folder output (`to_thread`, awaited);
    - assertions: the claim is finalized only after every handler returns and sidecar idle is confirmed; a hung handler keeps the worker alive and the heartbeat fresh; late calls are refused; post-failure states match §12.
  - `test_opencode_startup_sweep.py`.
- In-4: `test_engine_capabilities.py`: each D10 gate leaves no `ExecutionRun` and `Execution` unchanged.
  - AG-UI `input` (shared_folder) and legacy `workspacePath` pass; neither is rejected before the claim.
  - Ignored `workspacePath` fails in-segment with the thread open. Internal is never gated.
- In-5: `test_engine_config.py` (D18, including native without `OPENCODE_MODEL`); extend `test_public_surface_config.py` (default `internal`, keys in both `.env.example`) and `test_log_url_redaction.py` (`OPENCODE_BASE_URL`).
- In-6: docs and `changes/` entries reviewed in each slice PR.

### Files
- New: `src/app/services/engines/{__init__,contract,base,capabilities,resolver,internal,errors}.py`; `engines/opencode/{client,engine,translate,bridge,scope,tool_map,model_route,sweep}.py`; `alembic/versions/20261124_engine_identity.py`; the tests above; `tests/fixtures/opencode/`.
- Edit:
  - controllers: `orchestrator_controller.py`, `ag_ui_controller.py`, `agent_controller.py`;
  - persistence: `models/execution_models.py`, `execution_state_service.py` (`create_state`), `run_lifecycle.py` (`try_create_active_run` engine params, `is_run_live`);
  - wiring and config: `main.py` (bridge mount, sweep), `config.py`, `src/.env/{localhost,docker}/.env.example`.
- Docs:
  - `readme.md`: Configuration "Execution engine"; Error codes `ENGINE_MISMATCH`, `ENGINE_CAPABILITY_UNSUPPORTED`, `ENGINE_PROTOCOL_ERROR`, `ENGINE_TOOL_UNSUPPORTED`; Running many instances (one sidecar per process, D21 capacity).
  - `docs/ORCHESTRATOR.md` §5, §10, §11, §12 (stop sequence, fence, migration head), §13, §15; `docs/PLUGGABLE_EXECUTION_ENGINES.md` status.
  - One `changes/YYYY-MM-DD-<slug>.md` per slice, named in the "Engine Phase" series.

### Risks
- AG-UI closures are large: Slice 2 swaps only the call expression, and Slice 1 tests guard behaviour.
- A shared multi-process sidecar breaks D14/D15/D21; this is a documented hard deployment constraint.
- The D20 fence adds one DB read per tool call, accepted for ownership safety.
- A long local tool delays stop until its own bound; `worker_cancellation_slow` makes this visible.
- The D21 fallback serializes OpenCode runs per process: a capacity cost, not a correctness one. Gateways must not expose `/internal/*`.
- OpenCode version drift: the pinned version is checked on health, and fixtures are re-recorded on upgrade.

### Validation
- Per slice: `python -m pytest` green with no DB or network; the new tests fail before the change where applicable.
- Slice 2: `alembic upgrade head` / `downgrade -1` on a disposable Postgres, plus `scripts/smoke_service.py` and `smoke_agui.py` under internal.
- Slice 6: a manual headless execute against the pinned sidecar, in both provider modes.
- After every slice, the characterization suite verifies that `ORCHESTRATOR_ENGINE=internal` is unchanged.
