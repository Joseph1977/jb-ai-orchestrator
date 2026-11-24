# Plan — OpenCodeExecutionEngine implementation plan (replan)
Version: 1
Model: claude-opus-5-5-medium

## Plan

### What / where / why
Add an `ExecutionEngine` seam at the six controller calls of `ToolExecutionHub.process_request` (orchestrator ~770/~992, AG-UI ~1391/~1768, legacy ~175/~274); the hub's own subagent recursion (~1564) stays inside the hub. Lifecycle (claim, heartbeat, watchdog, close, persistence) stays in the controllers. Engine identity is persisted per attempt and per await state. A headless `OpenCodeExecutionEngine` routes every tool call back through an orchestrator-hosted, per-run, fenced tool bridge. Covers In-1..In-6; Out-1..Out-4 untouched.

Changes from the input draft (v4), each verified in the repo:
- Tool resolution and the hook check move inside the managed segment, under the existing `manage_run` watchdog. This drops the draft's `SegmentDeadline` and `manage_run(deadline_sec=)`, so the P007 double deadline cannot occur. Evidence: `manage_run` starts the watchdog with the full `segment_deadline_sec`, and orchestrator `_ensure_workspace_for_segment` already runs inside the task.
- P004 is kept, with the AG-UI gates moved earlier, to the start of the fresh branch (~1560). They read request shape only, so they run before binding resolution, `ensure_runtime`, `_create_agui_execution` (~1621) and the claim (~1632).
- The legacy `executeRequest` gate runs before `_create_execution_record()` (~158), which creates an `Execution` row; the draft did not place this gate.
- New: bridge MCP calls rebind the `bind_mcp_user_token` / `bind_request_mcp_tools` context variables, because the bridge's HTTP handler does not inherit the segment's context.
- New: A13 (seeding prior AG-UI turns, since `build_initial_messages` sends the full history), a multi-worker bridge constraint, and doc targets `docs/DOCUMENTATION_INDEX.md` (migration head) and the `docs/ORCHESTRATOR.md` §15 code map.

### Segment today (Q1)
- An orchestrator or AG-UI segment is one hub task inside `session_close_service.manage_run`, reached through `_managed_hub_process` (orchestrator) or `_stream_run` (AG-UI, wrapping `_run_fresh_segment` / `_run_resume_segment`). `manage_run` owns the heartbeat, the `RUN_SEGMENT_DEADLINE_SEC` watchdog, the cancel event plus `task.cancel()`, and awaiting the worker before unregistering. The claim comes from `run_lifecycle.try_create_active_run`.
- Legacy `agent_controller` `executeRequest` and `resumeRun` call the hub with no `ExecutionRun`, no watchdog and no local context.
- Result is the hub dict: `success, response, error, error_code`, counters, tokens, `tool_calls_info, agui_tool_calls, text_streamed, awaits_response, pending_tools, pending_tool, interrupts, state`.
- The await snapshot is `_build_await_result()["state"]` (tool_hub ~857), stored in `llm_states.state_payload` by `create_state` from orchestrator ~328, AG-UI ~599 and legacy ~132. Resume reads it with `resolve_resume_model` / `resolve_resume_max_tool_calls`; `prepare_hook_permission_resume_events` stays internal-only.

