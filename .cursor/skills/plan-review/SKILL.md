---
name: plan-review
description: >-
  Explicit-command planning loop in which a planner and a reviewer from different model families iterate
  through committed Markdown under workspace/outcomes/. The orchestrator owns git and user
  communication; the designated planner owns plan.md and a reviewer subagent owns review.md.
  Invoke only with /plan-review.
disable-model-invocation: true
---

# Plan review loop (planner ⇄ reviewer)

You are the **orchestrator**. A planner writes `plan.md`; a reviewer from a different model family writes
`review.md`. They communicate only through task-folder files. You own git, `brief.md`, `status.md`,
`memory.md`, and all user communication. On-demand detail is in `references/`. Obey the triggers below.

## Invocation

```
/plan-review <what we want to do> [--max-iterations N] [--planner-model <slug|self>] [--reviewer-model <slug>] [--audit-reviewer-model <slug>]
/plan-review resume <task folder>  [--max-iterations N] [--planner-model <slug|self>] [--reviewer-model <slug>] [--audit-reviewer-model <slug>]
```

| Setting | Default |
|---|---|
| `--max-iterations` | `4` |
| models | require explicit available planner (`self` allowed) and different-family reviewer |
| `--audit-reviewer-model` | off; when given, passes the same reviewer checks and runs only `audit`-scope reviews |

A new run without `--max-iterations` records `Iteration: 1 of 4`. Existing runs keep the limit in `status.md`.

### Model checks (before step 1, and on resume)

Use only Cursor's available slugs; never substitute/map/"latest", use `inherit`, or accept an
unrecognized family. Require explicit current-chat selection/confirmation for planner and reviewer
on new/resumed runs; recorded values are audit data.

Subagent planning is default: require an explicit slug. Use a new planner subagent for every version;
never resume. Self mode applies only when the user explicitly selects `--planner-model self`
and system identity gives recognized family plus concrete version (not Auto/unknown/bare family);
user claims cannot establish it. Warn that self retains round context.

Planner/reviewer families must differ: `claude-*`, `gpt-*`, `grok-*`/`cursor-grok-*`, or
`composer-*` (or matching named system family). Record exact writer identity in each file and
mode/models/model changes in status. An audit reviewer model passes the same reviewer checks; status
records `Reviewer model: <slug> (audit: <slug>)`, and an `audit` review's `Model:` is that slug.

Never infer model selection or implementation approval.

Frontmatter restricts invocation to explicit `/plan-review`. Unless stricter policy applies, it
authorizes missing workspace parents plus this task folder and local task commits only—not push,
branch change, implementation, or other writes. `AGENTS.md`/`ways-of-working.md` are optional.

## Runtime bootstrap and immutable safety envelope

Resolve `SKILL_ROOT` from the canonical path of the `SKILL.md` loaded in this chat. A symlinked install
is valid; canonical `SKILL.md` must be regular and canonical `scripts/` a real directory. Keep the
path session-only. Run every skill script as `python3 -I -B "$SKILL_ROOT/scripts/<name>.py"`.

Immediately compute `SKILL_BASELINE` with the exact snippet below. Before every skill script, reference
read, subagent launch/return, and `git commit`, recompute it. A mismatch uses “Skill-integrity
recovery” below. The check proves only that disk content did not change after capture; it cannot prove
that already-loaded instructions matched disk.

<!-- BEGIN SKILL_TREE_DIGEST_PY -->
```python
import fnmatch, hashlib, os, stat, sys
from pathlib import Path

def excluded(name):
    return (name == ".DS_Store" or name.endswith("~")
            or fnmatch.fnmatch(name, ".*.sw[pon]") or name.startswith(".#")
            or (name.startswith("#") and name.endswith("#")))

def skill_digest(value):
    root = Path(value).resolve(strict=True)
    digest = hashlib.sha256()
    def add(kind, relative, mode, payload=b""):
        for part in (kind.encode(), relative.encode(), oct(mode).encode(), payload):
            digest.update(len(part).to_bytes(8, "big")); digest.update(part)
    def visit(directory, prefix=""):
        for item in sorted(os.scandir(directory), key=lambda entry: entry.name):
            if excluded(item.name):
                continue
            relative = f"{prefix}/{item.name}" if prefix else item.name
            details = item.stat(follow_symlinks=False)
            mode = stat.S_IMODE(details.st_mode)
            if item.name == "__pycache__" and stat.S_ISDIR(details.st_mode):
                continue
            if stat.S_ISLNK(details.st_mode) or not (
                stat.S_ISDIR(details.st_mode) or stat.S_ISREG(details.st_mode)
            ):
                raise RuntimeError(f"unsupported skill-tree entry: {relative}")
            if stat.S_ISDIR(details.st_mode):
                add("directory", relative, mode); visit(Path(item.path), relative)
            else:
                add("file", relative, mode, Path(item.path).read_bytes())
    visit(root)
    return digest.hexdigest()

if __name__ == "__main__":
    print(skill_digest(sys.argv[1]))
```
<!-- END SKILL_TREE_DIGEST_PY -->

