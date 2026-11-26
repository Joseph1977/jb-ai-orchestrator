# Plan-review: exhaustive first audit, converging later rounds, and task memory

- **Date:** 2026-11-26
- **Branch:** `fix/plan-review-convergence` (pull request #29 against `feature/opencode-engine-plan`)

## What changed

- The first review of a run is a full audit. The reviewer builds a checklist of
  every brief Constraint, In and Out item, numbered question and Done criterion,
  plus every plan anchor, and reports every blocker, major and minor it finds in
  that one review. `## Coverage` must list each checklist item exactly once.
- Briefs number their items (`C-n`, `In-n`, `Out-n`, `Q<n>`, `Done`), plans anchor
  every line (`D1`, `S2`, ...), and review points cite anchors:
  `N. P### [sev] (<anchors>) issue → fix`.
- Later reviews converge. The planner lists every anchor it added, changed or
  removed in a new `## Changes` section, and `memory_build.py plan-check` compares
  that list with the committed plan. A converge review checks open points,
  pushbacks and the listed anchors. A new issue on an unlisted anchor is a late
  finding: it is allowed only as `[blocker] [late]` with `Missed: <reason>`, and
  any other late issue goes to `## Observations`, is kept in memory and is
  listed to the user at consensus.
- New orchestrator-owned task file `memory.md`, built only by the new
  `scripts/memory_build.py` (`init`, `plan-check`, `scope`, `update`, `feedback`,
  `seed`, `compact`). It has seven fixed sections: Settled decisions, Coverage
  (with an `Audit:` line that selects the review scope), Open points (short
  reference lines; `review.md` stays the source of truth), Observations, Closed
  points, User decisions / retired IDs, and Round log. Both agents read it first.
- Memory limits: at 200 lines or 16 KiB, it is compacted before the next launch
  as its own commit `plan-review(<slug>): memory-compact v<N>`, aiming for 100
  lines and 8 KiB. At 300 lines or 24 KiB, agents stop before reading it. Lines
  are at most 600 characters.
- `prompt_build.py reviewer` requires `--scope audit|converge` and appends the
  matching `AUDIT_BLOCK` or `CONVERGE_BLOCK` from `references/prompts.md`.
- `commit_check.py` accepts the subjects `memory-seed` and `memory-compact`.
  It requires `memory.md` from `brief v1` in new runs and keeps it in every later
  commit. It checks that each review updates memory to match its points and the
  audit state, that compaction keeps the other sections and every closed ID, and
  that launches are refused at the soft limit. `history` reports `memory`
  (`native`, `seeded` or `absent`).
- While memory records `Audit: pending`, `consensus` is refused, so an old
  `NO_FURTHER_COMMENTS` review cannot end a seeded or re-audited run. A
  `feedback` that changes the brief must set `Audit: pending`. The only
  exception is a change limited to `## User feedback` that records newly retired
  point IDs (`--no-audit`).
- Review coverage must agree with each point's anchors. Observations may not
  cite anchors listed in `## Changes`. Removed anchors can be cited and must be
  covered in a converge review.
- Resuming a task without `memory.md` seeds it from the committed files
  (`memory-seed vN`, default `Audit: pending`), so the next review is a full
  audit.
- Optional `--audit-reviewer-model` runs audit-scope reviews with a separate,
  explicitly selected reviewer.
- Default caps are plan 250 / review 150 lines. The rendered prompt budget is
  10,500 bytes (planner plus the larger reviewer variant).
- Tests: `test_memory_build.py` added; `test_commit_check.py`,
  `test_prompt_build.py`, `test_skill_contract.py` and `test_preflight.py` were
  extended. `references/validation.md` adds manual checks M10–M15.

## Why

Reviews did not converge. In task `opencode-engine-replan`, 16 iterations raised
about 25 majors one or two at a time, several of them on plan text that was
unchanged since v1. Reviewers stopped after the first issues, had no record of
what earlier rounds had settled, and no rule separated new defects from ones an
earlier review had missed.

## Migration / breaking

No Alembic revision. New runs need `memory.md` from `brief v1`, and `pre`
refuses `plan-start`/`review-start` in a current-format run without it. Reviewer
prompts need `--scope`. New default caps apply to new runs only. The skill digest
changes, so resume in-flight runs in a new chat. They are seeded with
`memory-seed vN` before their next launch, and their pre-memory commits are
still checked under the earlier rules.
