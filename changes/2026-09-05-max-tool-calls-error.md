# Tool-budget exhaustion has a stable error code

**Date:** 2026-09-05
**Branch:** `fix/max-tool-calls-error`
**Target:** `on-going-dev`

## What changed

Both tool-budget exhaustion paths now return `MAX_TOOL_CALLS` as a structured
error code. AG-UI maps the code to a safe message explaining that the segment
stopped at its tool limit and the session remains available.

Controller coverage pins the existing recovery contract: execute failures return
the execution to `PENDING`, while resume failures restore
`AWAITING_RESPONSE` with the existing state identifier.

## Why

Previously tool-budget exhaustion carried only a human-readable string. Callers
could not distinguish this bounded, recoverable stop from an unspecified
upstream failure and therefore fell back to generic error messaging.

## Migration and compatibility

No migration is required. The response remains HTTP 200 with `success: false`;
the added error code is backward-compatible for callers that ignore unknown
fields.

## Verification

- Focused tool-loop, AG-UI mapping, and controller tests: 92 passed.
- Full orchestrator suite: 767 passed.