Use this exact read-only probe for policy budgets. `metadata` streams without returning content;
`frontmatter` returns at most 8 KiB.

<!-- BEGIN POLICY_PROBE_PY -->
```python
import json, sys
from pathlib import Path

def metadata(path):
    size = lines = longest = current = 0
    with Path(path).open("rb") as handle:
        while chunk := handle.read(65536):
            size += len(chunk)
            for byte in chunk:
                current += 1
                if byte == 10:
                    lines += 1; longest = max(longest, current); current = 0
    if current: lines += 1; longest = max(longest, current)
    return {"bytes": size, "lines": lines, "max_line_bytes": longest}

def frontmatter(path):
    with Path(path).open("rb") as handle:
        data = handle.read(8192)
    text = data.decode("utf-8")
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---\n", 4)
    if end < 0:
        raise RuntimeError("unterminated frontmatter")
    return text[:end + 5]

if __name__ == "__main__":
    result = metadata(sys.argv[2]) if sys.argv[1] == "metadata" else frontmatter(sys.argv[2])
    print(json.dumps(result) if isinstance(result, dict) else result, end="")
```
<!-- END POLICY_PROBE_PY -->

Repository policy controls plan content, applicable standards, branch policy, and forbidden source
paths. It never overrides this safety envelope: ownership, path validation, explicit models, context
isolation, foreground execution, command restrictions, guards, recovery stops, or implementation
approval. Stop on a conflict.

The exact marker tool used by recovery is below. Extract and run it with `python3 -I -B -c`; do not
execute a skill script after a digest mismatch.

