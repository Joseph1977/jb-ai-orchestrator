# Plan — OpenCodeExecutionEngine implementation plan
Version: 2
Model: claude-opus-5-5-medium

## Response to review
| Point | Decision | Response |
|---|---|---|
| P001 | accepted | D10 adds pre-claim `subagents` (`SUBAGENT_ENABLED`, default true) and `context_compaction` (`CONTEXT_COMPACTION_ENABLED`, default true) gates; D12 lists `task` only when flag-off; flagless `ask_user` dropped, logged, documented; D18 logs flags; In-4 tests. |

## Plan

### What / where / why
Insert an `ExecutionEngine` seam at the six controller calls to `ToolExecutionHub.process_request`; lifecycle (claim, heartbeat, deadline, close, persistence) stays in controllers. Engine identity is persisted per run and per await state. A headless `OpenCodeExecutionEngine` routes every tool call back through an orchestrator-hosted, per-run, ownership-fenced tool bridge. Covers In-1..In-6; Out-1..Out-4 untouched. Baseline is the earlier run's plan v4, re-verified line by line against current code; corrections are marked "(re-verified)".

### Segment today (Q1)
- Six call sites: `orchestrator_controller.py` execute (770) and resume (992); `ag_ui_controller.py` `_run_resume_segment` (1391) and `_run_fresh_segment` (1768); `agent_controller.py` `executeRequest` (175) and `resumeRun` (274). `tool_hub.py` 1564 is the hub's own nested `task_local` call and stays internal.
- Orchestrator and AG-UI segments are one hub task inside `session_close_service.manage_run` (153), reached via `_managed_hub_process` (461) or `_stream_run` (807). `manage_run` owns heartbeat, the deadline watchdog (full `segment_deadline_sec`, 186), cancel event + `task.cancel()`, and awaiting the worker before release; the claim is `run_lifecycle.try_create_active_run` (334).
- Legacy agent paths have no `ExecutionRun` claim, no `manage_run`, and `resumeRun` does no state claim (re-verified). They stay unchanged under internal.
- Persisted segment fields: `execution_runs` (`execution_id, thread_key, run_id, origin, channel, status, heartbeat_at, started_at, finished_at`); await snapshot `llm_states` (`execution_id, thread_id, thread_key, run_id, tool_call_id, status, state_payload`), written by `create_state` (`execution_state_service.py` 106) from three controllers (orchestrator 328, AG-UI 599, agent 132).
- Result is the hub dict (`success, response, error, error_code`, counters, tokens, `tool_calls_info, agui_tool_calls, text_streamed, awaits_response, pending_tools, pending_tool, interrupts, state`); `state` comes from `_build_await_result` (857). Resume uses it with `resume_tool_results`, `resolve_resume_model`, `resolve_resume_max_tool_calls`; `prepare_hook_permission_resume_events` stays internal-only.

### Design decisions
- D1 (In-1) `engines/contract.py`: v1 `SegmentRequest` (all current `process_request` kwargs incl. `cancel_check`, + `event_sink`, + optional `resolved_tools`), `EngineEvent` (text/tool/usage), `SegmentResult` (TypedDict of hub keys), `AwaitEnvelope{engine, contractVersion, payload}`, `ENGINE_CONTRACT_VERSION = 1`.
- D2 (In-1) `engines/base.py` protocol: `name`, `contract_version`, `capabilities()`, `async run_segment`. Failures return `success: False`; `CancelledError` re-raised only after engine-owned work stopped (D14).
- D3 (In-2) `engines/internal.py` forwards kwargs unchanged with `event_sink=None`; rejects non-`None` `resolved_tools`. `task_local` stays inside the hub.
- D4 (In-2) `engines/resolver.py`: `resolve_fresh_engine()` reads `ORCHESTRATOR_ENGINE`; `resolve_persisted_engine(name, version)` never reads the environment. Used at all six call sites.
- D5 (Q2, C-5) Typed columns, not payload: `engine VARCHAR(32) NOT NULL DEFAULT 'internal'`, `engine_contract_version INTEGER NOT NULL DEFAULT 1` on `execution_runs` and `llm_states`. `state_payload` unchanged (C-6). `try_create_active_run` and `create_state` gain keyword params defaulting to internal/1.
- D6 (Q2) Alembic revision `20261124_engine_identity`, down-revision `20250816_phase3_lifecycle` (re-verified head). Server default backfills existing rows as internal/1, using the phase-3 adopt-if-present pattern; downgrade drops the columns; `executions.config` not rewritten.
- D7 (Q2, C-5) Mismatch check after state load and before any claim: orchestrator resume after `get_state` (890) before `try_claim_state_for_resume` (935); AG-UI `_handle_tool_response` after `get_resume_hold_by_tool_call_id` (1180) before claim (1257); legacy `resumeRun` after state load (251) before the hub call.
  - REST: 409 `{"success":false,"errorCode":"ENGINE_MISMATCH","error":"Run state was created by engine '<e>' contract v<n>; this deployment cannot resume it"}` (same shape as `_run_conflict_payload`). AG-UI: `RunErrorEvent("ENGINE_MISMATCH: …")`.
  - State stays `AWAITING_RESPONSE`, no `ExecutionRun`; legacy path returns before its `DISCARDED` branch (286). Internal state always resumes on internal under either setting.
