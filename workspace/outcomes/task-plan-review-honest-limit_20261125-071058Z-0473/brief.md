# Brief — Plan-review skill: honest rounds and a real iteration limit
Model: Auto

## Goal
Plan (do not implement) fixes to the plan-review skill in `.cursor/skills/plan-review/` so that every
round is a complete, honest round, and reaching the iteration limit stops the loop with the true open
findings. Reaching the limit never produces consensus. Consensus may happen at any iteration
(including iteration 1) or not at all.

Observed problem (two real runs on branch `feature/opencode-engine-plan`): both used every
iteration and reached consensus only in iteration 4 of 4. Point trends were 6→1→2→(not run) and
3→1→1→0. Causes:
- The reviewer prompt allows new blocker/major points after v1 only with a `Caused by <ID>:` prefix;
  otherwise they must be minor. Real issues missed in v1 get downgraded or hidden later.
- The reviewer prompt says "Raise every issue in v1" and "Report final-iteration blockers/majors",
  and both prompts carry "Iteration N of L", so agents know when the budget is ending.
- Later reviews re-check only earlier points, not the whole plan.
- The orchestrator added non-template guidance to a planner prompt in the last round, which steered
  the outcome. "Prompts are exact templates plus filled scalars" exists but nothing enforces it.

The plan must answer:
1. For each In item: the exact text or rule changes, with file and section.
2. The new limit-gate flow as a short state sequence: review committed → decide → limit check →
   next iteration, limit-reached, or consensus.
3. How the reviewer is kept from downgrading real issues and from reviewing only earlier points, and
   how that is validated.
4. A test list per In item (unit/contract tests that must exist and pass), plus the manual checks in
   `references/validation.md`.

Done means a reviewed plan a developer can execute that removes the convergence-by-limit behaviour.

## Constraints
- C-1 Editing the skill changes its digest. The plan must say how implementation edits the skill
  without running inside an active plan-review session (for example, implement outside the loop,
  then re-run the unit suite).
- C-2 Existing task folders on this branch must stay readable. Say whether `commit_check.py` history
  validation accepts old runs or flags them, and what message it gives.
- C-3 No tool, model, vendor or assistant attribution in any output. Pull requests target
  `on-going-dev` only.

## Scope
### In
- In-1 Planner completeness: each planner version answers every open point in full and replans the
  whole scope. No partial answers, no deferring a point to the next round.
- In-2 Reviewer completeness: each review, in every iteration, reviews the whole plan against the
  brief and repo and reports ALL issues it finds at their true severity. Remove the rule that new
  later points must be minor unless prefixed "Caused by". Keep stable point IDs and pushback
  assessment.
- In-3 Iteration blindness: planner and reviewer prompts no longer reveal the iteration number or
  limit, or any "final iteration" wording. Only the orchestrator and `status.md` track iterations.
- In-4 Limit gate placement: the limit check runs before starting the next iteration, not inside a
  round. If the review at iteration N = limit still has open points (any severity, including
  majors), the loop stops with `State: limit-reached`, the open points unchanged and listed, and no
  consensus. The user may grant K more iterations explicitly; nothing else continues the loop.
- In-5 Honest status: `status.md` distinguishes the outcomes explicitly. Examples: consensus at
  iteration 2 of 6 with open points 0/0/0, and limit-reached at iteration 4 of 4 with open points
  0/1/0. Early consensus (including iteration 1) is valid and expected.
- In-6 Orchestrator prompt integrity: the orchestrator sends the exact template plus filled scalars
  only. Define precisely which scalars are allowed (task folder, next point ID, header lines, length
  caps, pushed-back IDs). Make it checkable, for example a static contract test or a prompt-builder
  that refuses extra text.
- In-7 Retry and transition rules that caused corrupted history in these runs:
  - define the exact commit subject for a retried plan-start/review-start (the runs invented
    `plan-start v1 retry`, which history validation rejected);
  - require each commit to carry the State and Iteration values that step needs, set before the
    commit (runs committed `plan-start v2` with Iteration 1, and `plan v4` / `review-start v4` with
    State still `planner-running`).
- In-8 Tests and docs: update `scripts/test_skill_contract.py`, `commit_check.py` history validation
  (new or changed states and subjects), `references/validation.md` manual checks, and the
  `SKILL.md` / `references/operations.md` / `references/limit.md` text so they all agree.

### Out
- Out-1 Changes to orchestrator product code under `src/`, `alembic/`, or `ag-ui-demo/`.
- Out-2 Redesigning the repo guard, skill digest, or recovery-marker mechanics, beyond what In-7
  needs.
- Out-3 Changing the planner/reviewer model-family rules.

## Relevant files
- `AGENTS.md` (Contributing; Keep the docs current; Attribution) — repository policy.
- `CONTRIBUTING.md` (Making a change; House style) — repository policy.
- `.cursor/rules/concise-responses.mdc` (alwaysApply) — chat verbosity policy.
- `.cursor/skills/plan-review/SKILL.md` — Invocation; Loop; Minors close; Reviewer prompt; Planner
  prompt; Consensus; Triggers; Rules; Validation.
- `.cursor/skills/plan-review/references/operations.md` — status.md format; Commit transaction;
  Allowed files; Format checks (`Caused by` rule).
- `.cursor/skills/plan-review/references/limit.md`, `recovery.md`, `resume.md`, `integrity.md`,
  `validation.md`.
- `.cursor/skills/plan-review/scripts/commit_check.py` (history/subject/state validation) and
  `scripts/test_commit_check.py`.
- `.cursor/skills/plan-review/scripts/test_skill_contract.py` (static contract tests).
- Existing runs that must stay readable (C-2):
  `workspace/outcomes/task-opencode-execution-engine_20261124-134118Z-ca88/`,
  `workspace/outcomes/task-opencode-engine-replan_20261124-143650Z-fe98/`.

## User feedback
None
