# Plan — OpenCodeExecutionEngine implementation plan
Version: 4
Model: claude-opus-5-5-medium

## Response to review v3
| Point ID | Decision | Reason / change made |
|---|---|---|
| P004 | accepted | Confirmed: `_create_agui_execution` inserts a RUNNING `Execution` at `ag_ui_controller.py` ~1621. D10/D22 AG-UI gates now run right after binding resolution (~1598), before config/runtime assembly and ~1621. Tests assert no `Execution` and no `ExecutionRun` row on any gate rejection. |
| P007 | accepted | Confirmed: `manage_run` starts its watchdog with a full `RUN_SEGMENT_DEADLINE_SEC` after the claim, while §12 says that deadline also covers preparation. D24 starts one absolute deadline before gating and gives `manage_run` only the remainder. Cumulative-timeout tests added. |

## Plan

### What / where / why
Add an `ExecutionEngine` seam where controllers call `ToolExecutionHub.process_request`. Lifecycle (claims, heartbeat, deadline, close, persistence) stays in the controllers. Engine identity is persisted per run and per await state. A headless `OpenCodeExecutionEngine` sends every tool call back through an orchestrator-hosted, per-run, ownership-fenced tool bridge. Covers In-1..In-6; Out-1..Out-4 are untouched.

### Segment today (Q1)
- A segment is one `process_request(...)` task inside `session_close_service.manage_run`. It is reached through `_managed_hub_process` (`orchestrator_controller.py`) or `_stream_run` (`ag_ui_controller.py`, which wraps `_run_fresh_segment` and `_run_resume_segment`). `manage_run` owns the heartbeat, the `RUN_SEGMENT_DEADLINE_SEC` watchdog, `cancel_event` + `task.cancel()`, and awaiting the worker before release; the `ExecutionRun` comes from `run_lifecycle.try_create_active_run`.
- Legacy `agent_controller.py` execute and `resumeRun` call the hub without an `ExecutionRun` claim. They stay unchanged.
- The result is the hub dict: `success, response, error, error_code`, counters, tokens, `tool_calls_info, agui_tool_calls, text_streamed, awaits_response, pending_tools, pending_tool, interrupts, state`.
- The await snapshot is `_build_await_result()["state"]`, stored in `llm_states.state_payload`. Resume uses it with `resume_tool_results`, `resolve_resume_model` and `resolve_resume_max_tool_calls`. `prepare_hook_permission_resume_events` stays internal-only.

### Design decisions
- D1 (In-1) `engines/contract.py`: v1 `SegmentRequest` (current `process_request` kwargs + `event_sink` + optional `resolved_tools`, D22), `EngineEvent` (text/tool/usage), `SegmentResult` (TypedDict of the hub keys), `AwaitEnvelope {engine, contractVersion, payload}`, and `ENGINE_CONTRACT_VERSION = 1`.
- D2 (In-1) `engines/base.py` defines the `ExecutionEngine` protocol (`name`, `contract_version`, `capabilities()`, `async run_segment`). Failures return a `success: False` dict. `CancelledError` is re-raised only once all engine-owned work has stopped (D14).
- D3 (In-2) `engines/internal.py` forwards kwargs to the hub unchanged, with `event_sink=None`, and requires `resolved_tools is None`. `task_local` stays inside the hub.
- D4 (In-2) `engines/resolver.py`: `resolve_fresh_engine()` reads `ORCHESTRATOR_ENGINE`; `resolve_persisted_engine(name, version)` never reads the environment. Used at all six hub call sites.
- D5 (Q2, C-5) Identity lives in typed columns, not the payload: `engine VARCHAR(32) NOT NULL DEFAULT 'internal'` and `engine_contract_version INTEGER NOT NULL DEFAULT 1` on `execution_runs` and `llm_states`. `state_payload` is unchanged (C-6).
- D6 (Q2) Revision `20261124_engine_identity` (down-revision `20250816_phase3_lifecycle`). The server default backfills existing rows as internal/1, following the phase-3 adopt-if-present pattern, and the ORM models carry the same defaults. Downgrade drops the columns; `executions.config` is not rewritten.
- D7 (Q2, C-5) The mismatch check runs after state is loaded and before any claim, on orchestrator resume, the AG-UI hold lookup and legacy `resumeRun`.
  - REST: HTTP 409 `{"success":false,"errorCode":"ENGINE_MISMATCH","error":"Run state was created by engine '<e>' contract v<n>; this deployment cannot resume it"}`. AG-UI: `RunErrorEvent("ENGINE_MISMATCH: …")`.
  - State stays `AWAITING_RESPONSE` and no `ExecutionRun` is created. Internal state always resumes on internal.
