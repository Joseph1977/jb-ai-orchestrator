# Plan — OpenCodeExecutionEngine implementation plan
Version: 1
Model: claude-opus-5-5-medium

## Plan

### What / where / why
Insert an `ExecutionEngine` seam exactly where controllers call `ToolExecutionHub.process_request` today, keep lifecycle (claims, heartbeat, deadline, close, persistence) in the controllers, persist engine identity per run and per await state, then add a headless-only `OpenCodeExecutionEngine` whose every tool call goes back through an orchestrator-hosted, per-run tool bridge. Serves In-1..In-6; Out-1..Out-4 untouched.

### Segment today (Q1) — the contract matches this
- One segment = one `process_request(...)` call run as the task inside `_managed_hub_process` (`orchestrator_controller.py`) / the AG-UI run closures (`ag_ui_controller.py` ~1391 resume, ~1768 fresh), wrapped by `session_close_service.manage_run` (heartbeat, `RUN_SEGMENT_DEADLINE_SEC` watchdog, `cancel_event` + `task.cancel()`, await-worker-before-release) with one `ExecutionRun` row from `run_lifecycle.try_create_active_run`.
- Legacy `agent_controller.py` (175 execute, 274 `resumeRun`) calls the hub with no `ExecutionRun` claim; that stays as is.
- Result = the hub dict (`success, response, error, error_code, tool_calls_made/segment counters, total/prompt/completion_tokens, tool_calls_info, agui_tool_calls, text_streamed`, plus `awaits_response, pending_tools, pending_tool, interrupts, state`).
- Await snapshot = `_build_await_result()["state"]` (`messages, request, model, max_calls, requested_tools, lite_llm_request_timeout_in_sec`, counters, token totals, `litellm_tools, agui_records, agui_tool_calls, tool_calls_info, pending_tools, pending_tool, harness_manifest`), stored as `llm_states.state_payload` (`_persist_awaiting` and AG-UI equivalents); resume inputs are payload + `resume_tool_results` + `resolve_resume_model` / `resolve_resume_max_tool_calls`.
- AG-UI hook-permission resume also calls `hub.prepare_hook_permission_resume_events`; it remains internal-engine-only.

