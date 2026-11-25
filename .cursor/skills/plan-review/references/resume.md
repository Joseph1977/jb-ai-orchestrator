# Resume

Read this file only after `SKILL.md` has completed inline resume path validation, task-file `lstat`
checks, skill-integrity marker dispatch, and establishment of a trusted current skill baseline.
Then follow `SKILL.md` for model checks, branch policy, commits, and format checks.

A skill-integrity marker with `commit` set to a SHA or `repo-check=failed` is terminal and never
reaches this reference or an ordinary recovery choice. A nonterminal skill marker must be completed
or converted/removed by inline recovery first. This reference handles only ordinary commit-check
markers.

`/plan-review resume <task folder> [--max-iterations N]`:

1. Run “Task folder path validation” for a resume and the full branch matrix from “Branch preflight” before writing. On a branch protected by the applicable repository policy or fallback, require the separate branch decision and move first. Never commit there. Do not require a globally clean repo.
2. Read current `status.md`, `brief.md`, and every other task file that exists. Use
   `git log --format=fuller --name-status -- <task folder>`; never load historical patches. Git
   history is authoritative for commits. An absent step file is valid only when status permits it.
3. Validate every present file's exact headers, the recorded state transition, the version relationships, and `Next action`. Stop on inconsistency. Do not infer completion from matching version numbers alone. Before any write, run:

   ```bash
   python3 -I -B "$SKILL_ROOT/scripts/commit_check.py" history \
     --repo "$ROOT" --task workspace/outcomes/<task folder>
   ```

   `history` re-validates every commit that touched the task folder. It checks the `plan-review(<slug>): <step> v<N>` subject and slug (retries reuse the exact subject), first commit `brief v1`, a single parent, changed paths within that step's allowed files (required files changed), only the four task files as `100644` blobs, persistent `brief.md` and `status.md` in every resulting tree, the `State` each step must set and the state it may follow, and that every subject version equals that commit's `Iteration` and does not exceed its limit. It rejects a committed `Recovery:` line. This covers a crash between the commit and `post`. It also reports the worktree recovery marker. `pre --subject` applies the same checks to the staged step before any commit.

   Format: a task whose `brief v1` status has an `Outcome:` line uses the current rules; `history` reports `"format": "current"`. They add: `Outcome` matching each step and its Iteration and Open points; `consensus` only after a `NO_FURTHER_COMMENTS` review; `limit-reached` only after a review with open points at its limit, with Iteration and Open points copied from that review; no `plan-start`/`plan` after a review at the limit unless `feedback` raised it; the limit changes only in `feedback`; and `minors` rejected. A task without `Outcome:` (older run) reports `"format": "legacy"` and the note `legacy status format: honest-limit checks not applied`; only the existing rules apply.

   Version sequencing: before the first plan, N is 1. `plan-start`, `plan`, and (legacy format) `minors` open the same way. Let `latest_plan` and `latest_review` be the latest committed `Version` of those files (0 when absent) and `closed_plan` the latest `minors` version (0 when none). The next `plan-start` or `plan` expects `latest_plan` when `latest_plan > max(latest_review, closed_plan)`, otherwise `max(latest_review, closed_plan) + 1`. `minors` uses that same opening rule, checks `plan.md` `Version`, and records `closed_plan` as its subject version. Every other step keeps the open subject version.

   Legacy format only: `minors` is allowed only after `reviewed` or `planner-running`. It requires the latest committed review at version N with `Verdict: APPROVE_WITH_MINORS`, and it commits `plan.md` plus `status.md` at `State: awaiting-user` for version N+1. Until that `minors` commit exists, `consensus` and `limit-reached` are rejected, and `plan` is rejected unless a `feedback` commit follows that review. `plan-start vN+1` remains allowed so a subagent Minors close keeps its failure marker. In the current format, an `APPROVE_WITH_MINORS` review below the limit continues with `plan-start vN+1` (Minors close), is reviewed like any plan, and `minors` is rejected.

   Exit 0 means history is valid and no marker is present. Exit 1 (ordinary history failure or a
   commit-check marker) or exit 2 requires stopping. Report failures and the commit SHA. Require an
   explicit user recovery choice before continuing. Never amend, reset, revert, or clear the marker
   automatically. Recovery steps are in `references/recovery.md`.

4. If the task folder is dirty, use
   `git status --porcelain --untracked-files=all -- <task folder>` plus
   `git diff HEAD -- <task folder>` to classify staged, unstaged, and untracked partial state. Never
   absorb or discard it automatically. Ask the user to choose: adopt and commit only a complete
   valid next step; discard both index and worktree deltas for the task folder only; or leave it
   untouched and stop. State exact files and validation. Destructive recovery needs explicit choice.
5. Re-run the Model checks in `SKILL.md`:
   - In self-planner mode, the current system identity must equal the recorded Planner model exactly (same family and concrete version). If it is `Auto`, `unknown`, a bare display name, or different, leave self-planner mode and require explicit selection of both slugs. Record the mode and model change before the next plan version.
   - Reconfirm every planner or reviewer subagent slug in this chat before invoking it. Recorded values are recommendations only.
   - Re-run the different-family check and record every change.
6. Handle the committed state explicitly:
   - `planning`: start the next plan step.
   - `reviewing`: the plan is committed and validated. Start the review step.
   - `reviewed`: the review is committed and validated. Run Decide. Do not start another subagent. `APPROVE_WITH_MINORS` goes to Minors close when N < limit, else Limit reached.
   - `planner-running` / `reviewer-running`: the prior subagent was interrupted or guard-aborted.
     Confirm the owned file is restored/absent. Report outside deltas only when guard comparison
     actually ran; a skill-integrity marker with pending/failed repository checking makes no such
     claim. Require explicit approval before a fresh guarded retry.
   - `limit-reached`: read `references/limit.md`. Require explicit approval for K more iterations or stop.
   - `stopped` with reason `error`: require explicit approval to retry the failed step.
   - `stopped` with reason `user`: require explicit approval to reopen and a new iteration limit.
   - `awaiting-user`: present the final plan and require a fresh implementation **go**.
   - `approved`: report the prior approval, and require a fresh **go** before implementation in this chat. Another chat's approval is not current authorization.
7. A new `--max-iterations` replaces the limit and is recorded. With no flag, keep the limit already in `status.md` (a resumed `of 10` stays 10). Never continue with N > limit. Minors close runs only when N < limit.

Reviewer mode always starts fresh. Planner mode defaults to a fresh explicit subagent; self mode
requires a new explicit `--planner-model self` selection and exact concrete identity match.

All recovery, model, and implementation questions use the repository's plain-chat question format when one exists. Otherwise use a plain numbered list with concrete consequences and one recommendation. Model choice and implementation go are blocking. Never proceed on silence.