- D8 (Q3) OpenCode v1 has no resume mode: no awaiting tool, no `llm_states` row, `durable_resume=false`. The first mode to add later (Out-3) is the translated snapshot, because a session reference pins resume to one pod's sidecar (D11) and breaks replica independence. Rejection is tested with a crafted `engine='opencode'` row.
- D9 (Q4, In-4) `engines/capabilities.py` names `headless_segment, workspace_file_ops, mcp_tools, streaming_output, agui_frontend_interrupts, durable_resume, hooks, permission_asks, subagents, context_compaction, workspace_free`. Internal reports all true. OpenCode reports `mcp_tools=subset` (D22), `headless_segment`, `workspace_file_ops` and `streaming_output` true, and the rest false.
- D10 (Q4, P004) `require_capabilities(engine, needs)` rejects with 400 / `RunErrorEvent` `ENGINE_CAPABILITY_UNSUPPORTED` naming the capability, and logs the denial. Gate points:
  - Orchestrator `execute`: after `segment_config` is read (~707), before the claim session (~710); `Execution` status is untouched.
  - AG-UI fresh: right after binding resolution (`except BindingError`, ~1598), before `sanitize_execution_config`/`ensure_runtime` (~1600–1619) and `_create_agui_execution` (~1621).
  - Earlier `ensure_thread_session` (~1449) and `_prepare_thread_claims` (~1474) run before any gate, as they already do for binding errors. They create no `Execution` or `ExecutionRun`, and the thread stays runnable.
  - Gates: non-empty `frontendTools` or AG-UI frontend tools; `workspace_free` when the AG-UI `input_binding is None`, and on legacy `executeRequest`; `HOOKS_ENABLED` with a non-empty `load_hooks_for_workspace(...)`; then tool resolution (D22), last.
  - In-segment re-checks fail as an attempt (§12): `local_context is None` (for example, `workspacePath` ignored when `ALLOW_INPLACE_WORKSPACE=false`), and hooks found after `ensure_input_workspace`.
- D11 (C-2) One loopback sidecar per orchestrator process, with no workspace mount. Each session is rooted in an empty per-run scratch directory, and the orchestrator-composed system prompt is sent as the session system prompt (A4).
- D12 (C-2) Bridge `engines/opencode/bridge.py`: an internal MCP route, mounted only when engine=opencode.
  - It exposes registry local tools plus `resolved_tools.mapped`, minus `task_local`, `ask_user_local` and AG-UI tools.
  - Each run gets a 256-bit in-memory bearer bound to `(run_pk, LocalToolContext, registry, budget, user MCP token)`. Bad tokens get 401.
  - Local calls go through `LocalToolProvider.execute` (tool policy, `resolve_within`, path policy, opener). The user MCP token never reaches the sidecar, and OpenCode built-in tools are disabled (A2).