- D8 (Q3) OpenCode v1 has no resume mode: never awaits, writes no `llm_states`, `durable_resume=false`. First mode later (Out-3) is the translated snapshot: a session reference pins resume to one process's sidecar (D11) and breaks replica independence (`docs/ORCHESTRATOR.md` §10). Rejection tested with a crafted `engine='opencode'` row.
- D9 (Q4, In-4) `engines/capabilities.py`: `headless_segment, workspace_file_ops, mcp_tools, streaming_output, agui_frontend_interrupts, durable_resume, hooks, permission_asks, subagents, context_compaction, workspace_free`. Internal: all true. OpenCode: `headless_segment`, `workspace_file_ops`, `streaming_output` true; `mcp_tools=subset` (D22); rest false.
- D10 (Q4, In-4) `require_capabilities(engine, needs)` → 400 / `RunErrorEvent` `ENGINE_CAPABILITY_UNSUPPORTED` naming the capability; denial logged with engine, version, capability. Internal is never gated. Gate points (re-verified line numbers):
  - Orchestrator `execute`: after `_segment_config` (707), before the claim session (710); `Execution` status untouched.
  - AG-UI fresh: frontend-tool gate before binding resolution (1569); binding-dependent gates right after `except BindingError` (1597), before `sanitize_execution_config` (1601) and `_create_agui_execution` (1621, which inserts a RUNNING `Execution`, 569).
  - Legacy `executeRequest`: before `_create_execution_record()` (158), so rejection leaves no `Execution` row (new; baseline omitted this placement).
  - Earlier `ensure_thread_session` (1449), `_prepare_thread_claims` (1474) and the input-branch `ensure_runtime` (1582) run before gates as they do for binding errors; they create no `Execution`/`ExecutionRun` and the thread stays runnable.
  - Gates: non-empty `frontendTools`; `subagents` when `Config.SUBAGENT_ENABLED` (`config.py` 278, default true; exposes `task`, `local_tool_provider.py` 1061); `context_compaction` when `Config.CONTEXT_COMPACTION_ENABLED` (272, default true; drives offload and compaction, `context_compaction.py` 161/185/424); `workspace_free` when AG-UI `input_binding is None` and always on legacy `executeRequest`; `HOOKS_ENABLED` with non-empty `load_hooks_for_workspace` (`hooks/loader.py` 256, covers `preCompact`) when the path is known; then D22 tool resolution, last.
  - Both flags are process-global with no per-request field (controllers never read them), so an OpenCode deployment must set them `false` to run; the error names the capability and the flag. No new setting or default (C-4); internal unaffected (C-6).
  - `ask_user` is always listed headless with no flag and requires a durable await (D8): OpenCode omits it, logged `opencode_tool_unmapped` and documented in `readme.md` Tool selection; gating it would reject every run with no operator remedy.
  - In-segment re-checks fail as an attempt (§12): `local_context is None` (e.g. `workspacePath` ignored when `ALLOW_INPLACE_WORKSPACE=false`, 387), hooks found after `ensure_input_workspace`.
