# Plan-review: honest rounds and a real iteration limit

- **Date:** 2026-11-25
- **Branch:** `fix/plan-review-honest-limit` (pull request against `on-going-dev`)

## What changed

- Planner and reviewer prompts moved to `references/prompts.md` and are rendered
  only by the new `scripts/prompt_build.py`, which accepts a fixed set of scalars
  and refuses anything else (exit 2). Prompts no longer carry the iteration
  number, the limit, or any final-round wording, and agents no longer read
  `status.md`; each derives its `Version:` header from the task files.
- Every plan version replans the whole brief scope and answers every open point
  in full; deferral wording or pointing to an earlier version fails format.
- Every review covers the whole plan and reports each issue at its true severity.
  The `Caused by` rule and the "final iteration" instruction are gone; stable
  point IDs and pushback assessment are unchanged.
- The limit is checked once, after a review is committed. Open points at the
  limit (any verdict) end in `limit-reached` with the review's Iteration and
  Open points unchanged and no consensus. Only an explicit user K continues,
  through a `feedback` commit that changes `status.md` alone (`Iteration: N of
  N+K`); `brief.md` changes there only for dated feedback or decided point IDs.
- Consensus requires a `NO_FURTHER_COMMENTS` review and may occur at any
  iteration, including the first. A Minors close is now an ordinary reviewed
  round (`plan-start vN+1`, `plan vN+1`, `review vN+1`) and only when N is
  below the limit.
- `status.md` gains an `Outcome:` line (`none`, consensus, limit-reached or
  stopped, each with iteration and open points).
- Retried launches reuse the exact subject `plan-start vN` / `review-start vN`
  after incrementing the attempt in `Next action`. `commit_check.py pre` takes a
  required `--subject` and rejects a wrong subject, State, Iteration or Outcome
  before the commit; `post` fails if the commit subject differs.
- `commit_check.py history` reports `format` (`current` or `legacy`).
  Task folders whose `brief v1` status has no `Outcome:` line keep the previous
  rules and report the note `legacy status format: honest-limit checks not
  applied`; only the retry-subject failure message gains a hint.
- Tests: `test_prompt_build.py` added; `test_commit_check.py` and
  `test_skill_contract.py` extended; `references/validation.md` lists manual
  checks M1–M9.

## Why

Runs reached consensus because the budget ran out, not because the plan was
sound: late issues were downgraded, later reviews re-checked only earlier points,
prompts announced the final iteration, free text was appended to prompts, and
retry/transition commits corrupted history.

## Migration / breaking

No Alembic revision. Breaking for in-progress runs: `pre` requires `--subject`,
the commit manifest is version 3, prompts come only from `prompt_build.py`, the
`minors` commit is legacy-only, and no transaction may span the upgrade. Older
task folders stay readable in legacy mode.