### Design decisions
- D1 (In-1) `src/app/services/engines/contract.py`: v1 `SegmentRequest` (exactly the current `process_request` kwargs plus `event_sink`), `EngineEvent` (text start/delta/end, tool start/args/end/result, usage), `SegmentResult` (a TypedDict of the hub keys above), `AwaitEnvelope {engine, contractVersion, payload}`. `ENGINE_CONTRACT_VERSION = 1`.
- D2 (In-1) `engines/base.py`: `ExecutionEngine` protocol with `name`, `contract_version`, `capabilities()`, `async run_segment(SegmentRequest) -> SegmentResult`. Failures come back as `success: False` dicts, never raised, so the existing controller branches apply unchanged. Cancellation propagates as `CancelledError` only after the worker has stopped.
- D3 (In-2) `engines/internal.py`: `InternalExecutionEngine.run_segment` forwards kwargs verbatim to the existing hub and returns its dict untouched. The internal engine passes `event_sink=None`, so the hub keeps publishing AG-UI events itself. `task_local` keeps calling `self.process_request` inside the hub.
- D4 (In-2) `engines/resolver.py`: `resolve_fresh_engine()` reads `Config.ORCHESTRATOR_ENGINE`. `resolve_persisted_engine(name, version)` never reads the env. `get_tool_hub()` stays for internal wiring. The resolver is applied at the 6 call sites listed above.
- D5 (Q2, In-2, C-5) Engine identity lives in typed columns, not in the payload. `engine VARCHAR(32) NOT NULL server_default 'internal'` and `engine_contract_version INTEGER NOT NULL server_default 1` go on `execution_runs` (attempt record) and `llm_states` (resume authority). `state_payload` bytes stay unchanged (C-6).
- D6 (Q2) The alembic revision `20261124_engine_identity` (down `20250816_phase3_lifecycle`) adds the columns. The server default backfills every existing row as internal/1, which is metadata-only in Postgres 11+. It follows the phase-3 adopt-if-present pattern for `create_all` databases. The ORM models declare the same server defaults so `init_db()` matches. Downgrade drops the columns. No rewrite of `executions.config`.
- D7 (Q2, C-5) Resume-mismatch error, checked after loading the state and **before** any claim (orchestrator resume ~904, AG-UI hold lookup, legacy `resumeRun` ~253). It fires when the persisted engine is not enabled on this deployment or its version is not supported. It returns HTTP 409 `{"success":false,"errorCode":"ENGINE_MISMATCH","error":"Run state was created by engine '<e>' contract v<n>; this deployment cannot resume it"}`; on AG-UI it is `RunErrorEvent("ENGINE_MISMATCH: …")` via a stream helper like `_run_conflict_stream`. State stays `AWAITING_RESPONSE` and no `ExecutionRun` is created (C-3). An internal state always resumes on the internal engine even when env=opencode.
- D8 (Q3) OpenCode v1 offers **no resume mode**. Its headless tool surface has no awaiting tool (`ask_user_local`, AG-UI tools and hook `ask` are excluded or gated), so it never writes an `llm_states` row, and `durable_resume=false`. The first mode to add (Out-3) is the translated snapshot: a session reference would pin the resume to one pod's sidecar (D11), which breaks replica independence and makes tenant/workspace control depend on sidecar storage. The rejection path is still implemented and tested with a crafted `engine='opencode'` row.
- D9 (Q4, In-4) `engines/capabilities.py` names: `headless_segment, workspace_file_ops, mcp_tools, streaming_output, agui_frontend_interrupts, durable_resume, hooks, permission_asks, subagents, context_compaction, workspace_free`. Internal reports all true. OpenCode reports the first four true (mcp = bridged, contract-tested subset) and the rest false.
- D10 (Q4) `require_capabilities(engine, needs)` runs before `try_create_active_run` and before any `Execution` status change. It returns 400 / `RunErrorEvent` `ENGINE_CAPABILITY_UNSUPPORTED` naming the capability, and logs the denial. Gates:
  - non-empty `frontendTools` (orchestrator) or any AG-UI run with frontend tools;
  - AG-UI run without `workspacePath`, and legacy `executeRequest` (both `workspace_free`);
  - `HOOKS_ENABLED` with a non-empty `load_hooks_for_workspace(...)`.
  A sandbox reprovisioned inside the segment re-checks hooks after `ensure_input_workspace` and fails as an attempt (restored to PENDING). Subagents and compaction are not requested by callers: OpenCode simply exposes no `task_local`, and its native equivalents are disabled per A8 and documented.
