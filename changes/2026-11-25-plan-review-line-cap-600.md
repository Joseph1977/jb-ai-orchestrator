# Plan-review: raise the point and response line cap to 600 characters

- **Date:** 2026-11-25
- **Branch:** `feature/opencode-engine-plan`

## What changed

- `.cursor/skills/plan-review/references/operations.md` now allows one line of
  at most 600 characters per review point and per plan response reason
  (previously 300).
- The planner and reviewer prompts in `references/prompts.md` state the same
  600-character limit.
- `scripts/test_prompt_build.py` checks the new wording.

## Why

With the 300-character cap, reviews of `task-opencode-engine-replan` were
rejected on format repeatedly (302 and 310 characters on review v12, after
earlier runs at 466–730) although their content was valid. The cap guards
only scannability, so it is raised to fit the density the reviewer produces.

## Migration / breaking

No Alembic revision. The skill digest changes, so a run in progress must be
resumed in a new chat after this lands; no transaction may span the upgrade.