- D13 (C-4) `maxToolCalls` resolves request → segment config → `MAX_TOOL_CALLS` and is counted in the bridge. `OPENCODE_REQUEST_TIMEOUT_SEC` is an infrastructure guard. `OPENCODE_MODEL`/`OPENCODE_PROVIDER` have no effect unless engine=opencode.
- D14 (C-1) The stop sequence runs on cancel, `CancelledError`, completion or any failure. It is shielded and swallows a repeated `task.cancel()`.
  1. Close bridge admission; late calls get a "run stopping" error and execute nothing.
  2. Send abort to the sidecar.
  3. Await every in-flight call in the run's `BridgeRunScope`. Each call runs in a task owned by the scope, not by the HTTP request. MCP calls are cancelled, then awaited. Local tools run to their own bound, because cancelling `execute_local` orphans its subprocess (`local_tool_provider.py` ~519).
  4. Poll the sidecar until it is idle (A6).
  5. Revoke the token, then return or re-raise.
  - The worker stays alive until step 5, so the heartbeat stays fresh and `worker_cancellation_slow` fires; nothing is released because time has passed. Only a loopback connection-refused counts as the sidecar having stopped (A7).
- D15 (C-1) Crash orphans: stale-claim recovery is unchanged. The token and scope die with the process. On startup, `sweep.py` aborts and deletes tagged sidecar sessions (A1).
- D16 (Q5, C-3) Every in-segment failure runs D14, then returns `success: False`; controllers apply §12 unchanged. Raw sidecar bodies are never returned. Codes:
  - `UNAVAILABLE` (sidecar down, health or version failure); `TIMEOUT` (request timeout or segment deadline); `ENGINE_PROTOCOL_ERROR` (malformed event, partial stream);
  - `QUOTA`/`RATE_LIMIT`/`AUTH`/`UNAVAILABLE` for provider errors (A10); `RUN_LIFECYCLE_FAILED` when ownership is lost (D20). `ENGINE_TOOL_UNSUPPORTED` is raised only before the claim (D22).
- D17 (In-3) The OpenCode stream supplies text, usage and completion. The bridge supplies the authoritative `TOOL_CALL_*` events, `tool_calls_info` and counters. `event_sink` publishes through `agui_event_service` and sets `text_streamed`.
- D18 (In-5) `config.py` adds:
  - `ORCHESTRATOR_ENGINE` (default `internal`), `OPENCODE_BASE_URL`, `OPENCODE_REQUEST_TIMEOUT_SEC=300`, `OPENCODE_MODEL`, `OPENCODE_PROVIDER=litellm|native`, and the infrastructure setting `OPENCODE_TOOL_BRIDGE_URL` (default `http://127.0.0.1:8000`).
  - `validate_config` checks the engine value. When it is opencode, it also requires a base URL without userinfo or query, a positive timeout, a valid provider, and a non-empty `OPENCODE_MODEL` for native.
- D19 OpenCode does not depend on `ModelClient` (Out-1). The discovery adapter (Out-2) is not needed, because discovery stays on the orchestrator side.
- D20 (C-1) A new `RunLifecycleService.is_run_live(session, run_pk)` uses the `heartbeat_run` predicate narrowed to `status == active` (`run_lifecycle.py` ~408). The bridge checks it before each call. If the run is not live, the call is denied, the scope is marked `ownership_lost`, D14 runs and the result is `RUN_LIFECYCLE_FAILED`. Residual: a call already running when the run is reclaimed completes, as with the internal engine (§12).
- D21 (C-2) Primary design: per-session MCP config with a per-session credential (A3). Fallback if A3 fails: a process `asyncio.Lock` allows one session per sidecar, and a secret set at sidecar start routes calls only to the lock holder. The lock is released after D14 step 5. Time spent waiting for it counts against the D24 deadline (`TIMEOUT`).
- D22 (C-2, In-4) `engines/opencode/tool_map.py` `resolve_engine_tools(requested, user_token) -> ResolvedTools{mapped, rejected{name: reason}}`, called last in the D10 gate on OpenCode fresh paths.
  - Listing uses `mcp_service.fetch_mcp_tools(user_access_token=credentials.inputAccessToken)`; per-server `MCP_FETCH_TIMEOUT_SEC` is unchanged. The whole call is bounded by `asyncio.timeout_at(D24 deadline)`; expiry returns 504 / `RunErrorEvent` `TIMEOUT` before any claim.
  - Mapping accepts names in OpenCode's allowed charset and length, and an object-root `inputSchema` using allowlisted keywords only (A12).
  - Any requested name that is missing or unmapped returns 400 / `RunErrorEvent` `ENGINE_TOOL_UNSUPPORTED` naming the tool and reason. When `tools` is omitted (always the case on AG-UI fresh, `requested_tools=None` ~1773), unmapped tools are dropped and logged (`opencode_tool_unmapped`).
  - `ResolvedTools` travels in `SegmentRequest.resolved_tools`. Engine and bridge use only that set and never re-fetch. The internal engine keeps in-segment `_select_tools` unchanged (C-6).