### Design decisions
- D1 (In-1) `engines/contract.py` defines the v1 schemas: `SegmentRequest` (today's `process_request` kwargs plus `event_sink` and `engine_scope`), `EngineEvent` (text, tool, usage), `SegmentResult` (TypedDict of the hub keys), `AwaitEnvelope {engine, contractVersion, payload}`, and `ENGINE_CONTRACT_VERSION = 1`.
- D2 (In-1) `engines/base.py` defines the `ExecutionEngine` protocol: `name`, `contract_version`, `capabilities()`, `async run_segment(req) -> SegmentResult`. Failures return a `success: False` dict; `CancelledError` is re-raised only after all engine-owned work has stopped (D12).
- D3 (In-2) `engines/internal.py` forwards kwargs to the hub unchanged, ignores `event_sink`, and reuses the `get_tool_hub()` singleton (agent_controller ~56).
- D4 (In-2) `engines/resolver.py`, used at all six call sites: `resolve_fresh_engine()` reads `Config.ORCHESTRATOR_ENGINE`; `resolve_persisted_engine(name, version)` never reads the environment and raises `EngineMismatch` for an unknown name or version.
- D5 (Q2, C-5) Identity lives in typed columns on `execution_runs` and `llm_states`: `engine VARCHAR(32) NOT NULL server_default 'internal'` and `engine_contract_version INTEGER NOT NULL server_default 1`. `state_payload` and `executions.config` are unchanged (C-6). `try_create_active_run` and `create_state` gain kwargs defaulting to internal/1, so existing callers and tests are unaffected.
- D6 (Q2) Revision `20261124_engine_identity` (`down_revision = "20250816_phase3_lifecycle"`). Server defaults backfill existing rows as internal/1; each column is added only if absent, following the phase-3 adoption pattern. Downgrade drops both columns. A revision-chain test follows `test_run_lifecycle_phase3.py`.
- D7 (Q2, C-5) The mismatch check runs after state load and before any claim or state change. State stays `AWAITING_RESPONSE`, no `ExecutionRun` is created, and internal state always resumes on internal:
  - Orchestrator resume, after `get_state` (~890) and before `try_claim_state_for_resume` (~935): HTTP 409 `{"success":false,"errorCode":"ENGINE_MISMATCH","error":"Run state was created by engine '<e>' contract v<n>; this deployment cannot resume it"}`, the same shape as `_run_conflict_payload`.
  - AG-UI, after the hold lookup (~1511) and in `_handle_tool_response` before its claim: `RunErrorEvent("ENGINE_MISMATCH: …")`.
  - Legacy `resumeRun`, after state load (~251): `HTTPException(409, "ENGINE_MISMATCH: …")`, matching the other legacy errors.
- D8 (Q3) OpenCode v1 has no resume mode: no awaiting tool, no `llm_states` row, `durable_resume=false`.
  - The first mode added later (Out-3) is the translated snapshot, because a session reference pins resume to one process's sidecar (D9) and breaks replica independence (`docs/ORCHESTRATOR.md` §10). Tenant and workspace controls apply to any future snapshot through the persisted sanitized bindings (§12).
  - A fresh OpenCode AG-UI run discarding an internal stale hold (`_discard_stale_awaiting_and_cleanup_orphans`) is the existing thread-reset rule, not a resume.
- D9 (C-2) One loopback sidecar per orchestrator process, with no workspace mount. Each session is rooted in an empty sidecar-side scratch directory (A1), the orchestrator-composed system prompt is the session system prompt (A4), and built-in tools, subagents and compaction are disabled (A2, A8).
- D10 (Q4, In-4) `engines/capabilities.py` reports `headless_segment, workspace_file_ops, mcp_tools, streaming_output, agui_frontend_interrupts, durable_resume, hooks, permission_asks, subagents, context_compaction, workspace_free`. Internal reports all true. OpenCode reports `headless_segment`, `workspace_file_ops` and `streaming_output` true, `mcp_tools=subset`, and the rest false.
- D11 (Q4, In-4, P004) `require_capabilities(engine, needs)` rejects with 400 `{"success":false,"errorCode":"ENGINE_CAPABILITY_UNSUPPORTED","error":"<capability> is not supported by engine '<e>'"}`, or the equivalent AG-UI `RunErrorEvent`, and logs `engine_capability_denied`. Internal is never gated. Pre-claim gates read only the request:
  - Orchestrator `execute`, after `_segment_config` (~707) and before the claim session (~710): `agui_frontend_interrupts` when `frontendTools` is set.
  - AG-UI fresh, at the start of the fresh branch (~1560): `agui_frontend_interrupts` for non-empty `frontend_tools`; `workspace_free` when there is no `input` and no `workspacePath`. The earlier `ensure_thread_session` (~1449) and `_prepare_thread_claims` (~1474) create no `Execution` or run rows, and the global `refresh_frontend_tools` (~1430), which serves listing and bind-only runs, is unchanged.
  - Legacy `executeRequest`, before `_create_execution_record()` (~158): `workspace_free`, so every legacy execute is rejected under opencode.
- In-segment checks fail as an attempt with the §12 transitions, before any sidecar contact:
  - `local_context is None`, for example a `workspacePath` ignored because `ALLOW_INPLACE_WORKSPACE=false`.
  - `HOOKS_ENABLED` with a non-empty `load_hooks_for_workspace` result (hooks/loader ~256), checked after `ensure_input_workspace`, because AG-UI copy input does not exist before the claim and discovery is synchronous (§12, Known issue).
- D12 (C-1) Stop sequence, run on cancel, `CancelledError`, completion or failure. It is shielded and swallows a repeated `task.cancel()`:
  1. Close bridge admission; late calls get "run stopping" and execute nothing.
  2. Send abort to the sidecar (A6).
  3. Await every in-flight call in the run's `BridgeRunScope`. Each call runs in a scope-owned task, not the HTTP request's. MCP calls are cancelled, then awaited. Local tools run to their own bound, because cancelling the shell call orphans its subprocess (local_tool_provider ~519).
  4. Poll until the sidecar reports idle (A6). Only a loopback connection-refused counts as stopped (A7).
  5. Revoke the token, then return or re-raise.
  - The worker lives until step 5, so `manage_run` keeps the heartbeat fresh and `worker_cancellation_slow` fires; nothing is released because time has passed (§12).
- D13 (C-2) `engines/opencode/bridge.py` is an internal MCP route, mounted in `main.py` only when engine=opencode.
  - It exposes registry local tools plus the in-segment resolved MCP set (D16), minus `task_local`, `ask_user_local` and AG-UI tools.
  - Each run gets a 256-bit in-memory bearer bound to `(run_pk, LocalToolContext, resolved tools, budget, user MCP token)`; unknown or revoked tokens get 401.
  - Local calls go through `LocalToolProvider.execute` (~1083: tool policy, `resolve_within`, path policy). MCP calls run with the context variables rebound from the scope. The user MCP token and native provider credentials never reach the sidecar.
- D14 (C-4) `maxToolCalls` resolves as today (request → segment config → `MAX_TOOL_CALLS`) and is counted in the bridge; exceeding it denies the call, runs D12 and returns `MAX_TOOL_CALLS`. `OPENCODE_REQUEST_TIMEOUT_SEC` is a per-request idle guard; the total is bounded by the existing `RUN_SEGMENT_DEADLINE_SEC` watchdog.
- D15 (C-1) Ownership fence: a new `RunLifecycleService.is_run_live(session, run_pk)` reuses the `heartbeat_run` predicate (run_lifecycle ~408), narrowed to `active`. The bridge checks it before each call; on failure the call is denied, D12 runs and the result is `RUN_LIFECYCLE_FAILED`. It is needed because the heartbeat loop (session_close_service ~365) ignores a `False` rowcount, so a reclaimed run would keep calling tools. The internal path is unchanged (C-6, C-7).
- D16 (C-2, In-4) `engines/opencode/tool_map.py` `resolve_engine_tools(requested, user_token) -> ResolvedTools{mapped, rejected}` runs inside the managed segment after workspace setup, bounded by the existing watchdog, with one `fetch_mcp_tools(user_access_token=...)` call.
  - A missing or unmapped requested tool fails the attempt with `ENGINE_TOOL_UNSUPPORTED`, in the same in-segment place as the internal "Requested tools not found" (tool_hub ~1650).
  - When `tools` is omitted (always on AG-UI fresh, ~1773), unmapped tools are dropped and logged as `opencode_tool_unmapped` (A12).
- D17 (Q5, C-3) Every in-segment failure runs D12, then returns `success: False`; controllers apply §12 unchanged, and raw sidecar bodies are never returned. Codes by failure mode:
  - `UNAVAILABLE`: health or version mismatch (checked each segment before session start), or sidecar down mid-run.
  - `TIMEOUT`: request idle timeout, or the existing watchdog deadline.
  - `ENGINE_PROTOCOL_ERROR`: malformed event or partial stream.
  - `QUOTA`, `RATE_LIMIT`, `AUTH` or `UNAVAILABLE`: provider errors (A10).
  - `RUN_LIFECYCLE_FAILED`: the D15 fence.
  - A hung abort returns no code; the worker and claim stay held, per §12.
- D18 (C-1) Crash orphans: stale-claim recovery is unchanged, and the token and scope die with the process. `engines/opencode/sweep.py` aborts and deletes this process's tagged sidecar sessions at startup (A1).
- D19 (In-3) The OpenCode stream supplies text, usage and completion. The bridge supplies the authoritative `TOOL_CALL_*` events, `tool_calls_info` and counters. `event_sink` publishes through `agui_event_service` and sets `text_streamed`; prior AG-UI turns are seeded per A13.
- D20 (In-5) `config.py` adds `ORCHESTRATOR_ENGINE` (default `internal`), `OPENCODE_BASE_URL`, `OPENCODE_REQUEST_TIMEOUT_SEC=300`, `OPENCODE_MODEL`, `OPENCODE_PROVIDER=litellm|native`, and the infrastructure setting `OPENCODE_TOOL_BRIDGE_URL` (default `http://127.0.0.1:8000`). `validate_config` (~384) checks the engine value; only for opencode does it also require a base URL with no userinfo or query, a positive timeout, a valid provider, and a non-empty `OPENCODE_MODEL` for native.
- D21 (In-5, C-4) `engines/opencode/model_route.py`: the model is the caller-resolved model with today's precedence; `OPENCODE_MODEL`, empty by default, overrides it only when set. `litellm` uses the sidecar's LiteLLM provider (A9a); `native` uses an OpenCode provider-qualified id (A9b), with credentials only in the sidecar environment.
- D22 (C-2) Session isolation: the primary design is a per-session MCP config with a per-session credential (A3). If A3 fails, a process `asyncio.Lock` allows one session per sidecar. It is acquired inside the segment, so the watchdog bounds the wait; a sidecar-start secret routes bridge calls only to the lock holder, and the lock is released after D12 step 5.
- D23 OpenCode does not depend on `ModelClient` (Out-1). The discovery adapter (Out-2) is not needed, because discovery stays on the orchestrator side.

### Unverified OpenCode assumptions (gated by the Slice 3 spike, a pinned version and recorded fixtures)
- A1 The HTTP API offers health/version, session create (directory, system prompt, model/provider, tag), prompt, events, abort, status, list and delete; the directory may be a sidecar-local empty path. A2 Built-in tools can be disabled.
- A3 Per-session remote MCP config takes a per-session header or URL credential, proven by a two-concurrent-session fixture where each session's calls carry only its own credential.
- A4 Replaceable system prompt. A5 Text, usage, completion and error event shapes. A6 Abort plus an observable idle state. A7 No automatic resume after a sidecar restart. A8 Subagents and compaction can be disabled.
- A9a LiteLLM OpenAI-compatible provider config and model id format. A9b Native provider-qualified id format. A10 Provider error classification. A11 Non-interactive permissions. A12 Accepted MCP tool name charset and `inputSchema` keyword subset.
- A13 A session can be seeded with prior user and assistant turns. Fallback: render them as a delimited transcript in the first prompt.
- Outcomes: if A2, A6 or A11 fails, Slices 5–6 stop. If A3 fails, use the D22 fallback. If A9b fails, `native` is rejected at config validation. If A4, A7, A8, A10, A12 or A13 differs, document it and narrow the claims.

### Delivery slices (each from `origin/on-going-dev`, PR into `on-going-dev`, no attribution, C-8; `internal` stays the fallback after each)
1. Characterization (C-7). Entry: none. Exit: the new tests are green on unchanged code; no `src/` change.
2. Contract, internal extraction and identity (D1–D7). Entry: Slice 1 merged. Exit: suite green, new rows are internal/1, mismatch tests pass, migration note written.
3. Spike (A1–A13). Entry: Slice 2 merged. Exit: pinned version, `tests/fixtures/opencode/<version>/` for both providers, findings in `docs/PLUGGABLE_EXECUTION_ENGINES.md`; no runtime code.
4. Config and gating (D10, D11, D20, D21 validation). Entry: no stopping failure in Slice 3. Exit: engine=opencode boots, and every run is rejected as `ENGINE_CAPABILITY_UNSUPPORTED` (`headless_segment` off).
5. Bridge, fence and tool map (D13–D16, D22). Entry: Slice 4 merged. Exit: security, fence, scope, isolation and tool-map tests pass with a fake engine that reports `headless_segment`; the route is absent under internal.
6. Engine (D9, D12, D17–D19, D21). Entry: Slice 5 merged. Exit: fixture-replay and failure tests pass, `headless_segment` on, and a headless execute completes.
- Follow-ups only (Out-3): Phase 5 parity per capability, then Phase 6 rollout. Out-1 and Out-2 ship independently.

### Test matrix (Q6; no DB, no network)
- In-1: `test_engine_contract.py`: result keys equal hub keys, envelope round-trip, unknown version rejected.
- In-2:
  - `test_engine_characterization.py` (Slice 1): orchestrator complete, await, fail, `MAX_TOOL_CALLS` and `TIMEOUT`; AG-UI `text_streamed`; legacy execute and resume; snapshot keys; in-segment "Requested tools not found".
  - `test_engine_resolver.py`: fresh runs follow the environment and resume follows the persisted value. On all three paths, `ENGINE_MISMATCH` makes no claim and leaves the state `AWAITING_RESPONSE`. Internal state resumes under opencode.
  - `test_engine_identity_persistence.py` (columns, defaults, revision chain); `test_engine_architecture.py` (engine imports stay behind `engines/`).
  - Unchanged and green: `test_claim_lifecycle`, `test_stale_claim_recovery`, `test_bounded_run_lifecycle`, `test_agui_cancellation`, `test_resume_idempotency`, `test_orchestrator_claim_lifecycle`, `test_agui_await_resume`, `test_llm_error_contract`, `test_run_lifecycle_phase3`.
- In-3:
  - `test_opencode_engine.py`: fixture replay over httpx `MockTransport` for both providers, covering completion, AG-UI text, tool events, usage and A13 seeding. `test_opencode_model_route.py`: precedence and the `OPENCODE_MODEL` override.
  - `test_opencode_tool_bridge.py`: cross-run and revoked tokens get 401; workflow write blocked, traversal and symlinks refused, budget ends in `MAX_TOOL_CALLS`; the user token never reaches the sidecar and excluded tools are absent; two concurrent runs see only their own context and MCP token; D22 lock-holder mode; a stale-reclaimed run is denied and nothing executes (`RUN_LIFECYCLE_FAILED`).
  - `test_opencode_failures.py`: each D17 mode, and cancel during each tool class (blocking MCP cancelled then awaited; shell completes with no orphan; file writes awaited). It asserts the claim is finalized only after handlers return and the sidecar is idle, a hung abort keeps the heartbeat fresh, late calls are refused, and post-failure states match §12.
  - `test_opencode_startup_sweep.py`.
- In-4: `test_engine_capabilities.py`:
  - Every pre-claim gate leaves no `Execution` or `ExecutionRun` row: `create_execution`, `_create_execution_record` and `try_create_active_run` are never called, and orchestrator `Execution` status is unchanged.
  - The in-segment hook, null-context and unmapped-tool checks fail as attempts with no sidecar contact; the thread stays open and the orchestrator execution returns to `PENDING`.
  - Tool resolution that outlasts a small `RUN_SEGMENT_DEADLINE_SEC` ends as `TIMEOUT` through the existing watchdog.
- In-5: `test_engine_config.py` (D20, including native without a model). Extend `test_public_surface_config.py` (default `internal`; keys in both `.env.example` files) and `test_log_url_redaction.py` (`OPENCODE_BASE_URL`).
- In-6: docs and `changes/` entries are reviewed in each slice PR.

### Files
- New: `src/app/services/engines/{__init__,contract,base,capabilities,resolver,internal,errors}.py`; `engines/opencode/{client,engine,translate,bridge,scope,tool_map,model_route,sweep}.py`; `alembic/versions/20261124_engine_identity.py`; the tests above; `tests/fixtures/opencode/`.
- Edit: `orchestrator_controller.py`, `ag_ui_controller.py` and `agent_controller.py` (resolver, gates, mismatch); `run_lifecycle.py` (engine kwargs, `is_run_live`); `execution_state_service.py` (`create_state`); `models/execution_models.py`; `main.py` (bridge mount, sweep in `lifespan`); `config.py`; `src/.env/{localhost,docker}/.env.example`. `session_close_service.py` is unchanged.
- Docs:
  - `readme.md`: Configuration "Execution engine"; Error codes `ENGINE_MISMATCH`, `ENGINE_CAPABILITY_UNSUPPORTED`, `ENGINE_PROTOCOL_ERROR`, `ENGINE_TOOL_UNSUPPORTED`; Tool selection; Running many instances (one process per sidecar).
  - `docs/ORCHESTRATOR.md` §5, §10, §11, §12 (stop sequence, fence, gates, migration head), §13 and the §15 code map; `docs/DOCUMENTATION_INDEX.md` (migration head); status in `docs/PLUGGABLE_EXECUTION_ENGINES.md`.
  - One `changes/YYYY-MM-DD-<slug>.md` per slice; Slice 2 includes the migration note.

### Risks
- The AG-UI closures are large. Slice 2 swaps only the call expressions, and the Slice 1 tests guard behaviour.
- Bridge tokens live in process memory, so a bridge request routed to another worker gets 401. That fails closed as a tool error, never cross-run access. Multi-worker processes and shared sidecars are unsupported under opencode, documented as a hard constraint.
- The fence adds one DB read per bridge call, accepted for ownership safety. A long local tool delays stop until its own bound; `worker_cancellation_slow` makes the delay visible.
- An unmapped requested tool costs an attempt row, matching the internal engine's in-segment tool rejection.
- The D22 fallback serializes runs per process, a capacity cost rather than a correctness one. Gateways must not expose `/internal/*`.
- Version drift: the pinned version is checked at segment start, and fixtures are re-recorded on upgrade.

### Validation
- Per slice: `python -m pytest` passes with no DB or network, and the new tests fail before the change where applicable.
- Slice 2: `alembic upgrade head` and `downgrade -1` on a disposable Postgres, plus `scripts/smoke_service.py` and `scripts/smoke_agui.py` under internal.
- Slice 6: a manual headless execute against the pinned sidecar, in both provider modes.
- After every slice, the characterization suite confirms that `ORCHESTRATOR_ENGINE=internal` behaves exactly as before.