- D11 (C-2) Deployment: one OpenCode sidecar per orchestrator process (same pod, loopback). The sidecar gets **no workspace mount**. Each run gets its own OpenCode session rooted in an empty per-run scratch directory, so OpenCode loads no workspace instructions; the orchestrator's composed system prompt (harness manifest, run-binding block) is sent as the session system prompt (A4).
- D12 (C-2, In-3) Tool bridge `engines/opencode/bridge.py`: an internal MCP streamable-HTTP route mounted only when engine=opencode. It is served from `build_tool_registry` local plus MCP tools, minus `task_local`, `ask_user_local` and AG-UI tools. Every call needs a 256-bit per-run bearer token held only in process memory and bound to `(run_pk, LocalToolContext, registry, budget, mcp user token)`. Local calls go through `LocalToolProvider.execute` (tool_policy, `resolve_within`, path policy, workspace opener). MCP calls rebind the user token in the bridge task; it is never sent to the sidecar. The token is revoked before the engine returns. Unknown, revoked or cross-run tokens get 401. OpenCode built-in tools are disabled (A2), so the bridge is the only side-effect path.
- D13 (C-4) `maxToolCalls` keeps its contract (request, then segment config, then `MAX_TOOL_CALLS`) and is counted in the bridge. On exhaustion the engine aborts the session and returns `MAX_TOOL_CALLS`. `OPENCODE_REQUEST_TIMEOUT_SEC` is an infrastructure HTTP/stream-idle guard. `OPENCODE_MODEL` (empty means pass the resolved caller model) and `OPENCODE_PROVIDER` are operator routing settings, inert unless engine=opencode, which is itself off by default. Any OpenCode-native prompt or permission behaviour that cannot be disabled (A4/A11) is documented as engine-inherent and opt-in.
- D14 (C-1) Cancellation: on `cancel_event` or `CancelledError` the engine shields an abort-and-confirm loop. It sends abort, then polls session status until a terminal or idle state (A6) and swallows the repeated `task.cancel()` from `_await_worker_with_diagnostics`. Only then does it revoke the bridge token and re-raise. Timeouts and 5xx keep the worker alive (heartbeat fresh, `worker_cancellation_slow` emitted). Only connection-refused on the loopback sidecar counts as stopped, because a dead process cannot run a session (A7). The claim is never released by elapsed time.
- D15 (C-1) Crash orphans: `execution_runs` stale recovery is unchanged. An orphaned sidecar session cannot touch the workspace because its bridge token died with the process. On startup (engine=opencode), `sweep.py` aborts and deletes every orchestrator-tagged sidecar session (A1); there is one process per sidecar per D11.
- D16 (Q5, C-3) Failure mapping. Every failure first aborts and confirms (D14), then returns `success: False`; the controllers then apply the §12 table unchanged (fresh execute → `PENDING`, AG-UI fresh → thread open, `ExecutionRun` failed).

  | Failure | errorCode |
  |---|---|
  | sidecar down / health or pinned-version failure | `UNAVAILABLE` |
  | per-request timeout | `TIMEOUT` |
  | segment deadline | `TIMEOUT` (existing watchdog) |
  | malformed event, or stream ending without a terminal event | `ENGINE_PROTOCOL_ERROR` |
  | classifiable provider error (A10) | `QUOTA` / `RATE_LIMIT` / `AUTH` / `UNAVAILABLE` |

  Raw sidecar bodies are never returned.
- D17 (In-3) Translation: the OpenCode stream supplies text deltas, usage and completion. The bridge supplies the authoritative `TOOL_CALL_START/ARGS/END`, `tool_calls_info` and counters. `event_sink` publishes through `agui_event_service` when a channel exists and sets `text_streamed`. The output is the same result dict as D1.
- D18 (In-5) `config.py` adds:
  - `ORCHESTRATOR_ENGINE` (`internal`|`opencode`, default `internal`), `OPENCODE_BASE_URL`, `OPENCODE_REQUEST_TIMEOUT_SEC=300`, `OPENCODE_MODEL`, `OPENCODE_PROVIDER=litellm`;
  - an infrastructure-only `OPENCODE_TOOL_BRIDGE_URL` (default `http://127.0.0.1:8000`, matching `main.py`), needed because the sidecar must call back into its own process.
  `validate_config` checks: engine enum; when opencode, a non-empty base URL with no userinfo or query, a positive timeout and a provider enum. Internal needs none of these.
- D19 Out-1: OpenCode does not depend on `ModelClient`; the sidecar reaches LiteLLM through its own provider config (A9). Out-2 discovery is not needed because harness discovery stays orchestrator-side (D11).

