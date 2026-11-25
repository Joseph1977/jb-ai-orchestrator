# Brief — OpenCodeExecutionEngine implementation plan (continued)
Model: Auto

## Goal
Plan (plan only; implement nothing) the delivery of the `OpenCodeExecutionEngine` adapter over an
OpenCode sidecar, plus the minimum prerequisites it needs, following
`docs/PLUGGABLE_EXECUTION_ENGINES.md`, so that `ORCHESTRATOR_ENGINE=opencode` routes new headless runs
through OpenCode while `ORCHESTRATOR_ENGINE=internal` stays the default and behaves exactly as today.

This run supersedes the earlier planning run in
`workspace/outcomes/task-opencode-execution-engine_20261124-134118Z-ca88/`, which cannot resume.
Its goal, constraints and scope are carried over unchanged below. Starting point:
- The earlier run's `plan.md` (its plan version 4) is the baseline plan. The planner starts from it,
  keeps what holds against the current repository, and corrects what does not. Plan version 1 of
  this run must be a complete, self-contained plan, not a delta against the earlier file.
- The earlier run's `review.md` (its review version 3) raised two major points that its plan
  version 4 accepted: P004 (AG-UI capability gates must run before `_create_agui_execution` creates
  the RUNNING `Execution` row, so a rejection leaves no `Execution` or `ExecutionRun` row) and P007
  (one absolute segment deadline, started before gating, must cover pre-claim tool resolution and
  the managed run, with cumulative-timeout tests). The reviewer of this run must verify both fixes
  against the current repository code, not take them as settled. Point IDs in this run start fresh
  at P001; the earlier IDs are context only.

The plan must answer, from repository evidence:
1. Which current code paths and persisted fields define "one run segment" and its await snapshot today
   (`ToolExecutionHub.process_request` in `src/app/services/tool_hub.py`, the orchestrator and AG-UI
   controllers, the resume and persistence models), so the `ExecutionEngine` contract matches reality.
2. Where engine identity and contract version are stored, whether that needs an alembic revision, how
   existing rows are backfilled as internal, and the exact resume-mismatch error.
3. Which resume mode OpenCode starts with (translated snapshot or session reference), why, and how
   replica independence and tenant and workspace controls apply to it.
4. How each design-doc capability is reported and where gating happens before a run starts.
5. The failure, cancellation and deadline behaviour for each sidecar failure mode, mapped to C-1 and C-3.
6. A test matrix that names, for each In item, the tests that must exist and pass before it is done.
7. Delivery slices, each independently reviewable, with entry and exit criteria and
   `ORCHESTRATOR_ENGINE=internal` as the safe fallback after every slice.

Evidence rule: planner and reviewer can only read this repository. Anything about the OpenCode server
API or its session, event and permission semantics not documented in the repo must be listed as an
unverified assumption, with a named verification step (a spike or fixture) placed before the slice
that depends on it. Do not invent API shapes.

Done means: a reviewed plan a developer can execute slice by slice, where every acceptance criterion
in the design doc maps to a slice and a test, every constraint below is met by named design
decisions, and unverified OpenCode assumptions are isolated and gated.

## Constraints
The plan must show how each is met.
- C-1 The engine owns bounded execution and cancellation, and releases a run claim only after its
  worker has actually stopped. Say how an OpenCode sidecar session is cancelled and confirmed stopped
  before the claim is released, and how crash-orphaned claims are recovered.
- C-2 Workspace containment and isolation between conversations and users. Say how the sidecar is
  confined to the run's workspace, and how tenant isolation, credential redaction and tool permission
  enforcement are preserved.
- C-3 A failed segment never closes or permanently disables its session. A sidecar failure (down,
  timeout, malformed event, partial stream) is recorded on `ExecutionRun` and leaves the execution,
  pending interaction or thread in the executable state it actually had.
- C-4 No new global default may shape model behaviour. Any new limit is infrastructure-only or off by
  default. Caller-owned limits such as `maxToolCalls` keep their per-request contract with a
  segment-config fallback.
- C-5 Persisted engine identity wins over the environment. An OpenCode run never resumes through the
  internal engine, or the reverse, and old state is never reinterpreted under a new contract version.
- C-6 With `ORCHESTRATOR_ENGINE=internal`, every existing public request and response is unchanged
  and no OpenCode process is required.
- C-7 Land the internal-engine extraction as its own reviewable slice with characterization tests
  before any OpenCode code, per the design doc's delivery slices.
- C-8 Branch from `origin/on-going-dev`; pull requests target `on-going-dev` only. No tool, model,
  vendor or assistant attribution in any output.

## Scope
### In
- In-1 `ExecutionEngine` protocol and version-1 request, event, result and await-snapshot schemas,
  only as far as `OpenCodeExecutionEngine` needs them (design doc "Execution engine contract",
  Phase 0 contract part).
