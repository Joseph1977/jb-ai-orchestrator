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
1. <numbered question the plan must answer; cited as Q1>
Done means: <acceptance; cited as Done>
## Constraints
- C-1 ...
## Scope
### In
- In-1 ...
### Out
- Out-1 ...
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

## Changes <!-- omit in v1 -->
- D3 changed (P001): <what changed, ≤300 characters>
- S4 added (consistency): ...

## Plan
### Decisions
- D1 (In-1, C-2) <decision>
  <indented continuation>
### S1 — <slice name; the heading anchor covers the lines below it>
Entry: ... Exit: ...
```

Anchors: every non-heading line under `## Plan` belongs to an anchor: one capital letter other
than `P` plus digits (`D1`, `S2`, `A3`, `R4`, `T5`) at the start of the line (after an optional
`- `, `* ` or `1. `), or in a `###`/`####` heading that covers the lines below it until the next
heading or anchor. An ID at a line start always defines that anchor, so a line citing another
anchor must not begin with it. Indented lines continue the current anchor. Anchors are unique, never renumbered
or reused. Brief items are cited as `C-<n>`, `In-<n>`, `Out-<n>`, `Q<n>` and `Done`. plan.md has
only the title/header lines and the `## Response to review`, `## Changes` and `## Plan` sections.

`review.md`:

```markdown
# Review — <title>
Version: N
Model: <reviewer slug>
Verdict: CHANGES_REQUESTED | APPROVE_WITH_MINORS | NO_FURTHER_COMMENTS

## Points
1. P001 [blocker|major|minor] (D3, In-2) <issue> → <suggested fix>

## Coverage
- ok: C-1, C-2, In-1, Q1, Done, D1, D2, S1
- D3: P001

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
Outcome: none | consensus at iteration N of L, open points 0/0/0 | limit-reached at iteration L of L, open points b/m/n | stopped at iteration N of L (<reason>)
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

No caps means plan 250/review 150. Change only by explicit instruction plus dated brief feedback.
Never record new commit/`Recovery:`. Resume: `git log -1 -- <task folder>`. No consensus
`Outcome` has nonzero open points. A status without an `Outcome:` line (older run) uses the legacy
rules in `commit_check.py`; `pre` rejects a new `brief v1` without it.

The limit may change only in `brief v1` or `feedback`. `brief.md` never records the iteration count,
an `Iteration:` line, or this run's limit: agents read it.

## Status before each commit

Set `status.md` before `git add`:

| Commit | State | Iteration | Outcome / other |
|---|---|---|---|
| `brief v1` | planning | 1 of L | none |
| `plan-start vN` | planner-running | N of L | none; `Next action: planner writes plan vN — attempt k` |
| `plan vN` | reviewing | N of L | none |
| `review-start vN` | reviewer-running | N of L | none; `Next action` names attempt k |
| `review vN` | reviewed | N of L | none; Open points, Trend, Next point ID recorded |
| `consensus vN` | awaiting-user | N of L | consensus Outcome, open points 0/0/0 |
| `limit-reached vN` | limit-reached | L of L | limit Outcome; Iteration and Open points copied from `review vN` |
| `feedback vN` | planning | N of L′ | none |
| `stopped vN` | stopped | N of L | stopped Outcome; `Stop reason` set |
| `approved vN` | approved | N of L | Outcome unchanged |

Minors close uses the `plan-start`/`plan` rows; the `minors` subject (and its Allowed-files entry) is
legacy-only. Retry: a retried launch commits the exact subject `plan-review(<slug>): plan-start vN`
(or `review-start vN`) again, no suffix, after incrementing `attempt k` in `Next action`, so the
commit has a real diff.

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
CHECK_SHA="$(python3 -I -B "$CHECK" pre --repo "$ROOT" --output "$CHECK_FILE" \
  --subject "plan-review(<slug>): <step> v<N>" <allowed paths>)"
git diff --cached -- <allowed paths>
# Recheck SKILL_BASELINE immediately before commit.
git commit --only -m "plan-review(<slug>): <step> v<N>" -- <allowed paths>   # same string as --subject
python3 -I -B "$CHECK" post --repo "$ROOT" --baseline "$CHECK_FILE" \
  --baseline-sha256 "$CHECK_SHA" --status-file workspace/outcomes/<task>/status.md
rm -f "$CHECK_FILE"
```

On `pre`/commit/`post` failure stop for digest-gated recovery. Inspect the allowed diff; never
`--no-verify`, amend, reset, revert, or commit recovery markers.

Allowed files:

- `brief`: `brief.md` and `status.md`, both required
- `feedback`: `status.md` required; `brief.md` optional, changed only for dated user feedback or
  retired-point decisions (a K-only grant changes `status.md` alone)
- `plan-start` / `review-start`: `status.md`
- `plan` (and legacy `minors`): `plan.md`, `status.md`
- `review`: `review.md`, `status.md`
- `consensus` / `approved` / `limit-reached` / `stopped`: `status.md`

Initial commit: `brief v1`. Keep `plan-start`/`review-start` as durable launch markers. Before commit,
require current=`Working branch`, unprotected, with authorization SHA ancestor of `HEAD`. Branch
replacement/rebase needs fresh approval. Task commits are authorized; push is not.

## Format checks

Require exact unformatted `Version: N`, `Model: <expected>`, and other headers.

- Points match `^\d+\. P[0-9]{3,} \[(blocker|major|minor)\] \(([^()]+)\) .+ → .+$`; ordinals
  start at 1. Every anchor in the parentheses is a brief item or a current plan anchor.
- `## Coverage` lists every brief item and every plan anchor exactly once, in `- ok: <items>` lines
  or as `- <item>: <point IDs>`; every point ID appears there and every listed ID is a point.
- Before `plan vN`, run `python3 -I -B "$SKILL_ROOT/scripts/memory_build.py" plan-check --repo
  "$ROOT" --task workspace/outcomes/<task>`. Exit 1 is a format failure (anchors, sections,
  `## Changes`); exit 2 stops. With a prior review, `## Changes` is required and must list exactly
  the anchors whose text differs from the committed plan.md (skipped once when that plan has no
  valid anchors), with the matching `added`/`changed`/`removed` word; every `accepted` point is
  cited by a `## Changes` line. Without a prior review, `## Changes` is absent.
- IDs increase from `Next point ID`, stay while open/reframed, and are never repeated, reused,
  renumbered, or retired while current/maintained.
- Plan v2+ has one row per preceding review point, no older-only/duplicates. Decision is exactly
  `accepted`, `partly accepted`, `pushed back`, or `closed by user`; only retired IDs use the last,
  and every retired ID in that review must use it.
- `pushed back` and `partly accepted` include `Evidence: `. Every pushback has exactly one
  assessment. Accept lines match `^P[0-9]{3,}: accept rebuttal$`; maintain lines match
  `^P[0-9]{3,}: maintain, because .+: .+$`. Accept closes it; maintain keeps it open and in Points.
  No assessment is allowed for a non-pushback.
- `CHANGES_REQUESTED` has a blocker/major. `APPROVE_WITH_MINORS` has points, all minor, with no
  maintained blocker/major. `NO_FURTHER_COMMENTS` has no points or maintained pushback.
- A Response reason or `## Plan` containing `defer`, `next round`, `next version`, `follow-up`, `TBD`
  or `TODO`, or a `## Plan` that points to an earlier version instead of restating it, fails format.
- Enforce caps (default plan 250/review 150), one ≤600-character line per point/response reason,
  one ≤300-character line per `## Changes` entry.

Subagent format failure gets one `recovery.md` retry, then stops. Correct self-planner before commit.