### Unverified OpenCode assumptions (gated by Slice 3 spike, pinned version, recorded fixtures)
- A1 Server HTTP API: health/version, create session (with directory, system prompt, model/provider, a tag), prompt, event stream, abort, status, list, delete.
- A2 Built-in tools can be disabled for the session's agent.
- A3 A remote MCP server with a bearer header can be configured (per session preferred, otherwise per sidecar with a per-run header).
- A4 The system prompt is fully replaceable.
- A5 Event shapes for text, usage, completion and error.
- A6 Abort stops activity and an observable idle state exists.
- A7 A restarted sidecar does not auto-resume sessions.
- A8 Native subagents and compaction can be disabled.
- A9 A LiteLLM OpenAI-compatible provider is configurable, and its model id format is known.
- A10 Provider error classification is exposed.
- A11 Permissions can be set non-interactive, so OpenCode never blocks on an ask.
- Any failure of A2, A3, A6 or A11 stops Slices 5–6 (no weaker fallback). A4, A7, A8 and A10 failures are documented and mitigated.

### Delivery slices (each branched from `origin/on-going-dev`, PR into `on-going-dev`, no attribution; C-8)
1. Characterization (C-7, design-doc Phase 0 tests). Entry: none. Exit: new characterization tests green against unchanged code; no `src/` change.
2. Contract + internal extraction + identity (D1–D7). Entry: Slice 1 merged. Exit: full suite green, every new row is `internal/1`, mismatch tests pass, migration note written. Fallback: internal only.
3. OpenCode spike (A1–A11). Entry: Slice 2 merged. Exit: pinned version, `tests/fixtures/opencode/<version>/` transcripts, findings appended to `docs/PLUGGABLE_EXECUTION_ENGINES.md`; no runtime code.
4. Config + capability gating (D9, D10, D18). Entry: Slice 3 has no blocking A# failure. Exit: engine=opencode boots, but the OpenCode engine does not yet report `headless_segment`, so every run fails explicitly with `ENGINE_CAPABILITY_UNSUPPORTED`. Internal is unchanged.
5. Tool bridge (D12, D13). Entry: Slice 4 merged. Exit: bridge security and budget tests pass using a fake MCP client; route absent under internal.
6. OpenCode engine (D11, D14–D17). Entry: Slice 5 merged. Exit: fixture-replay and failure tests pass, `headless_segment` is enabled, and a headless orchestrator execute completes through existing APIs.
- Follow-ups only (Out-3): Engine Phase 5 parity slices per capability (MCP breadth, hooks/asks, interrupts, translated-snapshot resume, compaction, subagents) and Engine Phase 6 rollout. Out-1 (Engine Phase 2) and Out-2 (Engine Phase 3) ship independently.

### Test matrix (Q6; all with no DB, no network, per CONTRIBUTING)
- In-1: `tests/test_engine_contract.py` — the v1 result keys equal the hub keys; the envelope round-trips; an unknown version is rejected.
- In-2:
  - `tests/test_engine_characterization.py` (Slice 1) — orchestrator execute complete / await / fail / `MAX_TOOL_CALLS` / `TIMEOUT`, AG-UI fresh and resume `text_streamed`, legacy execute/resume, snapshot key set.
  - `tests/test_engine_resolver.py` — default internal; fresh runs follow env; resume follows persisted; `ENGINE_MISMATCH` without a claim on all 3 paths; internal state resumes while env=opencode.
  - `tests/test_engine_identity_persistence.py` — run and state rows carry the engine; revision chain and server defaults.
  - `tests/test_engine_architecture.py` — no opencode imports outside `engines/opencode/`; controllers reach the hub only via the engine.
  - Unchanged and green: `test_claim_lifecycle`, `test_stale_claim_recovery`, `test_bounded_run_lifecycle`, `test_agui_cancellation`, `test_resume_idempotency`, `test_orchestrator_claim_lifecycle`, `test_agui_await_resume`, `test_llm_error_contract`, `test_run_lifecycle_phase3`, `test_enum_column_labels`.