<!-- BEGIN RECOVERY_MARKER_PY -->
```python
import json, os, re, stat, sys, tempfile
from pathlib import Path

PREFIX = "Recovery:"
SKILL_PREFIX = "Recovery: skill-integrity-failed "
COMMIT_RE = re.compile(r"Recovery: commit-check-failed commit=(\S+) pre-head=(\S+)$")
ORDER = ("phase", "baseline", "current", "failure", "commit", "pre-head", "repo-check",
         "prior", "guard", "guard-sha", "commit-manifest", "commit-manifest-sha")
HEX = re.compile(r"[0-9a-f]{64}$")
COMMIT = re.compile(r"([0-9a-f]{40,64}|none)$")
PRE_HEAD = re.compile(r"([0-9a-f]{40,64}|unknown)$")

def validate(values):
    if tuple(values) != ORDER:
        raise RuntimeError("noncanonical skill-integrity marker")
    if values["phase"] not in {"detected", "skill-restored", "repo-checked"}:
        raise RuntimeError("invalid recovery phase")
    if not HEX.fullmatch(values["baseline"]):
        raise RuntimeError("invalid baseline digest")
    if values["current"] != "unavailable" and not HEX.fullmatch(values["current"]):
        raise RuntimeError("invalid current digest")
    if values["failure"] not in {
        "none", "symlink", "special", "unreadable", "traversal", "digest",
        "pre-capture", "planted-marker"
    }:
        raise RuntimeError("invalid failure")
    if values["current"] == "unavailable" and values["failure"] == "none":
        raise RuntimeError("missing digest failure")
    if not COMMIT.fullmatch(values["commit"]) or not PRE_HEAD.fullmatch(values["pre-head"]):
        raise RuntimeError("invalid commit state")
    if values["repo-check"] not in {"not-applicable", "pending", "passed", "failed"}:
        raise RuntimeError("invalid repository-check state")
    if values["prior"] not in {"none", "commit-check-failed"}:
        raise RuntimeError("invalid prior state")
    for path_key, digest_key in (("guard", "guard-sha"),
                                 ("commit-manifest", "commit-manifest-sha")):
        if (values[path_key] == "none") != (values[digest_key] == "none"):
            raise RuntimeError("manifest path/digest mismatch")
        if values[digest_key] != "none" and not HEX.fullmatch(values[digest_key]):
            raise RuntimeError("invalid manifest digest")
    guarded = values["guard"] != "none"
    terminal_record = (
        values["phase"] == "detected"
        and values["failure"] in {"pre-capture", "planted-marker"}
        and values["repo-check"] == "failed"
    )
    if values["failure"] in {"pre-capture", "planted-marker"} and not terminal_record:
        raise RuntimeError("terminal recovery failure cannot advance")
    if not guarded and values["phase"] != "detected":
        raise RuntimeError("unguarded recovery cannot persist an advanced phase")
    expected = (
        {"failed"} if terminal_record
        else {"pending"} if guarded and values["phase"] == "detected"
        else {"pending", "failed"} if guarded and values["phase"] == "skill-restored"
        else {"passed"} if guarded
        else {"not-applicable"}
    )
    if values["repo-check"] not in expected:
        raise RuntimeError("impossible guard/repository-check state")

def parse_skill(line):
    if not line.startswith(SKILL_PREFIX):
        raise RuntimeError("not a skill-integrity marker")
    values = {}
    for token in line[len(SKILL_PREFIX):].split():
        key, separator, value = token.partition("=")
        if not separator or key in values:
            raise RuntimeError("malformed skill-integrity marker")
        values[key] = value
    validate(values)
    return values

def format_skill(values):
    if any(not value or any(ch.isspace() for ch in value) for value in values.values()):
        raise RuntimeError("invalid skill-integrity fields")
    validate(values)
    return SKILL_PREFIX + " ".join(f"{key}={values[key]}" for key in ORDER)

def atomic_lines(path, lines, mode):
    fd, temporary = tempfile.mkstemp(prefix=".plan-review-status.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.writelines(lines); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(mode)); os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def validate_task_state(values, lines):
    states = [line[len("State: "):].strip() for line in lines if line.startswith("State: ")]
    if len(states) != 1:
        raise RuntimeError("status.md must contain exactly one State line")
    terminal_record = (
        values["failure"] in {"pre-capture", "planted-marker"}
        and values["repo-check"] == "failed"
    )
    if (
        states[0] in {"planner-running", "reviewer-running"}
        and values["guard"] == "none"
        and not terminal_record
    ):
        raise RuntimeError("running subagent recovery requires authenticated guard evidence")

def update_marker(status_value, operation, changes):
    path = Path(status_value)
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode):
        raise RuntimeError("status.md is not a regular file")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    indexes = [index for index, line in enumerate(lines) if line.startswith(PREFIX)]
    if len(indexes) > 1:
        raise RuntimeError("multiple recovery markers")
    existing = lines[indexes[0]].rstrip("\r\n") if indexes else None
    newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"

    if operation == "record":
        values = dict(changes)
        planted = existing and existing.startswith(SKILL_PREFIX)
        if planted:
            values["phase"] = "detected"
            values["failure"] = "planted-marker"
            values["repo-check"] = "failed"
            values["prior"] = "none"
        elif existing:
            matched = COMMIT_RE.fullmatch(existing)
            if not matched:
                raise RuntimeError("unknown recovery marker")
            values["commit"], values["pre-head"] = matched.groups()
            values["prior"] = "commit-check-failed"
        validate_task_state(values, lines)
        marker = format_skill(values) + newline
        if indexes: lines[indexes[0]] = marker
        else:
            if lines and not lines[-1].endswith(("\n", "\r")): lines[-1] += newline
            lines.append(marker)
    elif operation == "advance":
        if existing is None:
            raise RuntimeError("missing skill-integrity marker")
        values = parse_skill(existing)
        if values["repo-check"] == "failed":
            raise RuntimeError("repository-check failure is terminal")
        target = changes.get("phase", values["phase"])
        allowed = {("detected", "skill-restored"), ("skill-restored", "repo-checked")}
        if target != values["phase"] and (values["phase"], target) not in allowed:
            raise RuntimeError("invalid recovery phase transition")
        values.update(changes)
        validate_task_state(values, lines)
        lines[indexes[0]] = format_skill(values) + newline
    elif operation == "finish":
        if existing is None:
            raise RuntimeError("missing skill-integrity marker")
        values = parse_skill(existing)
        validate_task_state(values, lines)
        guarded_finish = values["phase"] == "repo-checked" and values["repo-check"] == "passed"
        unguarded_finish = (
            values["phase"] == "detected"
            and values["repo-check"] == "not-applicable"
            and values["guard"] == "none"
        )
        if not (guarded_finish or unguarded_finish):
            raise RuntimeError("repository check is incomplete")
        if values["commit"] != "none":
            raise RuntimeError("unverified commit is terminal")
        if values["prior"] == "commit-check-failed":
            lines[indexes[0]] = (
                f"Recovery: commit-check-failed commit={values['commit']} "
                f"pre-head={values['pre-head']}{newline}"
            )
        else:
            del lines[indexes[0]]
    else:
        raise RuntimeError("unknown marker operation")
    atomic_lines(path, lines, details.st_mode)

if __name__ == "__main__":
    update_marker(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]))
```
<!-- END RECOVERY_MARKER_PY -->