- D11 (C-2) One loopback sidecar per orchestrator process, no workspace mount; each session rooted in an empty per-run scratch dir; orchestrator-composed system prompt sent as session system prompt (A4).
- D12 (C-2) `engines/opencode/bridge.py`: internal MCP route mounted in `main.py` (99–102) only when engine=opencode.
  - Exposes `list_litellm_tools(exclude_base={"task","ask_user"}, mode, output_bound)` + `resolved_tools.mapped`, no AG-UI tools; `task` is reachable only with `SUBAGENT_ENABLED=false` (D10), so the exclusion is belt-and-braces, never a silent drop.
  - Per-run 256-bit in-memory bearer bound to `(run_pk, LocalToolContext, registry, budget, user MCP token)`; bad token → 401.
  - Local calls via `LocalToolProvider.execute` (1083): tool policy, `resolve_within`, path policy, workspace opener. The user MCP token never reaches the sidecar; OpenCode built-ins disabled (A2).
- D13 (C-4) `maxToolCalls`: request → segment config → `MAX_TOOL_CALLS`, counted in the bridge. `OPENCODE_REQUEST_TIMEOUT_SEC` is an infrastructure guard per sidecar request; the segment deadline (D24) still caps the whole run. `OPENCODE_MODEL`/`OPENCODE_PROVIDER` inert unless engine=opencode.
- D14 (C-1) Shielded stop sequence on cancel, `CancelledError`, completion or failure; swallows repeated `task.cancel()`:
  1. Close bridge admission; late calls get "run stopping" and execute nothing.
  2. Send abort to the sidecar.
  3. Await every in-flight call in the run's `BridgeRunScope` (tasks owned by the scope, not the HTTP request). MCP calls cancelled then awaited; local tools run to their own bound, because cancelling `execute_local` at `proc.communicate()` orphans the subprocess (`local_tool_provider.py` 519–530).
  4. Poll the sidecar until idle (A6).  5. Revoke the token, then return or re-raise.
  - The worker lives until step 5, so heartbeat stays fresh and `worker_cancellation_slow` fires; nothing is released on elapsed time. Only loopback connection-refused counts as "sidecar stopped" (A7).
- D15 (C-1) Crash orphans: stale-claim recovery (`mark_stale_runs_failed` in `try_create_active_run`, 368) unchanged; token and scope die with the process; startup `sweep.py` aborts and deletes tagged sidecar sessions (A1).
- D16 (Q5, C-3) Every in-segment failure runs D14 then returns `success: False`; controllers apply the §12 table unchanged; raw sidecar bodies never returned. Codes: `UNAVAILABLE` (down, health/version fail); `TIMEOUT` (request timeout, segment deadline); `ENGINE_PROTOCOL_ERROR` (malformed event, partial stream); `QUOTA`/`RATE_LIMIT`/`AUTH`/`UNAVAILABLE` for provider errors (A10); `RUN_LIFECYCLE_FAILED` on lost ownership (D20). `ENGINE_TOOL_UNSUPPORTED` only pre-claim (D22).
- D17 (In-3) Sidecar stream supplies text, usage, completion; the bridge supplies authoritative `TOOL_CALL_*` events, `tool_calls_info` and counters. `event_sink` publishes via `agui_event_service` and sets `text_streamed`.
- D18 (In-5) `config.py`: `ORCHESTRATOR_ENGINE` (default `internal`), `OPENCODE_BASE_URL`, `OPENCODE_REQUEST_TIMEOUT_SEC=300`, `OPENCODE_MODEL`, `OPENCODE_PROVIDER=litellm|native`, infra `OPENCODE_TOOL_BRIDGE_URL` (default `http://127.0.0.1:8000`). `validate_config` (384) checks the engine value; only when opencode: base URL without userinfo/query, positive timeout, valid provider, non-empty `OPENCODE_MODEL` for native. When opencode and `SUBAGENT_ENABLED` or `CONTEXT_COMPACTION_ENABLED` is true, log `opencode_capability_flag_enabled` naming the flag (boot continues; D10 rejects runs).
- D19 OpenCode does not depend on `ModelClient` (Out-1); no discovery adapter needed (Out-2) because discovery stays orchestrator-side.
- D20 (C-1) New `RunLifecycleService.is_run_live(session, run_pk)`: the `heartbeat_run` predicate (408) narrowed from `_ACTIVE_RUN_STATUSES` (32) to `status == active`. Bridge checks before each call; not live → deny, mark scope `ownership_lost`, run D14, return `RUN_LIFECYCLE_FAILED`. Residual: a call already running at reclaim completes, as on internal (§12).
- D21 (C-2) Primary: per-session MCP config with per-session credential (A3). Fallback if A3 fails: process `asyncio.Lock`, one session per sidecar, secret set at sidecar start routes calls only to the lock holder; released after D14 step 5; wait time counts against D24 (`TIMEOUT`).
- D22 (C-2, In-4) `engines/opencode/tool_map.py` `resolve_engine_tools(requested, user_token) -> ResolvedTools{mapped, rejected{name: reason}}`, last D10 gate on OpenCode fresh paths.
  - Listing via `mcp_service.fetch_mcp_tools(user_access_token=…)` (388), per-server `MCP_FETCH_TIMEOUT_SEC` unchanged, whole call under `asyncio.timeout_at(D24 deadline)`; expiry → 504 / `RunErrorEvent` `TIMEOUT` before any claim.
  - Mapping accepts names in OpenCode's charset/length and object-root `inputSchema` with allowlisted keywords (A12).
  - Requested missing/unmapped → 400 / `ENGINE_TOOL_UNSUPPORTED` naming tool and reason. `tools` omitted (always on AG-UI fresh, `requested_tools=None`, 1773) → unmapped dropped and logged `opencode_tool_unmapped`.
  - `ResolvedTools` travels in `SegmentRequest.resolved_tools`; engine and bridge never re-fetch. Internal keeps in-segment `_select_tools` (1620) unchanged (C-6).