- D23 (In-5) `engines/opencode/model_route.py` `resolve_model_route(provider, opencode_model, caller_model)`: `litellm` uses the sidecar's LiteLLM provider with `OPENCODE_MODEL` or the caller's model (A9a); `native` uses `OPENCODE_MODEL` in OpenCode's provider-qualified form (A9b). Native credentials live only in the sidecar environment.
- D24 (C-1, C-4, P007) `engines/deadline.py` `SegmentDeadline(at=loop.time() + RUN_SEGMENT_DEADLINE_SEC)`, with `remaining()`.
  - It is created on OpenCode fresh paths before the first D10 gate: orchestrator `execute` entry (~697), and AG-UI fresh before binding resolution (~1569). This matches §12, where the segment deadline covers preparation.
  - `manage_run(..., deadline_sec: float | None = None)`: when it is set, the watchdog sleeps that long instead of `segment_deadline_sec`. `_managed_hub_process` and `_stream_run` pass it through.
  - The controller passes `deadline.remaining()`, computed right before `manage_run`. If `remaining() <= 0` before the claim, the request returns `TIMEOUT` with no rows. After the claim, a remainder of 0 or less fails as a `TIMEOUT` attempt (§12).
  - Internal paths never pass `deadline_sec`, so their watchdog is unchanged (C-6). No new configuration setting is added (C-4).

### Unverified OpenCode assumptions (gated by the Slice 3 spike, a pinned version, and fixtures)
- A1 HTTP API for health/version, session create (directory, system prompt, model/provider, tag), prompt, events, abort, status, list and delete. A2 Built-in tools can be disabled.
- A3 Per-session remote MCP config with a per-session header or URL credential, proven by a fixture with two concurrent sessions where each session's calls carry only its own credential.
- A4 Replaceable system prompt. A5 Text, usage, completion and error event shapes. A6 Abort plus an observable idle state. A7 No automatic resume after restart. A8 Subagents and compaction can be disabled.
- A9a LiteLLM OpenAI-compatible provider config and model id format. A9b Native provider-qualified model id format. A10 Provider error classification. A11 Non-interactive permissions. A12 MCP tool names and the `inputSchema` keyword subset accepted end to end.
- Outcomes: failure of A2, A6 or A11 stops Slices 5–6. A3 failure uses the D21 fallback. A9b failure rejects `native` at config validation, documented. A4, A7, A8, A10 and A12 are documented and the capability claims narrowed.

### Delivery slices (each from `origin/on-going-dev`, PR into `on-going-dev`, no attribution, C-8; `internal` is the fallback after each)
1. Characterization (C-7). Entry: none. Exit: new tests green on unchanged code; no `src/` change.
2. Contract, internal extraction and identity (D1–D7). Entry: Slice 1 merged. Exit: suite green, new rows are `internal/1`, mismatch tests pass, migration note written.
3. Spike (A1–A12). Entry: Slice 2 merged. Exit: pinned version, `tests/fixtures/opencode/<version>/` for litellm and native, findings recorded in `docs/PLUGGABLE_EXECUTION_ENGINES.md`; no runtime code.
4. Config and gating (D9, D10, D18, D23 validation). Entry: no stopping failure in Slice 3. Exit: engine=opencode boots and every run is rejected with `ENGINE_CAPABILITY_UNSUPPORTED` (`headless_segment` off).
5. Bridge, pre-claim tool gate and deadline (D12, D13, D20–D22, D24). Entry: Slice 4 merged. Exit: security, fence, scope, isolation, tool-gate and cumulative-deadline tests pass with fakes, using a fake engine that reports `headless_segment`; the bridge route is absent under internal.
6. Engine (D11, D14–D17, D23). Entry: Slice 5 merged. Exit: fixture-replay and failure tests pass, `headless_segment` on, and a headless execute completes.
- Follow-ups only (Out-3): Phase 5 parity per capability and Phase 6 rollout. Out-1 and Out-2 ship independently.

