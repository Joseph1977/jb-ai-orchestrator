# Brief — OpenCodeExecutionEngine implementation plan (replan)
Model: Auto

## Goal
Plan only; implement nothing. Plan the delivery of `OpenCodeExecutionEngine` over an OpenCode sidecar and
its minimum prerequisites, following `docs/PLUGGABLE_EXECUTION_ENGINES.md`, so that
`ORCHESTRATOR_ENGINE=opencode` routes new headless runs through OpenCode while
`ORCHESTRATOR_ENGINE=internal` stays the default and behaves exactly as today.

This is a fresh review run. A previous run (`workspace/outcomes/task-opencode-execution-engine_20261124-134118Z-ca88/`)
produced plan v4 but its history did not validate and its final review never ran, so this run starts a clean
task folder.

## Starting draft
The planner's v1 starts from the previous run's plan v4, which is the input draft:
`workspace/outcomes/task-opencode-execution-engine_20261124-134118Z-ca88/plan.md`

Use it as the baseline. Check it against the full task requirements and constraints below. Keep what is sound and
change what is not. Do not treat it as approved.

Carry-over context from the previous run's reviews (read-only, for reference):
`workspace/outcomes/task-opencode-execution-engine_20261124-134118Z-ca88/review.md` (v2 and v3 points) and
the previous `brief.md`, which holds the full In/Out/Constraint text. The two points the previous plan v4
claims to have fixed are P004 (AG-UI and legacy tool gating before `Execution` creation) and P007
(one absolute segment deadline covering pre-claim gating). Verify both independently.

## Constraints
Full text in the previous `brief.md`. Summary:
- C-1 Engine owns bounded execution and cancellation; a run claim is released only after its worker has stopped; crash-orphaned claims are recovered.
- C-2 Workspace containment; isolation between conversations and users; credential redaction; tool permission enforcement.
- C-3 A failed segment never closes or permanently disables its session; `ExecutionRun` records the failed attempt.
- C-4 No new global default shapes model behaviour; caller limits such as `maxToolCalls` keep per-request contract.
- C-5 Persisted engine identity wins over environment; no cross-engine resume; no reinterpretation under a new contract version.
- C-6 With `ORCHESTRATOR_ENGINE=internal`, every public request and response is unchanged and no OpenCode process is required.
- C-7 Internal-engine extraction lands first as its own slice with characterization tests.
- C-8 Branch from `origin/on-going-dev`; PRs target `on-going-dev` only; no tool, model, vendor or assistant attribution.

## Scope
### In
- In-1 `ExecutionEngine` protocol and v1 request, event, result and await-snapshot schemas, as far as OpenCode needs them.
- In-2 `InternalExecutionEngine` wrapper over `ToolExecutionHub.process_request`; engine resolver at orchestrator, AG-UI and legacy entry points; persist `engine` and `engineContractVersion`; resume rejects engine mismatch.
- In-3 `OpenCodeExecutionEngine` headless capability set: config and health check, session start, prompt, event streaming, cancellation, completion, translation of tool activity to orchestrator events.
- In-4 Capability reporting and gating before a run starts, with clear validation errors for unsupported flows.
- In-5 Config: `ORCHESTRATOR_ENGINE`, `OPENCODE_BASE_URL`, `OPENCODE_REQUEST_TIMEOUT_SEC`, `OPENCODE_MODEL`, `OPENCODE_PROVIDER`; missing OpenCode config errors only when `ORCHESTRATOR_ENGINE=opencode`.
- In-6 Tests (characterization, contract, failure, security) and docs: `readme.md`, `docs/ORCHESTRATOR.md`, a `changes/` entry per slice, alembic migration note if persistence changes.

### Out
- Out-1 `ModelClient` / `LiteLLMModelClient` extraction (Phase 2). Mention only where OpenCode depends on it, and state that it does not.
- Out-2 OpenCode playbook discovery adapter (Phase 3); ships independently.
- Out-3 Parity expansion beyond the headless set (Phase 5) and production rollout (Phase 6). Follow-up slices only.
- Out-4 Any change to `jb-web-code`, per-request or per-tenant engine override, replacing the internal engine.

## Required answers (from the original request)
1. Current code paths and persisted fields that define one run segment and its await snapshot, so the contract matches reality.
2. Where engine identity and contract version are stored; whether an alembic revision is needed; backfill of existing rows as internal; the exact resume-mismatch error.
3. Resume mode OpenCode starts with (translated snapshot or session reference), why, and how replica independence and tenant and workspace controls apply.
4. How each design-doc capability is reported and where gating happens before a run starts.
5. Failure, cancellation and deadline behaviour per sidecar failure mode, mapped to C-1 and C-3.
6. Test matrix naming tests per In item that must exist and pass.
7. Delivery slices with entry and exit criteria; `ORCHESTRATOR_ENGINE=internal` as safe fallback after each.

Evidence rule: OpenCode server API and its session, event and permission semantics not documented in the repo are
unverified assumptions, each with a named verification step placed before the slice that depends on it. Do not invent API shapes.

## Relevant files
- `AGENTS.md`, `CONTRIBUTING.md`, `docs/PLUGGABLE_EXECUTION_ENGINES.md`
- `docs/ORCHESTRATOR.md` §3, §5, §10, §11, §12, §13
- `readme.md` (API reference, Error codes, Configuration, Running many instances)
- `src/app/services/tool_hub.py`, `src/app/controllers/orchestrator_controller.py`, `ag_ui_controller.py`, `agent_controller.py`
- `src/app/services/session_close_service.py` (`manage_run`), `run_lifecycle.py`, `execution_state_service.py`
- `src/app/models/execution_models.py`; `alembic/versions/` (head `20250816_phase3_lifecycle_persistence.py`)
- `src/app/config.py`, `src/.env/{localhost,docker}/.env.example`
- `tests/` (claim, stale-claim, bounded-run, AG-UI cancellation, resume idempotency suites)
- `changes/README.md`

## User feedback
None