- In-2 `InternalExecutionEngine` as a structural wrapper around the current
  `ToolExecutionHub.process_request` path, an engine resolver at the orchestrator, AG-UI and
  legacy-agent entry points, and persistence of `engine` and `engineContractVersion` on new runs,
  with resume rejecting an engine mismatch (Phase 1).
- In-3 `OpenCodeExecutionEngine` for the headless capability set: config and health check, session
  start, prompt submission, event streaming, cancellation, completion, and translation of OpenCode
  tool activity into orchestrator events (Phase 4).
- In-4 Capability reporting and gating, so controllers return a clear validation error for an
  unsupported flow (AG-UI frontend-tool interrupts, subagents, context-compaction parity, and any
  other capability not yet tested), per the design doc capability table.
- In-5 Configuration: `ORCHESTRATOR_ENGINE`, `OPENCODE_BASE_URL`, `OPENCODE_REQUEST_TIMEOUT_SEC`,
  `OPENCODE_MODEL`, `OPENCODE_PROVIDER`. Missing OpenCode config is an error only when
  `ORCHESTRATOR_ENGINE=opencode`.
- In-6 Tests (characterization, contract, failure, security) and documentation: `readme.md`,
  `docs/ORCHESTRATOR.md`, a `changes/` entry per slice, and an alembic migration note if persistence
  changes.

### Out
- Out-1 `ModelClient` / `LiteLLMModelClient` extraction (Phase 2). Mention it only where OpenCode
  depends on it, and state that it does not.
- Out-2 OpenCode playbook discovery adapter (Phase 3). It ships independently.
- Out-3 Parity expansion for MCP, permissions, interrupts, resume and compaction beyond the headless
  set (Phase 5), and production rollout (Phase 6). List them as follow-up slices only.
- Out-4 Any change to `jb-web-code`, per-request or per-tenant engine override, and replacing the
  internal engine.

## Relevant files
Starting point (earlier run; read-only context):
- `workspace/outcomes/task-opencode-execution-engine_20261124-134118Z-ca88/plan.md` (baseline plan)
- `workspace/outcomes/task-opencode-execution-engine_20261124-134118Z-ca88/review.md` (P004, P007)

Applicable policy and standards (read in full):
- `AGENTS.md`
- `CONTRIBUTING.md`
- `.cursor/rules/concise-responses.mdc` (alwaysApply; chat style only)
- `docs/PLUGGABLE_EXECUTION_ENGINES.md` (design source for this task)
- `docs/ORCHESTRATOR.md` §3 (harness, workspace containment), §10 (pause, resume, deployment
  guarantees), §12 (segment bindings, lifecycle, close; includes "Migration and backfill"),
  §13 (persistence schema); also §5 (`ToolExecutionHub.process_request()`), §11 (configuration)
- `readme.md` (caller-facing API contract; "API reference", "Execution lifecycle", "Error codes",
  "Configuration", "Running many instances")

Relevant source:
- `src/app/services/tool_hub.py` — `ToolExecutionHub`, `process_request` (and its internal caller)
- `src/app/controllers/orchestrator_controller.py` — `execute`, `_managed_hub_process`, call sites
- `src/app/controllers/ag_ui_controller.py` — binding resolution, `_create_agui_execution`,
  `_stream_run`, `_run_fresh_segment`, `_run_resume_segment`, interrupts, resume
- `src/app/controllers/agent_controller.py` — legacy-agent `process_request` call sites
- `src/app/models/execution_models.py` — `ThreadSession`, `ExecutionRun`, `Execution`, `LLMState`
- `src/app/services/session_close_service.py` — `manage_run` (heartbeat, deadline watchdog)
- `src/app/services/execution_state_service.py`, `run_lifecycle.py`, `run_registry.py`,
  `resume_options.py`, `execution_context.py`
- `src/app/services/agui_interrupt.py`, `agui_service.py`, `agui_event_service.py`
- `src/app/services/workspace_io.py`, `workspace_manager.py`, `path_policy.py`, `tool_policy.py`,
  `local_tool_provider.py`, `hooks/`, `tool_call_counters.py`, `context_compaction.py`
- `src/app/services/mcp_agent_service.py` (LiteLLM access; Out-1 context only)
- `src/app/config.py`; `src/.env/localhost/.env.example`; `src/.env/docker/.env.example`
- `alembic/versions/` (latest: `20250816_phase3_lifecycle_persistence.py`)
- `changes/README.md` and existing `changes/*.md` entries
- `tests/` — notably `test_claim_lifecycle.py`, `test_stale_claim_recovery.py`,
  `test_bounded_run_lifecycle.py`, `test_agui_cancellation.py`, `test_resume_idempotency.py`,
  `test_orchestrator_claim_lifecycle.py`, `test_workspace_containment.py`, `test_llm_error_contract.py`,
  `test_log_url_redaction.py`, `test_agui_await_resume.py`, `test_public_surface_config.py`
- `docs/comparisons/comprehensive-agent-harness-comparison.md` (only in-repo OpenCode description;
  not an API specification)

## User feedback
None
