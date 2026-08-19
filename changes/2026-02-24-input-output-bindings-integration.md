# Input/output bindings line merged to `on-going-dev`

- **Date:** 2026-02-24
- **Branch:** `feat/input-output-bindings`

## What changed

This landed the long-running integration branch on `on-going-dev`. It carried
Binding Phase 3 (PR #25), Binding Phase 4 (PRs #26 and #27) and Binding Phase 5
(PR #28), which have their own entries where backfilled, plus two fixes that
merged into the line directly:

- **Interrupt and error contract** (PR #29) — orchestrator interrupt and error
  contracts are preserved across resume, provider failures are classified rather
  than surfaced raw, null fields are omitted from interrupts, and provider
  diagnostics were tightened. Covered by `tests/test_llm_error_contract.py`.
- **Runtime cleanup on Python 3.11** (PR #31) — fixed runtime path cleanup that
  misbehaved on 3.11.

## Why

The binding work was developed as one line and integrated once, so the phase
PRs above merged into the feature branch rather than into `on-going-dev`
individually. This entry records the point at which the whole line shipped.

## Migration / breaking

Carries the Alembic changes from Binding Phase 5 (PR #28) — see
`2026-08-17-binding-phase-5-orchestrator-git-input.md`. Nothing additional.