- D23 (In-5) `engines/opencode/model_route.py` `resolve_model_route(provider, opencode_model, caller_model)`: `litellm` uses the sidecar's LiteLLM provider with `OPENCODE_MODEL` else caller model (A9a); `native` uses `OPENCODE_MODEL` provider-qualified (A9b). Native credentials only in the sidecar env, never in orchestrator env (which `execute_local` copies, 515).
- D24 (C-1, C-4) `engines/deadline.py` `SegmentDeadline(at=loop.time() + RUN_SEGMENT_DEADLINE_SEC)`, `remaining()`.
  - Created on OpenCode fresh paths before the first gate: orchestrator `execute` entry (697); AG-UI fresh before 1569. Matches §12 (deadline covers preparation).
  - `manage_run(..., deadline_sec: float | None = None)`: when set, the watchdog sleeps that instead of `segment_deadline_sec` (186). `_managed_hub_process` and `_stream_run` pass it through; their 8 existing test callers omit it and are unaffected.
  - Controller passes `deadline.remaining()` computed right before `manage_run`. `<= 0` before claim → `TIMEOUT`, no rows; after claim → `TIMEOUT` attempt (§12).
  - Internal paths never pass `deadline_sec` (C-6); no new setting (C-4).

### Unverified OpenCode assumptions (gated by Slice 3 spike, pinned version, fixtures)
- A1 HTTP API: health/version, session create (directory, system prompt, model/provider, tag), prompt, events, abort, status, list, delete. A2 Built-in tools can be disabled.
- A3 Per-session remote MCP config with per-session header/URL credential, proven by a two-concurrent-session fixture where each session's calls carry only its own credential.
- A4 Replaceable system prompt. A5 Text, usage, completion, error event shapes. A6 Abort plus observable idle state. A7 No automatic resume after restart. A8 Subagents and compaction can be disabled.
- A9a LiteLLM OpenAI-compatible provider config and model id. A9b Native provider-qualified id. A10 Provider error classification. A11 Non-interactive permissions. A12 MCP names and `inputSchema` keyword subset accepted end to end.
- Outcomes: A2, A6 or A11 failure stops Slices 5–6. A3 failure → D21 fallback. A9b failure → `native` rejected at config validation, documented. A4, A7, A8, A10, A12 failures documented and capability claims narrowed.