### Test matrix (Q6; no DB, no network)
- In-1: `test_engine_contract.py`: result keys equal hub keys, envelope round-trip, unknown version rejected, internal engine rejects a non-`None` `resolved_tools`.
- In-2:
  - `test_engine_characterization.py` (Slice 1): orchestrator complete, await, fail, `MAX_TOOL_CALLS` and `TIMEOUT`; AG-UI `text_streamed`; legacy execute and resume; snapshot keys; in-segment "Requested tools not found" under internal.
  - `test_engine_resolver.py`: fresh runs follow the environment, resume follows the persisted value, `ENGINE_MISMATCH` with no claim on all three paths, internal state resumes under opencode.
  - `test_engine_identity_persistence.py` (columns and revision chain); `test_engine_architecture.py` (import boundaries).
  - Unchanged and green: `test_claim_lifecycle`, `test_stale_claim_recovery`, `test_bounded_run_lifecycle`, `test_agui_cancellation`, `test_resume_idempotency`, `test_orchestrator_claim_lifecycle`, `test_agui_await_resume`, `test_llm_error_contract`.
- In-3:
  - `test_opencode_engine.py`: fixture replay over httpx `MockTransport` for both provider modes (completion, AG-UI text, tool events, usage); the engine never calls `fetch_mcp_tools`.
  - `test_opencode_model_route.py`: litellm precedence (caller model vs `OPENCODE_MODEL`), native qualified id, native credentials absent from orchestrator requests.
  - `test_opencode_tool_bridge.py`: token scoping, with cross-run and revoked tokens getting 401; policy (workflow write blocked, traversal and symlinks refused, budget); leakage (user MCP token never sent to the sidecar, `task`/`ask_user`/AG-UI tools absent, only `resolved_tools.mapped` exposed); isolation (two concurrent runs each resolve only their own context; D21 lock-holder mode); fence (a fake lifecycle stale-reclaims a live run, so the next call is denied, no tool executes and the result is `RUN_LIFECYCLE_FAILED`).
  - `test_opencode_failures.py`: sidecar down, timeout, malformed event, partial stream, deadline and close; cancel during each tool class (blocking fake MCP is cancelled then awaited; `execute_local` completes with no orphan process; file write and shared-folder output via `to_thread` are awaited). Assertions: the claim is finalized only after every handler returns and sidecar idle is confirmed; a hung handler keeps the worker alive and the heartbeat fresh; late calls are refused; post-failure states match §12.
  - `test_opencode_startup_sweep.py`.
- In-4: `test_engine_capabilities.py`:
  - Every D10 gate, including each AG-UI fresh rejection, leaves no `Execution` row and no `ExecutionRun` row (`create_execution` and `try_create_active_run` never called). On orchestrator, `Execution` status is unchanged. Internal is never gated.
  - AG-UI `input` (shared_folder) and legacy `workspacePath` pass the gates. An ignored `workspacePath` fails in-segment with the thread left open.
  - D22: requested missing or unmapped tools return `ENGINE_TOOL_UNSUPPORTED` with no rows and no sidecar contact. Unrequested unmapped tools are dropped and logged. The engine receives exactly the resolved set, `fetch_mcp_tools` runs once per request, and user-auth listing uses the request token.
  - D24 (small `RUN_SEGMENT_DEADLINE_SEC`, fake clock): resolution that outlasts the deadline returns pre-claim `TIMEOUT` with no rows; resolution taking 60% of the deadline followed by a blocking fake engine is cancelled with `TIMEOUT` at the deadline total, not 160% of it.
  - Extend `test_bounded_run_lifecycle.py`: `manage_run(deadline_sec=x)` fires at x; leaving `deadline_sec` out keeps `segment_deadline_sec`.
