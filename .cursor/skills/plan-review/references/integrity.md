# Integrity — repo guard, skill digest, and commit check

Read this before the first guarded capture or subagent launch in a session. The commands themselves stay in `SKILL.md`.

## Observable boundary

Do not claim an unobservable sandbox guarantee. The repository guard detects repository and Git
changes during guarded subagent windows. The whole-skill digest detects changes at its declared
checkpoints. Commit checks enforce only their declared transaction invariants. None monitors
arbitrary host paths or arbitrary orchestrator activity between checkpoints.

## What `repo_guard.py` checks

`capture` writes a manifest outside the repo and prints its sha256. `compare` re-captures and diffs that manifest. The baseline path and digest stay out of the subagent prompt.

The manifest hashes every worktree entry outside `.git` except the one owned path (`plan.md` for a planner, `review.md` for a reviewer). That includes ignored files and directories, ordinary untracked files, symlinks, and tracked files marked skip-worktree or assume-unchanged. It records file bytes, symlink targets, directory entries, and special-file metadata. It does not follow symlinks.

Normal compare separately hashes the complete Git directory, common Git directory, index
(`git ls-files --stage`) and flags (`git ls-files -v`), refs, effective configuration, external
`core.hooksPath`, branch, and `HEAD`. A linked-worktree `.git` marker is included.

Delayed recovery compare intentionally ignores raw `.git` bytes and index stat-cache churn. It
compares semantic branch, `HEAD`, refs, staged entries, flags, effective config, and hooks, while
retaining the complete worktree comparison. It normalizes only one exact canonical
skill-integrity marker in `status.md`; every other status change remains detectable.

Existing dirty files are valid when they are unchanged. The owned path is the only path allowed to change.

`compare` exit 0 means the Git snapshot and every non-owned path match. Exit 1 prints branch, HEAD, index, and path deltas (`added`, `removed`, `changed`, and Git control deltas). Exit 2 means the guard could not prove integrity (unreadable path, manifest mismatch, unsupported manifest version, or a capture error). Fail closed on exit 2.

The guard covers the repository and the Git-controlled paths that can affect the next commit. It is not a host-wide intrusion monitor.

This guard wraps planner and reviewer subagents only. A valid self-planner is the orchestrator, not an untrusted subagent. Its file ownership and the path-limited commit checks below apply instead. Do not take a guard baseline around a self-planner write.

Before reading subagent output, `lstat` the owned path and require a regular file. A missing, truncated, symlinked, or special owned file fails the format checks and is restored as `references/recovery.md` describes.

## What `pre` and `post` check

`pre` records the pre-commit `HEAD` and branch. Every allowed path must be staged, must be a regular file, and must have identical worktree and staged mode and content (what `git commit --only` will take). Worktree content is hashed with `git hash-object --path=<path>`, so `.gitattributes` clean filters and line-ending rules apply. `pre` records each inspected mode and blob and every unrelated index entry. At least one allowed path must differ from `HEAD`. If `pre` fails, do not commit.

`post` requires exactly one new commit whose only parent is the recorded `HEAD`, on the same branch, changing exactly the allowed paths that differed from that `HEAD`. Committed, index, and worktree modes and blobs must equal the inspected ones. Unrelated index entries must be unchanged. This detects a hook that rewrites an allowed file or changes its mode, whether or not it stages the change.

Git hooks run. Never pass `--no-verify`. A nonzero commit or `post` is a stop, not a retry. The recovery marker and restore rules are in `references/recovery.md`.

## Limits

- One owned path per capture. Capture again for a different path; never reuse a manifest across runs.
- `memory.md` is orchestrator-owned and outside every guard allowlist: a subagent change to it is an outside delta.
- Do not edit the manifest. `compare` rejects a sha256 mismatch.
- Delete the temporary manifest after normal handling. Preserve an authenticated manifest only for
  inline skill-integrity recovery, then clean it according to the recovery phase.
- A dirty tree outside the owned path is acceptable only when those paths still match the baseline byte for byte.
- The check is local to this repository. It does not watch the home directory, other clones, or editor state outside the worktree and Git control files.
