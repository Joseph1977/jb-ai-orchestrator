# Validation matrix

Read only while maintaining or explicitly validating the skill.

## Loop and format

- Default limit is 4; resume preserves an existing limit unless explicitly changed.
- Final-iteration reviewer still reports every blocker/major.
- Uncaused late blocker/major, invalid severity rise, bad verdict, missing Evidence, duplicate IDs,
  bad pushback assessment, and line/length cap violations fail format validation.
- Minors close adds exactly its one permitted prompt line and handles pushed-back minors.

## Context and models

- `SKILL.md` is at most 30,000 UTF-8 bytes; exact rendered planner+reviewer prompts total at most
  7,500 bytes. Contract tests measure shipped content and print actual values on failure.
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
- Ignore checks cover all four task files and honor `.gitignore`, `.git/info/exclude`, global
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
  Cursor behavior manually.
- Guards cover guarded subagent windows; commit checks cover declared transaction invariants; skill
  digest covers declared checkpoints. No host-wide, arbitrary-hook-worktree, or cross-session claim
  is made.
