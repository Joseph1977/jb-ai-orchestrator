# Validation matrix

Read only while maintaining or explicitly validating the skill.

## Loop and format

- Default limit is 4; resume preserves an existing limit unless explicitly changed.
- Bad verdict, missing Evidence, duplicate IDs, bad pushback assessment, deferral wording or a plan
  that points to an earlier version, and line/length cap violations fail format validation.
- Unanchored `## Plan` lines, a `## Changes` list that differs from the anchors actually changed,
  unanchored review points and incomplete `## Coverage` fail format validation.
- `[late]` below blocker or without `Missed:`, a new converge point on an anchor not listed in
  `## Changes`, Observations in an audit, and converge coverage that differs from the listed anchors
  fail `memory_build.py update`.
- M1 seeded missed major: plan v2 keeps a major defect audit review v1 missed on an unchanged anchor; converge review v2 records it under Observations, or as `[blocker] [late]` with `Missed:` only if it is a blocker.
- M2 whole-plan audit: audit review v1 raises seeded issues in every section, mapped through `## Coverage`; a feedback that changes the brief makes the next review an audit again.
- M3 prompt integrity: each Task prompt equals `prompt_build.py` output (sha256 match); no added text in any round.
- M4 blindness: rendered prompts disclose no budget; brief.md has no `Iteration:` line and does not state this run's limit or "N of L"; plan/review may discuss limits as subject matter; agents never open `status.md`; derived Versions pass format checks in v1, v2 and a Minors close.
- M5 limit: `--max-iterations 1` with a seeded major ends `limit-reached v1`, open points unchanged, no `plan-start v2`, plain-text question asked; granting K=1 commits `feedback v1` changing only `status.md`, and `plan-start v2` follows.
- M6 early consensus: a trivial brief with limit 6 reaches consensus before 6; `Outcome` reads "consensus at iteration N of 6, open points 0/0/0".
- M7 legacy: `history` on older folders without `Outcome:` reports `format: legacy` and only pre-existing failure messages.
- M8 retry: a guard-failed planner retry commits `plan-start vN` again with attempt 2 and passes `pre`/`history`.
- M9 Minors close: a seeded minor at N < L yields `plan vN+1` then `review vN+1`; a pushed-back minor is assessed there; consensus only at 0/0/0, otherwise the gate continues.
- M10 seeded gaps: a fresh run whose plan v1 carries 5 known gaps on distinct anchors (a constraint with no decision, a wrong file path, an In item with no test, contradictory slice entry criteria, an ungated assumption); audit review v1 reports all 5, each mapped through `## Coverage`.
- M11 convergence: plan v2 fixes the 5 gaps and changes only those anchors; review v2 raises no new non-late point on unchanged anchors; consensus within 3 rounds.
- M12 late routing: in M11, a non-blocker found on an unchanged anchor goes to Observations, appears in `memory.md`, and is listed in the consensus report.
- M13 evidence replay: a fresh task with the `opencode-engine-replan` brief and its plan v1; record the share of the P001–P025 issue themes audit review v1 raises (target at least 60%) and rounds to consensus (target at most 4).
- M14 resume: a scratch-branch copy of that task resumes with `memory-seed v16`, `plan-start v17` and an audit review v17; `history` reports `memory: seeded` and no failures.
- M15 compaction: a large audit fixture pushes memory over the soft limit; `memory-compact` lands before the next `plan-start` with the kept sections unchanged.

## Context and models

- `SKILL.md` is at most 30,000 UTF-8 bytes; exact rendered planner+reviewer prompts total at most
  10,500 bytes. Contract tests measure shipped content and print actual values on failure.
- Trigger-only task schemas, commit mechanics, and format detail live in digest-gated
  `operations.md`; trusted mismatch bootstrap remains inline.
- Planner defaults to a fresh explicit model; reviewer is always fresh and a different family.
- Both use `generalPurpose`, foreground execution, no resume, and no inherit.
- Explicit self mode warns about retained context and uses the same policy/file budgets.
- Task prompts contain paths/scalars only, no pasted bodies, diffs, history, replies, or guard data.
- Subagents return exactly one line.

## Policy and reads

- Repository policy shapes plan content but cannot override the safety envelope; a conflicting
  request to test, build, use Git, commit, implement, background, or widen writes stops.
- Root/nested `AGENTS.md`, root/nested `.cursor/rules/**/*.mdc`, glob rules, and nested
  `alwaysApply: true` rules are discovered before reading beneath a candidate path.
- A v2 plan introducing another project routes and reads that project's complete standards.
- User-level rules without repository paths never enter `brief.md`.
- Policy budgets are per-agent invocation. Fixed streaming probes enforce candidate/frontmatter,
  file, byte, line, and maximum-line ceilings before body reads.
- Source discovery remains targeted but checks callers, consumers, tests, migrations, contracts,
  generated types, configuration, deployment, and docs.

## Portability and task paths

- Project-level, direct user-level, and symlinked user-level installs resolve canonical skill root.
- Existing `main`, `master`, and remote default are protected when no repository policy exists.
- Ignore checks cover all five task files and honor `.gitignore`, `.git/info/exclude`, global
  excludes, ignored-parent negation behavior, and fail closed on Git errors.
- Resume rejects symlinked/special task files and sees staged, unstaged, and untracked task deltas.
- Explicit discard restores index and worktree for the task folder only.

## Skill and repository integrity

- All script calls use `python3 -I -B`; runtime scripts import no local skill module.
- Whole-skill digest covers instructions, references, scripts, modes, and bytes; rejects child
  symlinks/special files; excludes only named benign artifacts.
- Digest is checked before references, scripts, subagent boundaries, and commits.
- Initial record overwrites a pre-existing skill marker with terminal
  `failure=planted-marker repo-check=failed`; it never trusts that marker's phase or guard claims.
  Only resume phase operations accept a marker recorded by the orchestrator.
- `failure=planted-marker` and `failure=pre-capture` are valid only at
  `phase=detected repo-check=failed`; parsers reject every advanced/nonterminal form.
- A mismatch after committing a running state but before guard capture records terminal
  `failure=pre-capture repo-check=failed` instead of leaving an unmarked running state.
- The recording chat refuses continue. A later `phase=detected` invocation advances immediately on
  exact baseline match when authenticated guard evidence exists, directly finishes an unguarded
  nonterminal marker without persisting an advanced phase, or stops without a skill script on
  mismatch.
- Recovery is crash-safe across `detected`, `skill-restored`, and `repo-checked`.
- Preserved guard evidence is digest-verified before delayed comparison.
- Delayed comparison ignores raw `.git` stat-cache churn but detects complete worktree and declared
  semantic Git-state changes. It normalizes only the exact recovery marker.
- Comparison success is persisted, but authenticated guard evidence remains required through marker
  completion; only then is the manifest deleted. `repo-check=failed` and any commit SHA are terminal.
- Any unverified commit remains terminal and never becomes ordinary commit recovery.
- A prior commit-check marker is restored only after successful/not-applicable repository checking.
- Between `pre` and commit, staged allowed files and marker-bearing status remain visible together.

## Honest claims

- Static contract tests validate declared prompt/checkpoint structure; this matrix validates actual
  Cursor behavior manually. Verbatim delivery of rendered prompts to the Task tool and semantic
  severity honesty are manual-only (M1–M3, M10–M15).
- Guards cover guarded subagent windows; commit checks cover declared transaction invariants; skill
  digest covers declared checkpoints. No host-wide, arbitrary-hook-worktree, or cross-session claim
  is made.
