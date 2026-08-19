# Binding Phase 5 — Git input and deployable migrations

- **Date:** 2026-08-17
- **Branch:** `phase-5-orchestrator-git-input`

## What changed

- Orchestrator runs can bind a **Git repository as input**, including a specific
  branch. New `src/app/services/binding_contract.py` validates the contract;
  `workspace_manager.py` and `binding_runtime.py` carry it through.
- Migrations are now deployable: the three Alembic revisions were reworked to
  run cleanly from scratch and in sequence, and the Dockerfile/`.dockerignore`
  updated so the image can apply them on start.
- Fixed a PostgreSQL thread-close lookup that failed to find the session row.
- Added `scripts/orchestrator_continuity_exercise.py` for local binding and
  output continuity checks.
- Test coverage in `tests/test_git_branch_binding.py`, plus Binding Phase 3
  lifecycle and close tests updated.

## Why

Callers needed to point a run at a playbook repository rather than a
pre-mounted folder, and the migration path had to work in a real deployment
rather than only against a hand-built database.

## Migration / breaking

**Migrations changed.** `20250124_create_execution_state_tables`,
`20250802_add_orchestration_fields`, and `20250816_phase3_lifecycle_persistence`
were all revised. A database previously initialised with SQLAlchemy
`create_all` — including one with partially present Binding Phase 3 tables — needs the
handling described in `docs/ORCHESTRATOR.md` §13. Run `alembic upgrade head`
on deploy.
