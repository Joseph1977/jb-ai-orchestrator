# Recovery — commit, post, guard, and format failures

Read this only after the whole-skill digest still matches. Skill-integrity mismatches use the inline
`SKILL.md` recovery and must not read this potentially changed file. For ordinary nonzero guard,
commit, `post`, or format results: never pass `--no-verify`, amend, reset, or revert; never retry a
guard failure or re-baseline; never commit a `Recovery:` line.

## Nonzero commit or `post`

Stop. Report the pre-commit `HEAD`, the resulting commit (or that none was created), and every failure. Do not continue the loop. The next action is an explicit user recovery choice.

If the commit command itself fails before `post` runs, still run `post`. It reports that no commit was created.

`post` (and a failed `post` command path) appends an uncommitted marker to the worktree `status.md`:

`Recovery: commit-check-failed commit=<sha|none> pre-head=<sha|unknown>`

`commit=none` means `HEAD` did not move. Otherwise `commit` is the new `HEAD` and `pre-head` is the recorded parent. Leave `status.md` dirty with that marker. Never stage or commit it. A committed `status.md` must never contain a `Recovery:` line. `history` exits 1 when the marker is present or a committed copy exists.

Remove the marker only as part of the recovery the user explicitly selects. Clearing it, amending the commit, or resetting the branch without that choice is forbidden. Say which files would change before asking.

If inline skill-integrity recovery records a commit before verified `post`, that failure is terminal
for plan-review. Its authenticated commit manifest is diagnostic only: the recovery marker changed
the inspected `status.md`, so `post` is never rerun. The user repairs the branch outside this
workflow and starts a new run.

## Guard failure (exit 1 or 2)

Fail closed on exit 2. On either nonzero result:

1. Restore only the owned file to its exact pre-run state. Restore it from `HEAD` when it existed there. On its first version, delete only that newly created owned file.
2. Stop the round. Report the outside delta (exit 1) or the guard error (exit 2), plus the current branch and `HEAD`.
3. Do not edit or commit `status.md`. The already committed `planner-running` or `reviewer-running` state is the durable failure marker. That is why `plan-start` and `review-start` stay.
4. Never restore, stage, or commit any other path. It may be concurrent user work.
5. Never retry a guard failure in the same invocation, and never take a new baseline that absorbs the delta.

A later resume of `planner-running` or `reviewer-running` still needs the user's explicit approval before a fresh guarded run. See `references/resume.md`.

## Format failure

A format or consistency failure is one retry, then `stopped`. This includes a plan that defers or splits work across versions or points to an earlier version, a wrong derived `Version:` header, a bad verdict, a missing `Evidence: ` field, a bad maintain line, and a length-cap miss.

- Self-planner output: correct the owned file yourself before `plan vN` or `minors vN+1` is committed. There is no subagent retry.
- Subagent output: only a format failure after guard exit 0 is retriable. Restore the valid pre-run owned file, or remove its invalid first version. Retry once with a new subagent. The retry is a new guarded run: capture a fresh manifest, launch, compare, then validate. Commit its launch as the exact subject `plan-start vN` (or `review-start vN`) again, per `operations.md` §Status before each commit, with no suffix.
- A second format failure: restore again, set `State: stopped` and `Stop reason: error`, record the failure, commit only `status.md` as `stopped vN`, and report. Never leave or resume from invalid output.
- A guard nonzero result is not a format retry. Use the guard-failure steps above.

A missing, truncated, symlinked, or malformed owned file fails these checks and is restored the same way. `lstat` it before reading subagent output, as `references/integrity.md` requires.