- In-5: `test_engine_config.py` (D18, including native without `OPENCODE_MODEL`); extend `test_public_surface_config.py` (default `internal`, keys present in both `.env.example` files) and `test_log_url_redaction.py` (`OPENCODE_BASE_URL`).
- In-6: docs and `changes/` entries are reviewed in each slice PR.

### Files
- New: `src/app/services/engines/{__init__,contract,base,capabilities,resolver,internal,errors,deadline}.py`; `engines/opencode/{client,engine,translate,bridge,scope,tool_map,model_route,sweep}.py`; `alembic/versions/20261124_engine_identity.py`; the tests above; `tests/fixtures/opencode/`.
- Edit:
  - Controllers: `orchestrator_controller.py` (gates, D24 deadline, `_managed_hub_process`), `ag_ui_controller.py` (gates before `_create_agui_execution`, D24, `_stream_run`), `agent_controller.py`.
  - Services and persistence: `session_close_service.py` (`manage_run` gains optional `deadline_sec`; its existing test callers are unaffected), `models/execution_models.py`, `execution_state_service.py` (`create_state`), `run_lifecycle.py` (engine params on `try_create_active_run`, `is_run_live`).
  - Wiring: `main.py` (bridge mount, sweep), `config.py`, `src/.env/{localhost,docker}/.env.example`.
- Docs:
  - `readme.md`: Configuration "Execution engine"; Lifecycle note that under opencode `RUN_SEGMENT_DEADLINE_SEC` also covers pre-claim tool resolution; Error codes `ENGINE_MISMATCH`, `ENGINE_CAPABILITY_UNSUPPORTED`, `ENGINE_PROTOCOL_ERROR`, `ENGINE_TOOL_UNSUPPORTED`; Tool selection; Running many instances (one sidecar per process, D21 capacity).
  - `docs/ORCHESTRATOR.md` §5, §10, §11, §12 (stop sequence, fence, pre-claim gates, single deadline, migration head), §13, §15; status in `docs/PLUGGABLE_EXECUTION_ENGINES.md`.
  - One `changes/YYYY-MM-DD-<slug>.md` per slice, in the "Engine Phase" series.

### Risks
- The AG-UI closures are large. Slice 2 swaps only the call expression, and the Slice 1 tests guard behaviour. Moving the gates ahead of ~1600–1621 touches only OpenCode branches.
- A sidecar shared by several processes breaks D14, D15 and D21. This is documented as a hard deployment constraint.
- The D20 fence adds one DB read per tool call, accepted for ownership safety.
- A long local tool delays stop until its own bound; `worker_cancellation_slow` makes the delay visible.
- D22 spends part of the D24 budget before the claim, so a request that then loses with `RUN_CONFLICT` has paid for the listing. This applies only to OpenCode. MCP servers that change between gate and run surface as the bridge's call-time error.
- The D21 fallback serializes OpenCode runs per process: a capacity cost, not a correctness one. Gateways must not expose `/internal/*`.
- OpenCode version drift: the pinned version is checked on health, and fixtures are re-recorded on upgrade.

### Validation
- Per slice: `python -m pytest` passes with no DB or network, and the new tests fail before the change where applicable.
- Slice 2: `alembic upgrade head` and `downgrade -1` on a disposable Postgres, plus `scripts/smoke_service.py` and `smoke_agui.py` under internal.
- Slice 5: the cumulative-deadline tests and the unchanged `test_bounded_run_lifecycle` both pass.
- Slice 6: a manual headless execute against the pinned sidecar, in both provider modes.
- After every slice, the characterization suite confirms that `ORCHESTRATOR_ENGINE=internal` behaves exactly as before.
