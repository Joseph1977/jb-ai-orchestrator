# Plan-review: prompts state the 300-character line rule and the review title

- **Date:** 2026-11-25
- **Branch:** `fix/plan-review-line-limit` (pull request against `feature/opencode-engine-plan`)

## What changed

- The reviewer prompt in `.cursor/skills/plan-review/references/prompts.md` now
  states the exact title (`# Review — <plan title>`) and that each point is one
  line of at most 300 characters.
- The planner prompt now states that each `## Response to review` row is at
  most 300 characters.
- The rendered planner+reviewer prompt budget rises from 7,500 to 7,600 bytes
  (`test_skill_contract.py`, `references/validation.md`); the old budget had
  12 bytes of headroom.
- `test_prompt_build.py` checks that the rendered prompts carry these rules.

## Why

`references/operations.md` fails any point or response line over 300
characters and requires the `Review — <title>` heading, but the prompts never
told the agents. Every review attempt in the `task-opencode-engine-replan` run
failed format (point lines of 466–730 characters, and once a wrong title), so
the run stopped before any review could be committed.

## Migration / breaking

No Alembic revision. The skill digest changes, so a run in progress must be
resumed in a new chat after this lands; no transaction may span the upgrade.
