# Plan — OpenCodeExecutionEngine implementation plan (replan)
Version: 4
Model: claude-opus-5-5-medium

## Response to review v3
| Point ID | Decision | Reason / change made |
|---|---|---|
| P001 | accepted | §12 (~1131) puts binding prep inside RUN_SEGMENT_DEADLINE_SEC. D24: under opencode one absolute SegmentDeadline starts before gating (AG-UI ~1560, orchestrator ~707); pre-claim expiry fails with TIMEOUT, no claim; manage_run(deadline_sec=remainder). Internal unchanged (C-6). |

## Plan

### What / where / why
Add an `ExecutionEngine` seam at the six controller calls of `ToolExecutionHub.process_request` (orchestrator ~770/~992, AG-UI ~1391/~1768, legacy ~175/~274); the hub's subagent recursion (~1564) stays inside the hub. Lifecycle (claim, heartbeat, watchdog, close, persistence) stays in the controllers. Engine identity is persisted per attempt and per await state. A headless `OpenCodeExecutionEngine` routes every tool call through an orchestrator-hosted, per-run, fenced tool bridge. Covers In-1..In-6; Out-1..Out-4 untouched.

Changes from the input draft (v4), verified in the repo:
- Tool resolution runs inside the managed segment; pre-claim gates do no I/O (D11).
- Under opencode one absolute `SegmentDeadline` starts before gating and covers pre-claim binding prep (binding parse ~1570, `ensure_runtime` ~1609, `_create_agui_execution` ~1621, claim ~1632), per §12 (~1131); `manage_run` receives only the remainder, so there is a single watchdog, never a double deadline (D24). Internal is unchanged (C-6, C-7).
- P004 kept: AG-UI gates sit at the start of the fresh branch (~1560), before binding resolution, `ensure_runtime`, `_create_agui_execution` and the claim; legacy gate before `_create_execution_record()` (~158).
- Hooks under opencode are refused at config load, not discovered per segment (D20).
- Bridge MCP calls rebind `bind_mcp_user_token` / `bind_request_mcp_tools`, since the bridge HTTP handler does not inherit segment context. Adds A13 history seeding and a single-worker constraint.

### Segment today (Q1)
- An orchestrator or AG-UI segment is one hub task inside `session_close_service.manage_run`, via `_managed_hub_process` (orchestrator) or `_stream_run` (AG-UI, wrapping `_run_fresh_segment` / `_run_resume_segment`). `manage_run` owns heartbeat, `RUN_SEGMENT_DEADLINE_SEC` watchdog, cancel event plus `task.cancel()`, and awaits the worker before unregistering. Claim: `run_lifecycle.try_create_active_run`.
- Legacy `agent_controller` `executeRequest` and `resumeRun` call the hub with no `ExecutionRun`, watchdog or local context.
- Result is the hub dict: `success, response, error, error_code`, counters, tokens, `tool_calls_info, agui_tool_calls, text_streamed, awaits_response, pending_tools, pending_tool, interrupts, state`.
- Await snapshot is `_build_await_result()["state"]` (tool_hub ~857), stored in `llm_states.state_payload` by `create_state` (orchestrator ~328, AG-UI ~599, legacy ~132); resume reads it via `resolve_resume_model` / `resolve_resume_max_tool_calls`; `prepare_hook_permission_resume_events` stays internal-only.

