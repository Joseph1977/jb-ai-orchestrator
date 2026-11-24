# Task files, commits, and format contracts

Digest-check/read once per chat before new task-folder creation (resume: before first write). At
commits/output validation recheck digest; reread only after context loss.

## Task files

Exact `Model: <identity>`: system identity for brief/status, self/planner slug for plan, reviewer
slug for review.

`brief.md`:

```markdown
# Brief — <title>
Model: <orchestrator identity>

## Goal
...
## Constraints
...
## Scope
### In
...
### Out
...
## Relevant files
...
## User feedback
<dated entries; "None" initially>
```

`plan.md`:

```markdown
# Plan — <title>
Version: N
Model: <planner identity>

## Response to review v<N-1> <!-- omit in v1 -->
| Point ID | Decision | Reason / change made |
|---|---|---|
| P001 | accepted / partly accepted / pushed back / closed by user | ... |

## Plan
<what / where / why, steps, files, risks, validation>
```

`review.md`:

```markdown
# Review — <title>
Version: N
Model: <reviewer slug>
Verdict: CHANGES_REQUESTED | APPROVE_WITH_MINORS | NO_FURTHER_COMMENTS

## Points
1. P001 [blocker|major|minor] <issue> → <suggested fix>

## Pushback assessment <!-- omit in v1 -->
P001: accept rebuttal | maintain, because <path/standard/brief ref>: <one line>
```

`status.md`:

```markdown
# Status — <title>
Model: <orchestrator identity>
Iteration: N of <limit>
Length caps: plan <n>, review <n>
State: planning | planner-running | reviewing | reviewer-running | reviewed | limit-reached | awaiting-user | approved | stopped
Working branch: <approved non-protected branch>
Branch authorization: approved-current@<sha> | from-<source-branch>@<sha>
Planner mode: self | subagent
Planner model: <identity>   Reviewer model: <slug>
Model changes: <none | "v4: reviewer old → new">
Open points: <blockers>/<majors>/<minors>
Trend: <total-open count per iteration>
Next point ID: P001
Retired point IDs: none | P001, P004
Stop reason: none | user | error
Next action: <one line>
```

No caps means plan 150/review 50. Change only by explicit instruction plus dated brief feedback.
Never record new commit/`Recovery:`. Resume: `git log -1 -- <task folder>`.

Brief Relevant files: applicable repo policy/standards plus relevant source; exclude absent files,
pathless user rules; prefer section/symbol/ranges.

## New-run task path checks

Candidate must not exist as a file, directory, or dangling symlink. On collision, rename and rerun
ignore preflight for that exact candidate. After plain `mkdir`, `lstat` a real directory and require
its canonical path to be a direct child of canonical `workspace/outcomes`.

## Commit transaction

Commit every completed step with only its allowed task paths:

```bash
ROOT="$(git rev-parse --show-toplevel)"
CHECK="$SKILL_ROOT/scripts/commit_check.py"
CHECK_FILE="$(mktemp "${TMPDIR:-/tmp}/plan-review-commit.XXXXXX")"
PRE_HEAD="$(git rev-parse HEAD)"
git add -- <allowed paths>
CHECK_SHA="$(python3 -I -B "$CHECK" pre --repo "$ROOT" --output "$CHECK_FILE" <allowed paths>)"
git diff --cached -- <allowed paths>
# Recheck SKILL_BASELINE immediately before commit.
git commit --only -m "plan-review(<slug>): <step> v<N>" -- <allowed paths>
python3 -I -B "$CHECK" post --repo "$ROOT" --baseline "$CHECK_FILE" \
  --baseline-sha256 "$CHECK_SHA" --status-file workspace/outcomes/<task>/status.md
rm -f "$CHECK_FILE"
```

On `pre`/commit/`post` failure stop for digest-gated recovery. Inspect the allowed diff; never
`--no-verify`, amend, reset, revert, or commit recovery markers.

Allowed files:

- `brief` / `feedback`: `brief.md`, `status.md`
- `plan-start` / `review-start`: `status.md`
- `plan` / `minors`: `plan.md`, `status.md`
- `review`: `review.md`, `status.md`
- `consensus` / `approved` / `limit-reached` / `stopped`: `status.md`

Initial commit: `brief v1`. Keep `plan-start`/`review-start` as durable launch markers. Before commit,
require current=`Working branch`, unprotected, with authorization SHA ancestor of `HEAD`. Branch
replacement/rebase needs fresh approval. Task commits are authorized; push is not.

## Format checks

Require exact unformatted `Version: N`, `Model: <expected>`, and other headers.

- Points match `^\d+\. P[0-9]{3,} \[(blocker|major|minor)\] .+ → .+$`; ordinals start at 1.
- IDs increase from `Next point ID`, stay while open/reframed, and are never repeated, reused,
  renumbered, or retired while current/maintained.
- In v2+, a new blocker/major or severity rise starts
  `Caused by <accepted-or-partly-accepted-ID>:` or `Caused by feedback:`. Feedback requires a
  feedback commit after the prior review. A cited point ID must differ from the current point's own
  ID. Otherwise the new point is minor.
- Plan v2+ has one row per preceding review point, no older-only/duplicates. Decision is exactly
  `accepted`, `partly accepted`, `pushed back`, or `closed by user`; only retired IDs use the last,
  and every retired ID in that review must use it.
- `pushed back` and `partly accepted` include `Evidence: `. Every pushback has exactly one
  assessment. Accept lines match `^P[0-9]{3,}: accept rebuttal$`; maintain lines match
  `^P[0-9]{3,}: maintain, because .+: .+$`. Accept closes it; maintain keeps it open and in Points.
  No assessment is allowed for a non-pushback.
- `CHANGES_REQUESTED` has a blocker/major. `APPROVE_WITH_MINORS` has points, all minor, with no
  maintained blocker/major. `NO_FURTHER_COMMENTS` has no points or maintained pushback.
- Enforce caps (default plan 150/review 50), one ≤300-character line per point/response reason.

Subagent format failure gets one `recovery.md` retry, then stops. Correct self-planner before commit.
