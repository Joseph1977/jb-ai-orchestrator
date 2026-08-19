# Binding Phase 4 — demo adoption of bindings and close

- **Date:** 2026-08-17
- **Branch:** `phase-4-demo-bindings-close`

## What changed

- `ag-ui-demo` migrated to the canonical binding lifecycle and the close
  endpoints: fresh runs send `input` (and optional `output`, `mode: workflow`)
  only when workflow input is configured, plain chat sends no bindings, and
  resume omits binding changes.
- New `src/config/bindings.ts` and `src/api/closeRetryConfig.ts` in the demo make
  the binding policy and close-retry behaviour explicit and testable.
- Documented the Binding Phase 4 caller policy in `docs/ORCHESTRATOR.md` and `readme.md`;
  refreshed `docs/DOCUMENTATION_INDEX.md` and the `.env` examples.

## Why

The demo is the reference caller. Until it followed the same binding and close
policy as the real clients, the contract had no worked example and drift went
unnoticed.

## Migration / breaking

None to the service. No orchestrator web-specific behaviour was added — the change
is caller-side plus documentation.
