# Binding Phase 4 — deterministic durable output routing

- **Date:** 2026-08-17
- **Branch:** `phase-4-output-routing-hardening`

## What changed

- Durable output routing is now deterministic: the run-binding prompt states
  exactly where the model must write durable state versus runtime scratch, and
  the unbound case says so explicitly.
- Reworked `src/app/prompts/output_binding.yaml` and
  `src/app/prompts/output_unbound.yaml` accordingly.
- Added `tests/test_output_routing_acceptance.py` as the acceptance gate.
- `docs/ORCHESTRATOR.md` and `readme.md` updated to match.

## Why

Output placement was inferable rather than stated, so a model could write
durable artifacts into runtime paths that get cleaned up on close. The
acceptance test pins the behaviour.

## Migration / breaking

None.