### Delivery slices (each branched from `origin/on-going-dev`, PR into `on-going-dev`, no attribution, C-8; `internal` safe fallback after each)
1. Characterization (C-7). Entry: none. Exit: new tests green on unchanged code; no `src/` change.
2. Contract, internal extraction, identity (D1–D7). Entry: Slice 1 merged. Exit: suite green; new rows internal/1; mismatch tests pass; migration note written.
3. Spike (A1–A12). Entry: Slice 2 merged. Exit: pinned version; `tests/fixtures/opencode/<version>/` for litellm and native; findings in `docs/PLUGGABLE_EXECUTION_ENGINES.md`; no runtime code.
4. Config and gating (D9, D10, D18, D23 validation). Entry: no stopping failure in Slice 3. Exit: engine=opencode boots; every run rejected `ENGINE_CAPABILITY_UNSUPPORTED` (`headless_segment` still off).
5. Bridge, pre-claim tool gate, deadline (D12, D13, D20–D22, D24). Entry: Slice 4 merged. Exit: security, fence, scope, isolation, tool-gate, cumulative-deadline tests pass with a fake engine reporting `headless_segment`; bridge route absent under internal.
6. Engine (D11, D14–D17, D23). Entry: Slice 5 merged. Exit: fixture-replay and failure tests pass; `headless_segment` on; a headless execute completes with `SUBAGENT_ENABLED=false`, `CONTEXT_COMPACTION_ENABLED=false`.
- Follow-ups only (Out-3): Phase 5 parity per capability, Phase 6 rollout. Out-1/Out-2 ship independently.

### Test matrix (Q6; no DB, no network)
- In-1 `test_engine_contract.py`: result keys equal hub keys; envelope round-trip; unknown version rejected; internal rejects non-`None` `resolved_tools`.
- In-2:
  - `test_engine_characterization.py` (Slice 1): orchestrator complete, await, fail, `MAX_TOOL_CALLS`, `TIMEOUT`; AG-UI `text_streamed`; legacy execute and resume; snapshot keys; in-segment "Requested tools not found" under internal.
  - `test_engine_resolver.py`: fresh follows env; resume follows persisted value; `ENGINE_MISMATCH` with no claim on all three resume paths, state still `AWAITING_RESPONSE`; internal state resumes under opencode.
  - `test_engine_identity_persistence.py` (columns, defaults, revision chain); `test_engine_architecture.py` (engine imports stay behind adapters).
  - Unchanged and green: `test_claim_lifecycle`, `test_stale_claim_recovery`, `test_bounded_run_lifecycle`, `test_agui_cancellation`, `test_resume_idempotency`, `test_orchestrator_claim_lifecycle`, `test_agui_await_resume`, `test_llm_error_contract`, `test_workspace_containment`.
- In-3:
  - `test_opencode_engine.py`: fixture replay over httpx `MockTransport`, both provider modes (completion, AG-UI text, tool events, usage); engine never calls `fetch_mcp_tools`.
  - `test_opencode_model_route.py`: litellm precedence; native qualified id; native credentials absent from orchestrator requests.
  - `test_opencode_tool_bridge.py`: cross-run and revoked tokens → 401; policy (workflow write blocked, traversal and symlinks refused, budget); leakage (user MCP token never sent to sidecar, `task`/`ask_user`/AG-UI tools absent, only `mapped` exposed); isolation (two concurrent runs see only their own context; D21 lock mode); fence (fake stale-reclaim → next call denied, nothing executes, `RUN_LIFECYCLE_FAILED`).
  - `test_opencode_failures.py`: sidecar down, timeout, malformed event, partial stream, deadline, close; cancel during each tool class (blocking MCP cancelled then awaited; `execute_local` completes with no orphan; file write and shared-folder output awaited). Assert claim finalized only after handlers return and idle confirmed; hung handler keeps worker alive and heartbeat fresh; late calls refused; post-failure states match §12.
  - `test_opencode_startup_sweep.py`.
- In-4 `test_engine_capabilities.py`:
  - Every D10 gate (orchestrator, each AG-UI fresh rejection, legacy `executeRequest`) leaves no `Execution` and no `ExecutionRun` row (`create_execution`, `_create_execution_record`, `try_create_active_run` never called); orchestrator `Execution` status unchanged; internal never gated.
  - `subagents` and `context_compaction`: with each flag true (others satisfied) every OpenCode entry (orchestrator, AG-UI fresh, legacy) returns `ENGINE_CAPABILITY_UNSUPPORTED` naming capability and flag, no rows; both false → pass; internal with both true never gated; bridge listing never contains `task`/`ask_user`.
  - AG-UI `input` (shared_folder) and in-place `workspacePath` pass gates; ignored `workspacePath` fails in-segment with the thread open.
  - D22: requested missing/unmapped → `ENGINE_TOOL_UNSUPPORTED`, no rows, no sidecar contact; unrequested unmapped dropped and logged; engine receives exactly the resolved set; `fetch_mcp_tools` once per request with the request token.
  - D24 (small `RUN_SEGMENT_DEADLINE_SEC`, fake clock): resolution outlasting the deadline → pre-claim `TIMEOUT`, no rows; resolution at 60% then a blocking fake engine → `TIMEOUT` at 100% total, not 160%.
  - Extend `test_bounded_run_lifecycle.py`: `manage_run(deadline_sec=x)` fires at x; omitted keeps `segment_deadline_sec`.