### Branch preflight (before any write)

Resolve canonical repo root. Read applicable branch policy; use `ways-of-working.md` when present.
Otherwise protect existing `main`, `master`, and `origin/HEAD`. A protected branch requires explicit
approval to create `chore/plan-review-<slug>`; any other branch requires explicit approval to use
it. `/plan-review` never authorizes a branch change. Finish models and branch before any path write.

## Task folder

`workspace/outcomes/task-<slug>_<YYYYMMDD-HHmmssZ>-<4hex>/`

Name regex: `^task-[a-z0-9]+(-[a-z0-9]+){1,4}_[0-9]{8}-[0-9]{6}Z-[0-9a-f]{4}$`; use
2–5 slug words, UTC timestamp, and random hex. Regenerate collisions.

### Task folder path validation

Use `lstat`, never follow symlinks. Before creating anything, run digest-gated
`preflight.py check-ignore --repo "$ROOT" --task "workspace/outcomes/<name>"`; exit 1 reports ignored
task files and stops, exit 2 hard-stops, and `git add -f` is forbidden. Existing parents must be
real directories. Create missing parents (the sole authorized outside-task writes) with plain
`mkdir`, recheck, then create the nonexistent task child with plain `mkdir`.

Resume accepts only the matching repo-relative path or exact absolute equivalent, without `.`/`..`.
All parents/task must already be real directories and canonical task parent must be canonical
`workspace/outcomes`; never create on resume.

Files are fixed: orchestrator owns brief/status/memory, planner owns plan, reviewer owns review. Operations
gate: digest-check/read `operations.md` once per chat before new task-folder creation (resume: before
first write). At each commit/output validation digest-check; reread only after context loss.

Policy lookup occurs before searching or reading beneath a candidate path: inspect ancestor `AGENTS.md` and
`.cursor/rules/**/*.mdc` frontmatter; select matching globs and `alwaysApply: true`, follow routing,
and read selected policy/standards fully within the prompt budgets (including 16 KiB per line).

## Commits

Use the loaded `operations.md` transaction/paths: set `status.md` for the step first, then
`pre --subject "plan-review(<slug>): <step> v<N>"` and commit that exact subject. Commit each
completed step, including `plan-start`/`review-start`; inspect the scoped diff and leave unrelated
staging untouched.
Never amend/reset/revert/bypass hooks or commit recovery markers. Stop on `pre`, commit, or `post`
failure. Recheck branch, protection, authorization ancestry, and skill digest before each commit.
Push only on an explicit request; report whether terminal-state commits are local.

## Loop

One iteration is plan vN then review vN; every feedback round counts.

Run `python3 -I -B "$SKILL_ROOT/scripts/memory_build.py" <mode> --repo "$ROOT" --task <task>` as
`operations.md` describes; exit 1 is a format failure of agent output, exit 2 stops.
**Memory gate** (before every `plan-start`/`review-start`): probe `memory.md` with `metadata`; at 200
lines or 16 KiB or more, run `compact` and commit `memory-compact vN` first. A `compact` exit 1 means the
floor is still too large: stop and ask the user to retire or decide points.

1. **Setup:** finish models/path/branch, discover applicable policy, then create brief/status per
   `operations.md`; record branch authorization, `State: planning`, `Outcome: none`, iteration/limit,
   models. Run `memory_build.py init`; commit `brief v1` with `memory.md`.