- In-3:
  - `tests/test_opencode_engine.py` — replay of the Slice 3 fixtures over an httpx `MockTransport`: completion, streaming to AG-UI TEXT events, bridge tool events, usage, `OPENCODE_MODEL` precedence.
  - `tests/test_opencode_tool_bridge.py` — token scoping, cross-run and revoked tokens get 401, workflow-mode write blocked, traversal and symlinks refused (reuse `test_workspace_containment` fixtures), budget, user MCP token never in a sidecar request, `task`/`ask_user`/AG-UI absent.
  - `tests/test_opencode_failures.py` — down, timeout, malformed, partial stream, deadline, close during run. Each checks: abort is confirmed before the claim is finalized; the worker stays alive on an unconfirmed abort; connection-refused counts as stopped; post-failure states match §12.
  - `tests/test_opencode_startup_sweep.py`.
- In-4: `tests/test_engine_capabilities.py` — each D10 gate on orchestrator, AG-UI and legacy returns `ENGINE_CAPABILITY_UNSUPPORTED`, with no `ExecutionRun` and `Execution` status unchanged; internal is never gated.
- In-5: `tests/test_engine_config.py` (D18 validation); extend `test_public_surface_config.py` (ships `internal`; keys present in both `.env.example` files); extend `test_log_url_redaction.py` for `OPENCODE_BASE_URL`.
- In-6: the docs and `changes/` entries are reviewed in each slice's PR.

### Files
- New:
  - `src/app/services/engines/{__init__,contract,base,capabilities,resolver,internal,errors}.py`
  - `src/app/services/engines/opencode/{client,engine,translate,bridge,sweep}.py`
  - `alembic/versions/20261124_engine_identity.py`
  - the tests listed above
  - `tests/fixtures/opencode/`
- Edit:
  - controllers: `orchestrator_controller.py`, `ag_ui_controller.py`, `agent_controller.py`
  - persistence: `models/execution_models.py`, `execution_state_service.py` (`create_state` engine params), `run_lifecycle.py` (`try_create_active_run` engine params)
  - wiring and config: `main.py` (bridge mount, startup sweep), `config.py`, `src/.env/{localhost,docker}/.env.example`
- Docs:
  - `readme.md` — new Configuration "Execution engine" table, Error codes (`ENGINE_MISMATCH`, `ENGINE_CAPABILITY_UNSUPPORTED`, `ENGINE_PROTOCOL_ERROR`), Running many instances (one sidecar per process)
  - `docs/ORCHESTRATOR.md` — §5 engine seam, §10 persisted engine on resume, §11 config, §12 sidecar cancellation plus Migration head `20261124_engine_identity`, §13 columns, §15 code map
  - `docs/PLUGGABLE_EXECUTION_ENGINES.md` status
  - one `changes/YYYY-MM-DD-<slug>.md` per slice, naming the "Engine Phase" series, never a bare "Phase N"

### Risks
- The AG-UI controller closures are large. Slice 2 swaps only the call expression and keeps `prepare_hook_permission_resume_events` on the internal engine. The Slice 1 tests guard behaviour.
- A shared, multi-process sidecar would break D14/D15. This is documented as a hard deployment constraint, and the startup sweep is limited to tagged sessions.
- The bridge route is reachable by anyone who can reach the port, but every call needs a per-run in-memory token. It is mounted only for opencode, and the readme tells gateways not to expose `/internal/*`.
- OpenCode version drift: the pinned version is checked on health, and the fixtures are re-recorded on upgrade (design risk table).

### Validation
- Per slice: `python -m pytest` green with no DB or network; the new tests above exist and fail before the change where applicable.
- Slice 2: `alembic upgrade head` / `downgrade -1` on a disposable Postgres; also a smoke run of `scripts/smoke_service.py` and `smoke_agui.py` under internal.
- Slice 6: a manual headless execute against the pinned sidecar.
- After every slice, `ORCHESTRATOR_ENGINE=internal` is verified unchanged by the characterization suite.