- In-5 `test_engine_config.py` (D18, incl. native without `OPENCODE_MODEL`; internal valid with no OpenCode vars; flag-enabled startup log under opencode only); extend `test_public_surface_config.py` (default `internal`, keys in both `.env.example`) and `test_log_url_redaction.py` (`OPENCODE_BASE_URL`).
- In-6: docs and `changes/` entries reviewed in each slice PR.

### Files
- New: `src/app/services/engines/{__init__,contract,base,capabilities,resolver,internal,errors,deadline}.py`; `engines/opencode/{client,engine,translate,bridge,scope,tool_map,model_route,sweep}.py`; `alembic/versions/20261124_engine_identity.py`; tests above; `tests/fixtures/opencode/`.
- Edit:
  - Controllers: `orchestrator_controller.py` (gates, D7, D24, `_managed_hub_process`); `ag_ui_controller.py` (gates before 1621, D7 in `_handle_tool_response`, D24, `_stream_run`); `agent_controller.py` (resolver, gate before 158, D7).
  - Services/persistence: `session_close_service.py` (`manage_run` `deadline_sec`); `models/execution_models.py`; `execution_state_service.py` (`create_state`); `run_lifecycle.py` (`try_create_active_run` engine params, `is_run_live`).
  - Wiring: `src/app/main.py` (bridge mount, sweep in `lifespan`); `config.py`; `src/.env/{localhost,docker}/.env.example`.
- Docs:
  - `readme.md`: Configuration "Execution engine" (opencode requires `SUBAGENT_ENABLED=false`, `CONTEXT_COMPACTION_ENABLED=false`; `ask_user` unavailable); Lifecycle note that under opencode `RUN_SEGMENT_DEADLINE_SEC` also covers pre-claim tool resolution; Error codes `ENGINE_MISMATCH`, `ENGINE_CAPABILITY_UNSUPPORTED`, `ENGINE_PROTOCOL_ERROR`, `ENGINE_TOOL_UNSUPPORTED`; Tool selection; Running many instances (one sidecar per process, D21 capacity).
  - `docs/ORCHESTRATOR.md` §5, §10, §11, §12 (stop sequence, fence, pre-claim gates, single deadline, migration head `20261124_engine_identity`), §13, §15; status in `docs/PLUGGABLE_EXECUTION_ENGINES.md`.
  - `changes/README.md`: add an "Engine Phase" row to the series table (required by its "Naming phases" rule); one `changes/YYYY-MM-DD-<slug>.md` per slice.

### Risks
- Large AG-UI closures: Slice 2 swaps only the call expression; Slice 1 tests guard behaviour; moved gates touch only OpenCode branches.
- A sidecar shared by several processes breaks D14, D15, D21: documented hard deployment constraint.
- D20 adds one DB read per tool call, accepted for ownership safety.
- A long local tool delays stop to its own bound; `worker_cancellation_slow` makes it visible.
- D22 spends D24 budget pre-claim, so a later `RUN_CONFLICT` has paid for listing (OpenCode only); MCP drift between gate and run surfaces as the bridge's call-time error.
- D21 fallback serializes OpenCode runs per process (capacity, not correctness). The bridge shares the app port: gateways must not expose `/internal/*`; the per-run bearer is the control.
- The input-branch `ensure_runtime` (1582) creates the thread runtime dir before a rejection; idempotent, preserved across runs, no lifecycle row.
- Version drift: pinned version checked on health; fixtures re-recorded on upgrade.

### Validation
- Per slice: `python -m pytest` with no DB or network; new tests fail before the change where applicable.
- Slice 2: `alembic upgrade head` and `downgrade -1` on disposable Postgres; `scripts/smoke_service.py` and `scripts/smoke_agui.py` under internal.
- Slice 5: cumulative-deadline tests and unchanged `test_bounded_run_lifecycle` pass.
- Slice 6: manual headless execute against the pinned sidecar, both provider modes.
- After every slice the characterization suite confirms `ORCHESTRATOR_ENGINE=internal` behaves exactly as before.