### Design decisions
- D1 (In-1) `engines/contract.py`: `SegmentRequest` (today's `process_request` kwargs plus `event_sink`, `engine_scope`), `EngineEvent` (text, tool, usage), `SegmentResult` (TypedDict of hub keys), `AwaitEnvelope {engine, contractVersion, payload}`, `ENGINE_CONTRACT_VERSION = 1`.
- D2 (In-1) `engines/base.py` `ExecutionEngine` protocol: `name`, `contract_version`, `capabilities()`, `async run_segment(req) -> SegmentResult`. Failures return `success: False`; `CancelledError` is re-raised only after engine-owned work has stopped (D12).
- D3 (In-2) `engines/internal.py` forwards kwargs to the hub unchanged, ignores `event_sink`, reuses the `get_tool_hub()` singleton (agent_controller ~56).
- D4 (In-2) `engines/resolver.py` at all six call sites: `resolve_fresh_engine()` reads `Config.ORCHESTRATOR_ENGINE`; `resolve_persisted_engine(name, version)` never reads the environment and raises `EngineMismatch` for unknown name or version.
- D5 (Q2, C-5) Typed columns on `execution_runs` and `llm_states`: `engine VARCHAR(32) NOT NULL server_default 'internal'`, `engine_contract_version INTEGER NOT NULL server_default 1`. `state_payload` and `executions.config` unchanged (C-6). `try_create_active_run` and `create_state` gain kwargs defaulting to internal/1.
- D6 (Q2) Revision `20261124_engine_identity` (`down_revision = "20250816_phase3_lifecycle"`); server defaults backfill existing rows as internal/1; add-if-absent per the phase-3 adoption pattern; downgrade drops both columns; chain test follows `test_run_lifecycle_phase3.py`.
- D7 (Q2, C-5) Mismatch check after state load, before any claim or state change; state stays `AWAITING_RESPONSE`, no `ExecutionRun`; internal state always resumes on internal:
  - Orchestrator resume, after `get_state` (~890), before `try_claim_state_for_resume` (~935): HTTP 409 `{"success":false,"errorCode":"ENGINE_MISMATCH","error":"Run state was created by engine '<e>' contract v<n>; this deployment cannot resume it"}`, shaped like `_run_conflict_payload`.
  - AG-UI, after the hold lookup (~1511) and in `_handle_tool_response` before its claim: `RunErrorEvent("ENGINE_MISMATCH: …")`.
  - Legacy `resumeRun`, after state load (~251): `HTTPException(409, "ENGINE_MISMATCH: …")`.
- D8 (Q3) OpenCode v1 has no resume mode: no awaiting tool, no `llm_states` row, `durable_resume=false`.
  - First later mode (Out-3) is translated snapshot: a session reference pins resume to one process's sidecar (D9), breaking replica independence (`docs/ORCHESTRATOR.md` §10). Tenant/workspace controls apply through persisted sanitized bindings (§12).
  - A fresh OpenCode AG-UI run discarding an internal stale hold (`_discard_stale_awaiting_and_cleanup_orphans`) is the existing thread-reset rule, not a resume.
- D9 (C-2) One loopback sidecar per orchestrator process, no workspace mount; each session rooted in an empty sidecar-side scratch dir (A1); orchestrator-composed system prompt is the session system prompt (A4); built-in tools, subagents, compaction disabled (A2, A8).
- D10 (Q4, In-4) `engines/capabilities.py` reports `headless_segment, workspace_file_ops, mcp_tools, streaming_output, agui_frontend_interrupts, durable_resume, hooks, permission_asks, subagents, context_compaction, workspace_free`. Internal: all true. OpenCode: `headless_segment`, `workspace_file_ops`, `streaming_output` true, `mcp_tools=subset`, rest false; `hooks` is enforced at config load (D20).
- D11 (Q4, In-4, P004) `require_capabilities(engine, needs)` rejects with 400 `{"success":false,"errorCode":"ENGINE_CAPABILITY_UNSUPPORTED","error":"<capability> is not supported by engine '<e>'"}` or the AG-UI `RunErrorEvent`, logging `engine_capability_denied`. Internal is never gated. Gates are synchronous pure functions of the request and `Config` (no await, DB, filesystem or network); they run after the D24 deadline starts:
  - Orchestrator `execute`, after `_segment_config` (~707), before the claim session (~710): `agui_frontend_interrupts` when `frontendTools` is set.
  - AG-UI fresh, at the start of the fresh branch (~1560): `agui_frontend_interrupts` for non-empty `frontend_tools`; `workspace_free` when no `input` and no `workspacePath`. `ensure_thread_session` (~1449), `_prepare_thread_claims` (~1474) and global `refresh_frontend_tools` (~1430) are unchanged.
  - Legacy `executeRequest`, before `_create_execution_record()` (~158): `workspace_free`, so every legacy execute is rejected under opencode.
- In-segment check, failing as an attempt with §12 transitions before sidecar contact: `local_context is None` (e.g. `workspacePath` ignored with `ALLOW_INPLACE_WORKSPACE=false`). No per-segment hook discovery is added.
- D12 (C-1) Stop sequence on cancel, `CancelledError`, completion or failure; shielded, swallows repeated `task.cancel()`:
  1. Close bridge admission; late calls get "run stopping" and execute nothing.
  2. Send abort to the sidecar (A6).
  3. Await every in-flight call in the run's `BridgeRunScope` (scope-owned tasks). MCP calls are cancelled then awaited; local tools run to their own bound, since cancelling the shell call orphans its subprocess (local_tool_provider ~519).
  4. Poll until the sidecar reports idle (A6); only loopback connection-refused counts as stopped (A7).
  5. Revoke the token, then return or re-raise.
  - The worker lives until step 5, so `manage_run` keeps the heartbeat fresh and `worker_cancellation_slow` fires; nothing is released by elapsed time (§12).
- D13 (C-2) `engines/opencode/bridge.py`: internal MCP route mounted in `main.py` only when engine=opencode.
  - Exposes registry local tools plus the in-segment resolved MCP set (D16), minus `task_local`, `ask_user_local` and AG-UI tools.
  - Per-run 256-bit in-memory bearer bound to `(run_pk, LocalToolContext, resolved tools, budget, user MCP token)`; unknown or revoked tokens get 401.
  - Local calls go through `LocalToolProvider.execute` (~1083: tool policy, `resolve_within`, path policy); MCP calls rebind the context variables from the scope. The user MCP token and native provider credentials never reach the sidecar.
- D14 (C-4) `maxToolCalls` resolves as today (request → segment config → `MAX_TOOL_CALLS`), counted in the bridge; exceeding it denies the call, runs D12, returns `MAX_TOOL_CALLS`. `OPENCODE_REQUEST_TIMEOUT_SEC` is a per-request idle guard; total bounded by the D24 absolute `RUN_SEGMENT_DEADLINE_SEC`.
- D15 (C-1) Ownership fence: new `RunLifecycleService.is_run_live(session, run_pk)` reusing the `heartbeat_run` predicate (run_lifecycle ~408) narrowed to `active`; checked before each bridge call; failure denies the call, runs D12, returns `RUN_LIFECYCLE_FAILED`. Needed because the heartbeat loop (session_close_service ~365) ignores a `False` rowcount. Internal path unchanged (C-6, C-7).
- D16 (C-2, In-4) `engines/opencode/tool_map.py` `resolve_engine_tools(requested, user_token) -> ResolvedTools{mapped, rejected}` runs in-segment after workspace setup, bounded by the watchdog (async network I/O), one `fetch_mcp_tools(user_access_token=...)` call.
  - A missing or unmapped requested tool fails the attempt with `ENGINE_TOOL_UNSUPPORTED`, where the internal "Requested tools not found" fails (tool_hub ~1650).
  - When `tools` is omitted (always on AG-UI fresh, ~1773), unmapped tools are dropped and logged `opencode_tool_unmapped` (A12).
- D17 (Q5, C-3) Every in-segment failure runs D12 then returns `success: False`; controllers apply §12 unchanged; raw sidecar bodies are never returned. Codes:
  - `UNAVAILABLE`: health or version mismatch (checked each segment before session start), or sidecar down mid-run.
  - `TIMEOUT`: request idle timeout or the existing watchdog. `ENGINE_PROTOCOL_ERROR`: malformed event or partial stream.
  - `QUOTA`, `RATE_LIMIT`, `AUTH` or `UNAVAILABLE`: provider errors (A10). `RUN_LIFECYCLE_FAILED`: the D15 fence.
  - A hung abort returns no code; worker and claim stay held, per §12.
- D18 (C-1) Crash orphans: stale-claim recovery unchanged; token and scope die with the process. `engines/opencode/sweep.py` aborts and deletes this process's tagged sidecar sessions at startup (A1).
- D19 (In-3) OpenCode stream supplies text, usage, completion; the bridge supplies authoritative `TOOL_CALL_*` events, `tool_calls_info` and counters. `event_sink` publishes through `agui_event_service` and sets `text_streamed`; prior AG-UI turns seeded per A13.
- D20 (In-5) `config.py` adds `ORCHESTRATOR_ENGINE` (default `internal`), `OPENCODE_BASE_URL`, `OPENCODE_REQUEST_TIMEOUT_SEC=300`, `OPENCODE_MODEL`, `OPENCODE_PROVIDER=litellm|native`, infrastructure `OPENCODE_TOOL_BRIDGE_URL` (default `http://127.0.0.1:8000`). `validate_config` (~384) checks the engine value; only for opencode it requires a base URL with no userinfo or query, positive timeout, valid provider, non-empty `OPENCODE_MODEL` for native, and `HOOKS_ENABLED=false` (error names both keys; internal unaffected).
- D21 (In-5, C-4) `engines/opencode/model_route.py`: model is the caller-resolved model with today's precedence; `OPENCODE_MODEL`, empty by default, overrides only when set. `litellm` uses the sidecar LiteLLM provider (A9a); `native` an OpenCode provider-qualified id (A9b), credentials only in the sidecar environment.
- D22 (C-2) Session isolation: primary is per-session MCP config with per-session credential (A3). If A3 fails, a process `asyncio.Lock` allows one session per sidecar, acquired in-segment (watchdog bounds the wait); a sidecar-start secret routes bridge calls only to the lock holder; released after D12 step 5.
- D23 OpenCode does not depend on `ModelClient` (Out-1); discovery adapter (Out-2) not needed, discovery stays orchestrator-side.
- D24 (C-1, §12) `engines/deadline.py` `SegmentDeadline`: monotonic start, `remaining()`, `expired()`, length `RUN_SEGMENT_DEADLINE_SEC` via the existing settings parse (session_close_service ~87). Constructed only when the resolved fresh engine is opencode:
  - Start: AG-UI at the top of the fresh branch (~1560), orchestrator `execute` before the D11 gate (~707); legacy is rejected by its gate.
  - Checkpoints after binding prep (before `_create_agui_execution`) and before the claim; expiry returns `TIMEOUT` (AG-UI `RunErrorEvent`; orchestrator today's watchdog result shape, ~510, execution left `PENDING`) with no claim; an already-created AG-UI execution is marked `FAILED` as the `RunClaimError` branch (~1640) does; thread stays open (C-3).
  - Awaited DB steps are not wrapped in `asyncio.timeout` (cancelling a claim commit leaves its outcome unknown); their elapsed time is still deducted.
  - `manage_run` gains `deadline_sec: Optional[float] = None`; `None` keeps `segment_deadline_sec` (internal); else the watchdog sleeps `max(0, remaining)` read at entry, so lazy AG-UI stream start (~822) is also counted. `_stream_run` and `_managed_hub_process` thread an optional `segment_deadline` through.

### Unverified OpenCode assumptions (gated by the Slice 3 spike, a pinned version and recorded fixtures)
- A1 HTTP API offers health/version, session create (directory, system prompt, model/provider, tag), prompt, events, abort, status, list, delete; directory may be a sidecar-local empty path. A2 Built-in tools can be disabled.
- A3 Per-session remote MCP config takes a per-session header or URL credential, proven by a two-concurrent-session fixture.
- A4 Replaceable system prompt. A5 Text, usage, completion, error event shapes. A6 Abort plus observable idle. A7 No automatic resume after sidecar restart. A8 Subagents and compaction can be disabled.
- A9a LiteLLM OpenAI-compatible provider config and model id. A9b Native provider-qualified id. A10 Provider error classification. A11 Non-interactive permissions. A12 MCP tool name charset and `inputSchema` subset. A13 Session seeding with prior turns (fallback: delimited transcript in the first prompt).
- Outcomes: A2, A6 or A11 fails → Slices 5–6 stop. A3 fails → D22 fallback. A9b fails → `native` rejected at config validation. A4, A7, A8, A10, A12, A13 differ → document and narrow claims.

### Delivery slices (each from `origin/on-going-dev`, PR into `on-going-dev`, no attribution, C-8; `internal` remains the fallback after each)
1. Characterization (C-7). Entry: none. Exit: new tests green on unchanged code; no `src/` change.
2. Contract, internal extraction, identity (D1–D7). Entry: Slice 1 merged. Exit: suite green; new rows internal/1; mismatch tests pass; migration note written; head updated in `docs/DOCUMENTATION_INDEX.md` (~27), `docs/ORCHESTRATOR.md` §12 Migration and §15 code map.
3. Spike (A1–A13). Entry: Slice 2 merged. Exit: pinned version, `tests/fixtures/opencode/<version>/` for both providers, findings in `docs/PLUGGABLE_EXECUTION_ENGINES.md`; no runtime code.
4. Config, gating, deadline (D10, D11, D20, D21 validation, D24). Entry: no stopping failure in Slice 3. Exit: engine=opencode boots only with `HOOKS_ENABLED=false`; every run rejected as `ENGINE_CAPABILITY_UNSUPPORTED` (`headless_segment` off); D24 tests pass with a fake engine.
5. Bridge, fence, tool map (D13–D16, D22). Entry: Slice 4 merged. Exit: security, fence, scope, isolation, tool-map tests pass with a fake engine reporting `headless_segment`; route absent under internal.
6. Engine (D9, D12, D17–D19, D21). Entry: Slice 5 merged. Exit: fixture-replay and failure tests pass, `headless_segment` on, a headless execute completes.
- Follow-ups only (Out-3): Phase 5 parity per capability, then Phase 6 rollout. Out-1 and Out-2 ship independently.

### Test matrix (Q6; no DB, no network)
- In-1: `test_engine_contract.py`: result keys equal hub keys, envelope round-trip, unknown version rejected.
- In-2: `test_engine_characterization.py` (Slice 1): orchestrator complete, await, fail, `MAX_TOOL_CALLS`, `TIMEOUT`; AG-UI `text_streamed`; legacy execute and resume; snapshot keys; in-segment "Requested tools not found".
  - Same file: AG-UI fresh with `input` and with `workspacePath` calls `workspace_manager.provision` / `_ensure_workspace_for_agui_segment` only after `manage_run` entry, and a provision outlasting a small `RUN_SEGMENT_DEADLINE_SEC` ends `TIMEOUT` with the run row terminalized.
  - `test_engine_resolver.py`: fresh follows env, resume follows persisted; on all three paths `ENGINE_MISMATCH` makes no claim and keeps `AWAITING_RESPONSE`; internal state resumes under opencode.
  - `test_engine_identity_persistence.py` (columns, defaults, revision chain); `test_engine_architecture.py` (engine imports stay behind `engines/`).
  - Unchanged and green: `test_claim_lifecycle`, `test_stale_claim_recovery`, `test_bounded_run_lifecycle`, `test_agui_cancellation`, `test_resume_idempotency`, `test_orchestrator_claim_lifecycle`, `test_agui_await_resume`, `test_llm_error_contract`, `test_run_lifecycle_phase3`.
- In-3: `test_opencode_engine.py`: fixture replay over httpx `MockTransport`, both providers: completion, AG-UI text, tool events, usage, A13 seeding. `test_opencode_model_route.py`: precedence and override.
  - `test_opencode_tool_bridge.py`: cross-run and revoked tokens 401; workflow write blocked, traversal and symlinks refused, budget ends `MAX_TOOL_CALLS`; user token never reaches the sidecar, excluded tools absent; two concurrent runs see only their own context and MCP token; D22 lock-holder mode; stale-reclaimed run denied, nothing executes (`RUN_LIFECYCLE_FAILED`).
  - `test_opencode_failures.py`: each D17 mode; cancel during each tool class (MCP cancelled then awaited; shell completes, no orphan; file writes awaited); claim finalized only after handlers return and sidecar idle; hung abort keeps heartbeat fresh; late calls refused; post-failure states match §12.
  - `test_opencode_startup_sweep.py`.
- In-4: `test_engine_capabilities.py`:
  - Every pre-claim gate leaves no `Execution` or `ExecutionRun` row (`create_execution`, `_create_execution_record`, `try_create_active_run` never called; orchestrator status unchanged).
  - `require_capabilities` is not a coroutine function and passes with DB, filesystem and network fixtures that raise on use.
  - Null-context and unmapped-tool checks fail as attempts with no sidecar contact; thread stays open, orchestrator execution returns to `PENDING`.
  - Tool resolution outlasting a small `RUN_SEGMENT_DEADLINE_SEC` ends `TIMEOUT` through the existing watchdog.
- In-4/C-1: `test_engine_deadline.py` (fake opencode engine): slow patched `ensure_runtime` / `_create_agui_execution` make the watchdog fire at start + deadline, not entry + deadline; expiry before claim returns `TIMEOUT`, `try_create_active_run` never called, AG-UI execution `FAILED`, thread open, orchestrator execution `PENDING`; delayed AG-UI stream consumption is deducted.
  - Under internal, `SegmentDeadline` is never constructed and `manage_run` is called without `deadline_sec` (spy); `test_session_close_phase3.py` gains `deadline_sec=None` keeps settings and `deadline_sec=0.05` fires `segment_deadline`.
- In-5: `test_engine_config.py` (D20: native without model; opencode with `HOOKS_ENABLED=true` fails; internal with `HOOKS_ENABLED=true` passes). Extend `test_public_surface_config.py` (default `internal`; keys in both `.env.example`) and `test_log_url_redaction.py` (`OPENCODE_BASE_URL`).
- In-6: docs and `changes/` entries reviewed in each slice PR.

### Files
- New: `src/app/services/engines/{__init__,contract,base,capabilities,resolver,internal,errors,deadline}.py`; `engines/opencode/{client,engine,translate,bridge,scope,tool_map,model_route,sweep}.py`; `alembic/versions/20261124_engine_identity.py`; tests above; `tests/fixtures/opencode/`.
- Edit: `orchestrator_controller.py`, `ag_ui_controller.py`, `agent_controller.py` (resolver, gates, mismatch); `run_lifecycle.py` (engine kwargs, `is_run_live`); `execution_state_service.py` (`create_state`); `models/execution_models.py`; `main.py` (bridge mount, sweep in `lifespan`); `config.py`; `src/.env/{localhost,docker}/.env.example` (opencode keys commented, noting `HOOKS_ENABLED=false` is required with opencode); `session_close_service.py` (`manage_run` optional `deadline_sec`, watchdog type `float`; existing callers and test fakes untouched since internal never passes it). `hooks/` unchanged.
- Docs:
  - `readme.md`: Configuration "Execution engine" (opencode requires `HOOKS_ENABLED=false`); Error codes `ENGINE_MISMATCH`, `ENGINE_CAPABILITY_UNSUPPORTED`, `ENGINE_PROTOCOL_ERROR`, `ENGINE_TOOL_UNSUPPORTED`; Tool selection; Running many instances (one process per sidecar).
  - `docs/ORCHESTRATOR.md` §5, §10, §11 (Hooks: unsupported under opencode), §12 (stop sequence, fence, gates, opencode deadline start at admission, migration head ~1245), §13, §15 code map (~1361); `docs/DOCUMENTATION_INDEX.md` (~27, migration head); status in `docs/PLUGGABLE_EXECUTION_ENGINES.md`.
  - One `changes/YYYY-MM-DD-<slug>.md` per slice; Slice 2 includes the migration note.

### Risks
- AG-UI closures are large; Slice 2 swaps only call expressions, guarded by Slice 1 tests.
- Internal pre-claim AG-UI prep still runs before its watchdog, contrary to §12 (~1131); C-6 forbids changing it here, so Slice 4 records it as a §12 Known issue for a separate internal change.
- A single blocking pre-claim step (sync mkdir, DB commit) cannot be interrupted under opencode; D24 deducts its time and refuses the claim on expiry, so total wall time exceeds the deadline only by that one step.
- Bridge tokens live in process memory: a bridge request routed to another worker gets 401, failing closed. Multi-worker processes and shared sidecars are unsupported under opencode (hard constraint).
- Existing in-segment `collect_manifest` scan keeps the §12 synchronous-discovery Known issue on both engines; opencode adds no new synchronous discovery and the fix stays a separate change.
- Refusing `HOOKS_ENABLED=true` blocks opencode on hosts that enable hooks for internal runs; this is fail-closed and documented, and Phase 5 hook parity lifts it (Out-3).
- Fence adds one DB read per bridge call. A long local tool delays stop until its own bound; `worker_cancellation_slow` surfaces it.
- An unmapped requested tool costs an attempt row, matching internal in-segment tool rejection. D22 fallback serializes runs per process (capacity cost). Gateways must not expose `/internal/*`.
- Version drift: pinned version checked at segment start; fixtures re-recorded on upgrade.

### Validation
- Per slice: `python -m pytest` passes with no DB or network; new tests fail before the change where applicable.
- Slice 1: the AG-UI provisioning-under-watchdog characterization passes on unchanged code, pinning today's in-segment provisioning before any seam lands.
- Slice 2: `alembic upgrade head` and `downgrade -1` on a disposable Postgres; `scripts/smoke_service.py` and `scripts/smoke_agui.py` under internal; search for `20250816_phase3_lifecycle` finds it only as historical or `down_revision`, never as the stated head.
- Slice 4: boot fails with opencode plus `HOOKS_ENABLED=true` and succeeds with internal plus `HOOKS_ENABLED=true`; `test_engine_deadline.py` and the `manage_run` fakes in the nine suites using it stay green.
- Slice 6: manual headless execute against the pinned sidecar, both provider modes.
- After every slice, the characterization suite confirms `ORCHESTRATOR_ENGINE=internal` behaves exactly as before.