2. **Plan:** v1 starts; later versions answer every prior point in full. Self mode obeys identical
   budgets and digest checks. Subagent mode commits `planner-running` as `plan-start vN` (a retry
   repeats that exact subject, per `operations.md`), then launches a new guarded foreground Task with
   explicit model, `subagent_type: generalPurpose`, `run_in_background: false`, and no `resume` or
   `inherit`. Validate, including `memory_build.py plan-check` (`operations.md`); commit plan with
   `State: reviewing`.
3. **Review:** run `memory_build.py scope` (`audit` or `converge`); commit `reviewer-running` as
   `review-start vN` (retry: same exact subject); render the reviewer prompt with that `--scope` and
   launch a new guarded foreground Task with reviewer model (the audit reviewer model, if set, for
   `audit`), `subagent_type: generalPurpose`, `run_in_background: false`, and
   no `resume` or `inherit`. Validate, then `memory_build.py update --scope <same>`; record `State: reviewed`,
   counts/trend/next ID from its output, and commit `review vN` with review, status and memory.
4. **Decide** (the limit is checked here, never in a prompt or inside a round):
   1. `NO_FURTHER_COMMENTS` at any N ≤ limit → Consensus; its Outcome open points are 0/0/0. If
      `memory.md` records `Audit: pending` (seeded or re-audit), the review was not an audit: treat
      it as `CHANGES_REQUESTED` (`commit_check.py` refuses that consensus).
   2. Open points remain (`CHANGES_REQUESTED` or `APPROVE_WITH_MINORS`) and N = limit → digest-check/read
      `limit.md`, commit `limit-reached vN`; only an explicit user K continues.
   3. `APPROVE_WITH_MINORS` and N < limit → Minors close.
   4. `CHANGES_REQUESTED` and N < limit → commit `plan-start vN+1` with `Iteration: N+1 of L`, then plan
      and review as usual.

### Minors close

Only when N < limit: commit `plan-start vN+1` with `Iteration: N+1 of L`, render `prompt_build.py minors`,
validate and commit `plan vN+1` at `reviewing`, then run Review. Consensus follows only a
`NO_FURTHER_COMMENTS` review.

## Repository guard

Before the first guarded capture or subagent launch in this session, read `references/integrity.md`.
Never retry a guard failure or re-baseline. Restore only the owned file. Fail closed on guard exit 2.

This guard wraps planner and reviewer subagents only. A self-planner is the orchestrator; path-limited commits apply.

Immediately before each subagent:

```bash
ROOT="$(git rev-parse --show-toplevel)"
GUARD_FILE="$(mktemp "${TMPDIR:-/tmp}/plan-review-guard.XXXXXX")"
GUARD_SHA="$(python3 -I -B "$SKILL_ROOT/scripts/repo_guard.py" capture \
  --repo "$ROOT" --allow "<repo-relative plan.md or review.md>" --output "$GUARD_FILE")"
```

Stop before launch if capture fails. Keep `GUARD_FILE` and `GUARD_SHA` out of the prompt. After it returns:

```bash
python3 -I -B "$SKILL_ROOT/scripts/repo_guard.py" compare \
  --repo "$ROOT" --allow "<same path>" \
  --baseline "$GUARD_FILE" --baseline-sha256 "$GUARD_SHA"
```

Exit 0 means only the owned path may have changed. Exit 1 prints the deltas. Exit 2 means integrity was not proved. Delete the temporary manifest after a normal compare or guard-failure report; preserve it only for Skill-integrity recovery. Before reading subagent output, `lstat` the owned path and require a regular file.

### Skill-integrity recovery

Mismatch/unreadable/symlink/special/traversal:

1. Stop before the gate; preserve authenticated manifests. Restore only the owned subagent file.
2. Revalidate task/status with `lstat`; use loaded marker `record` with original baseline, current
   digest/failure, commit/pre-head, repo state, prior marker, and manifest evidence. It overwrites a
   pre-existing skill marker as terminal `failure=planted-marker repo-check=failed`; malformed,
   unknown, or multiple markers hard-stop.
3. The recording chat never continues/retries/re-baselines. Never run a changed skill script.

Mismatch after a running-state commit but before capture records sole unguarded running marker
`phase=detected failure=pre-capture repo-check=failed guard=none guard-sha=none`; it is terminal.

On resume, before `resume.md`, validate path and `lstat` every present task file as regular, read
status, then:

- `detected`: a later invocation recomputes digest without a skill script. Nonmatch stops for
  restore/reinstall. Exact match plus guard evidence atomically advances to `skill-restored`.
  Unguarded/not-applicable never advances: terminal commits stop; otherwise `finish` directly.
- `phase=skill-restored`: require baseline and nonfailed repo state. With guard evidence, verify its digest
  and run:

  ```bash
  python3 -I -B "$SKILL_ROOT/scripts/repo_guard.py" compare \
    --repo "$ROOT" --allow "<owned path>" \
    --baseline "$GUARD_FILE" --baseline-sha256 "$GUARD_SHA" \
    --recovery-status "workspace/outcomes/<task>/status.md"
  ```

  Delayed mode compares full worktree plus semantic Git state and normalizes only the exact marker.
  Missing/tampered evidence or nonzero compare atomically records terminal `repo-check=failed`.
  Success fsyncs `phase=repo-checked repo-check=passed`; retain guard evidence.
- `phase=repo-checked`: require baseline and still-authenticated guard; never compare again. Any commit SHA
  is terminal. With `commit=none`, `finish` restores prior commit-check marker or removes this one,
  then delete guard and use explicit retry flow.

Unknown/impossible state, failed repo check, missing evidence, or different skill version is
terminal; `repo-check=failed` is terminal. Marker is never committed. Retain a pre-`post` commit
manifest for diagnosis but never rerun `post`.

## Format and consistency checks

Digest-check; apply every loaded `operations.md` format rule. Reread only per Operations gate.
Self-planner corrects failures before commit; subagent gets one `recovery.md` retry, rendered with
`--format-error`, then stops.

### Prompts

Render each Task prompt only with `python3 -I -B "$SKILL_ROOT/scripts/prompt_build.py" planner|reviewer|minors
--task <abs task folder> --model <header slug> --plan-cap <n> --review-cap <n> [--next-id P###]
[--retired IDs|none] [--pushed-back IDs|none] [--scope audit|converge]` (templates in `references/prompts.md`;
the probe code comes from the marker above). `--scope` is required for reviewer, refused otherwise, and
always equals the `memory_build.py scope` output. Pass stdout verbatim as the Task prompt; never edit, prefix, append, or paste. The
builder exits 2 on any other input. It has no version, iteration, or limit option; agents derive `Version:`
from task files. Self-planner mode follows the same rendered rules.

## Consensus

After no comments, use the current version:

1. Keep plan unchanged; set `awaiting-user` and the consensus `Outcome`, commit `consensus`, and report
   local-only status.
2. Give iteration count, short plan summary, pushed-back IDs the reviewer accepted, and the
   `memory.md` Observations; require explicit **go**.
3. On go in this chat, set `approved`, commit `approved`, then implement only the plan. After `approved`,
   the session ends; implementing changes to this skill runs outside the loop.
4. Otherwise put dated feedback only in brief, set `planning`, run `memory_build.py feedback`
   (`operations.md`), commit `feedback`, continue after the latest plan, and raise the limit in
   `status.md` only (never in brief) to at least that version.

## Triggers

Digest-check before each reference. Read `integrity.md` before first guard/launch; `resume.md` only
after inline resume integrity (a task without `memory.md` is seeded there before any launch); `recovery.md` on nonzero guard/commit/post/format only with matching
skill; `limit.md` at limit-reached; `operations.md` at its triggers. Skill mismatch always uses inline flow.

## Rules

- Prompts are exactly `prompt_build.py` output; the orchestrator adds no text, including in a last or
  retried round. Agents communicate only through task files. Do not append or paste file contents,
  diffs, Git output, replies, chat history, guard data, or other context.
- Search first and read targeted source; never load a whole large source/generated file/log/binary
  extraction/document when narrower evidence works. Read applicable policy/standards fully in budget.
- Only this chat commits/writes brief/status/memory (memory only through `memory_build.py`); planner
  writes plan; reviewer writes review. In
  subagent mode orchestrator never edits plan. Approval exists only in status.
- Teammate resume requires user-authorized push, upstream, and pushed task commits.
- Obey repository forbidden paths. Chat update: one line per iteration.

## Validation

Automated suites cover preflight, guard, commit checks, memory building, inline snippets, and static
skill contracts.
Actual Cursor orchestration and model behavior remain manual checks in `references/validation.md`.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -I -B -m unittest discover -s "$SKILL_ROOT/scripts"
```

Verify the skill digest before reading the validation reference.
