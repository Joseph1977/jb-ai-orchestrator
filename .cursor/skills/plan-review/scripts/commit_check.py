#!/usr/bin/env python3
"""Verify plan-review task commits: one commit at a time (pre/post) and the whole history."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

MANIFEST_VERSION = 3


class CheckError(RuntimeError):
    """Raised when the commit state cannot be proven."""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise CheckError(f"git {' '.join(args)} failed: {message}")
    return result


def _out(repo: Path, *args: str) -> str:
    return _git(repo, *args).stdout.decode().strip()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalize(repo: Path, value: str) -> str:
    candidate = Path(value)
    absolute = Path(os.path.abspath(candidate if candidate.is_absolute() else repo / candidate))
    try:
        relative = absolute.relative_to(repo)
    except ValueError as exc:
        raise CheckError(f"path is outside repository: {value}") from exc
    if relative == Path("."):
        raise CheckError("repository root cannot be an allowed path")
    return relative.as_posix()


def _index_entries(repo: Path) -> dict[str, str]:
    """Map path to '<mode> <blob> <stage>' for every index entry."""
    entries: dict[str, str] = {}
    data = _git(repo, "ls-files", "--stage", "-z").stdout.decode("utf-8", "surrogateescape")
    for record in filter(None, data.split("\0")):
        meta, path = record.split("\t", 1)
        key = path if path not in entries else f"{path}\0{meta}"
        entries[key] = meta
    return entries


def _staged(entries: dict[str, str], path: str) -> list[str] | None:
    """Return [mode, blob] for a stage-0 index entry."""
    meta = entries.get(path)
    if meta is None:
        return None
    mode, blob, stage = meta.split()
    if stage != "0":
        raise CheckError(f"{path} has an unmerged index entry")
    return [mode, blob]


def _tree(repo: Path, commit: str, path: str) -> list[str] | None:
    output = _git(repo, "ls-tree", "-z", commit, "--", path).stdout.decode()
    record = output.split("\0", 1)[0]
    if not record:
        return None
    mode, _kind, blob = record.split("\t", 1)[0].split()
    return [mode, blob]


def _file_mode_tracked(repo: Path) -> bool:
    result = _git(repo, "config", "--type=bool", "--get", "core.fileMode", check=False)
    return result.stdout.decode().strip() != "false"


def _worktree(repo: Path, path: str, index_mode: str | None) -> list[str] | None:
    """Return [mode, blob] as Git would stage the worktree file, applying its attributes."""
    absolute = repo / path
    try:
        details = absolute.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(details.st_mode):
        raise CheckError(f"{path} is not a regular file")
    if _file_mode_tracked(repo):
        mode = "100755" if details.st_mode & 0o111 else "100644"
    else:
        mode = index_mode or "100644"
    return [mode, _out(repo, "hash-object", f"--path={path}", "--", path)]


def _branch(repo: Path) -> str:
    result = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if result.returncode != 0:
        raise CheckError("HEAD is detached")
    return result.stdout.decode().strip()


def pre(repo_value: str, paths: list[str], subject: str | None = None) -> dict[str, Any]:
    """Inspect the staged paths; with a subject, also validate the step it commits.

    The CLI always passes a subject. The Python entry point accepts none only for low-level
    staging checks that carry no task semantics.
    """
    repo = Path(repo_value).resolve(strict=True)
    allowed = sorted({_normalize(repo, value) for value in paths})
    if not allowed:
        raise CheckError("at least one allowed path is required")

    head = _out(repo, "rev-parse", "--verify", "HEAD")
    entries = _index_entries(repo)
    files: dict[str, dict[str, Any]] = {}
    expected_changed: list[str] = []
    for path in allowed:
        staged = _staged(entries, path)
        if staged is None:
            raise CheckError(f"{path} is not staged")
        worktree = _worktree(repo, path, staged[0])
        if _git(repo, "diff", "--quiet", "--", path, check=False).returncode != 0 or worktree != staged:
            raise CheckError(f"{path}: worktree mode/content differs from staged mode/content")
        head_entry = _tree(repo, head, path)
        if head_entry != staged:
            expected_changed.append(path)
        files[path] = {"entry": staged, "head_entry": head_entry}
    if not expected_changed:
        raise CheckError("no allowed path differs from HEAD")

    if subject is not None:
        _validate_staged_step(repo, allowed, expected_changed, subject)

    unrelated = {key: value for key, value in entries.items() if key.split("\0", 1)[0] not in allowed}
    return {
        "version": MANIFEST_VERSION,
        "repo": str(repo),
        "pre_head": head,
        "subject": subject,
        "branch": _branch(repo),
        "files": files,
        "expected_changed": expected_changed,
        "unrelated_index": unrelated,
    }


def post(manifest: dict[str, Any]) -> dict[str, Any]:
    repo = Path(manifest["repo"])
    pre_head = manifest["pre_head"]
    head = _out(repo, "rev-parse", "--verify", "HEAD")
    failures: list[str] = []

    if head == pre_head:
        failures.append("HEAD did not move; no commit was created")
        return {"ok": False, "pre_head": pre_head, "commit": head, "failures": failures}

    parents = _out(repo, "rev-list", "--parents", "-n", "1", head).split()[1:]
    if parents != [pre_head]:
        failures.append(f"commit parents {parents} are not exactly [{pre_head}]")
    try:
        branch = _branch(repo)
    except CheckError as exc:
        branch = str(exc)
    if branch != manifest["branch"]:
        failures.append(f"branch changed from {manifest['branch']} to {branch}")
    expected_subject = manifest.get("subject")
    if expected_subject is not None:
        actual_subject = _out(repo, "log", "-1", "--format=%s", head)
        if actual_subject != expected_subject:
            failures.append(
                f"commit subject {actual_subject!r} differs from inspected {expected_subject!r}"
            )

    changed_output = _git(
        repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", pre_head, head
    ).stdout.decode("utf-8", "surrogateescape")
    changed = sorted(filter(None, changed_output.split("\0")))
    if changed != manifest["expected_changed"]:
        failures.append(
            f"committed paths {changed} differ from expected {manifest['expected_changed']}"
        )

    entries = _index_entries(repo)
    for path, recorded in manifest["files"].items():
        expected = recorded["entry"]
        committed = _tree(repo, head, path)
        if committed != expected:
            failures.append(f"{path}: committed entry {committed} differs from inspected {expected}")
        try:
            staged = _staged(entries, path)
        except CheckError as exc:
            staged = str(exc)
        if staged != expected:
            failures.append(f"{path}: index entry {staged} differs from inspected {expected}")
        try:
            worktree = _worktree(repo, path, expected[0])
        except CheckError as exc:
            worktree = str(exc)
        if worktree != expected:
            failures.append(f"{path}: worktree entry {worktree} differs from inspected {expected}")

    allowed = set(manifest["files"])
    unrelated = {key: value for key, value in entries.items() if key.split("\0", 1)[0] not in allowed}
    if unrelated != manifest["unrelated_index"]:
        before = manifest["unrelated_index"]
        delta = sorted(
            key.split("\0", 1)[0]
            for key in set(before) | set(unrelated)
            if before.get(key) != unrelated.get(key)
        )
        failures.append(f"unrelated index entries changed: {delta}")

    return {"ok": not failures, "pre_head": pre_head, "commit": head, "failures": failures}


TASK_NAME = re.compile(r"^task-([a-z0-9]+(?:-[a-z0-9]+){1,4})_[0-9]{8}-[0-9]{6}Z-[0-9a-f]{4}$")
STEPS = (
    r"brief|feedback|plan-start|review-start|plan|review|minors|consensus|approved|"
    r"limit-reached|stopped|memory-seed|memory-compact"
)
SUBJECT = re.compile(rf"^plan-review\(([a-z0-9-]+)\): ({STEPS}) v([0-9]+)$")
TASK_FILES = {"brief.md", "plan.md", "review.md", "status.md", "memory.md"}
STEP_FILES = {
    "brief": {"brief.md", "status.md", "memory.md"},
    "feedback": {"brief.md", "status.md", "memory.md"},
    "plan": {"plan.md", "status.md"},
    "minors": {"plan.md", "status.md"},
    "review": {"review.md", "status.md", "memory.md"},
    "memory-seed": {"memory.md", "status.md"},
    "memory-compact": {"memory.md", "status.md"},
}
STEP_REQUIRED = {
    "brief": {"brief.md", "status.md"},
    "plan": {"plan.md", "status.md"},
    "minors": {"plan.md", "status.md"},
    "review": {"review.md", "status.md"},
    "memory-seed": {"memory.md"},
    "memory-compact": {"memory.md"},
}
MEMORY_STEPS = {"memory-seed", "memory-compact"}
MEMORY_SECTIONS = (
    "Settled decisions", "Coverage", "Open points", "Observations", "Closed points",
    "User decisions / retired IDs", "Round log",
)
MEMORY_KEPT = (
    "Settled decisions", "Coverage", "Open points", "Observations", "User decisions / retired IDs",
)
MEMORY_SOFT = (200, 16 * 1024)
MEMORY_HARD = (300, 24 * 1024)
MEMORY_LINE_CHARS = 600
MEMORY_AUDIT = re.compile(r"^Audit: (pending|done v[0-9]+|waived at seed v[0-9]+)$")
MEMORY_BLIND = re.compile(r"(?i)\blimit\b|\bremaining\b|\bbudget\b|\bfinal\b|\b[0-9]+ of [0-9]+\b")
MEMORY_NOTE = "pre-memory commits: memory checks not applied"
STEP_STATE: dict[str, str | None] = {
    "brief": "planning",
    "feedback": "planning",
    "plan-start": "planner-running",
    "plan": "reviewing",
    "minors": "awaiting-user",
    "review-start": "reviewer-running",
    "review": "reviewed",
    "consensus": "awaiting-user",
    "approved": "approved",
    "limit-reached": "limit-reached",
    "stopped": "stopped",
    "memory-seed": None,
    "memory-compact": None,
}
STEP_PREVIOUS = {
    "brief": {None},
    "feedback": {"awaiting-user", "limit-reached", "stopped"},
    "plan-start": {"planning", "planner-running", "reviewed", "stopped"},
    "plan": {"planning", "planner-running", "reviewed"},
    "minors": {"reviewed", "planner-running"},
    "review-start": {"reviewing", "reviewer-running", "stopped"},
    "review": {"reviewer-running"},
    "consensus": {"reviewed"},
    "approved": {"awaiting-user"},
    "limit-reached": {"reviewed"},
    "stopped": {"planner-running", "reviewer-running", "reviewed", "limit-reached"},
    "memory-seed": {"planning", "planner-running", "reviewing", "reviewer-running", "reviewed",
                    "limit-reached", "stopped"},
    "memory-compact": {"planning", "reviewing", "reviewed"},
}
RECOVERY_PREFIX = "Recovery:"
COMMIT_RECOVERY = re.compile(
    r"Recovery: commit-check-failed commit=([0-9a-f]{40,64}|none) "
    r"pre-head=([0-9a-f]{40,64}|unknown)$"
)
SKILL_RECOVERY = re.compile(
    r"Recovery: skill-integrity-failed "
    r"phase=(detected|skill-restored|repo-checked) "
    r"baseline=[0-9a-f]{64} "
    r"current=([0-9a-f]{64}|unavailable) "
    r"failure=(none|symlink|special|unreadable|traversal|digest|pre-capture|planted-marker) "
    r"commit=([0-9a-f]{40,64}|none) "
    r"pre-head=([0-9a-f]{40,64}|unknown) "
    r"repo-check=(not-applicable|pending|passed|failed) "
    r"prior=(none|commit-check-failed) "
    r"guard=\S+ guard-sha=([0-9a-f]{64}|none) "
    r"commit-manifest=\S+ commit-manifest-sha=([0-9a-f]{64}|none)$"
)


def _valid_skill_recovery(marker: str) -> bool:
    if not SKILL_RECOVERY.fullmatch(marker):
        return False
    values = dict(token.split("=", 1) for token in marker.split()[2:])
    if (values["guard"] == "none") != (values["guard-sha"] == "none"):
        return False
    if (values["commit-manifest"] == "none") != (
        values["commit-manifest-sha"] == "none"
    ):
        return False
    if values["current"] == "unavailable" and values["failure"] == "none":
        return False
    guarded = values["guard"] != "none"
    terminal_record = (
        values["phase"] == "detected"
        and values["failure"] in {"pre-capture", "planted-marker"}
        and values["repo-check"] == "failed"
    )
    if values["failure"] in {"pre-capture", "planted-marker"} and not terminal_record:
        return False
    if not guarded and values["phase"] != "detected":
        return False
    expected = (
        {"failed"} if terminal_record
        else {"pending"} if guarded and values["phase"] == "detected"
        else {"pending", "failed"} if guarded and values["phase"] == "skill-restored"
        else {"passed"} if guarded
        else {"not-applicable"}
    )
    return values["repo-check"] in expected


def _header(text: str, name: str) -> str | None:
    values = [line[len(name) + 2:] for line in text.splitlines() if line.startswith(f"{name}: ")]
    return values[0] if len(values) == 1 else None


def _show(repo: Path, commit: str, path: str) -> str | None:
    result = _git(repo, "show", f"{commit}:{path}", check=False)
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else None


def _task_relative(repo: Path, task_value: str) -> tuple[str, str]:
    task = _normalize(repo, task_value)
    parent, _, name = task.rpartition("/")
    match = TASK_NAME.match(name)
    if parent != "workspace/outcomes" or not match:
        raise CheckError(f"not a plan-review task folder: {task_value}")
    return task, match.group(1)


def recovery_marker(repo: Path, task: str) -> str | None:
    """Return the sole uncommitted recovery marker; reject ambiguous marker state."""
    path = repo / task / "status.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise CheckError(f"cannot read {path}: {exc}") from exc
    markers = [line for line in text.splitlines() if line.startswith(RECOVERY_PREFIX)]
    if len(markers) > 1:
        raise CheckError(f"{path} contains multiple recovery markers")
    if not markers:
        return None
    marker = markers[0]
    if not (COMMIT_RECOVERY.fullmatch(marker) or _valid_skill_recovery(marker)):
        raise CheckError(f"{path} contains a malformed recovery marker")
    return marker


def write_recovery_marker(repo: Path, status_value: str, commit: str, pre_head: str) -> str:
    status_path = _normalize(repo, status_value)
    task, _ = _task_relative(repo, status_path.rpartition("/")[0])
    if status_path != f"{task}/status.md":
        raise CheckError(f"recovery marker target is not a task status.md: {status_value}")
    absolute = repo / status_path
    if not stat.S_ISREG(absolute.lstat().st_mode):
        raise CheckError(f"{status_path} is not a regular file")
    data = absolute.read_bytes()
    if any(line.startswith(RECOVERY_PREFIX.encode()) for line in data.splitlines()):
        raise CheckError(f"{status_path} already contains a recovery marker")
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    if data and not data.endswith(b"\n"):
        data += newline
    line = f"{RECOVERY_PREFIX} commit-check-failed commit={commit} pre-head={pre_head}"
    absolute.write_bytes(data + line.encode("utf-8") + newline)
    return line


SUBJECT_SUFFIX = re.compile(rf"^plan-review\(([a-z0-9-]+)\): ({STEPS}) v([0-9]+)\s.+$")
ITERATION = re.compile(r"([0-9]+) of ([0-9]+)")
COUNTS = re.compile(r"[0-9]+/[0-9]+/[0-9]+")
OUTCOME_CONSENSUS = re.compile(
    r"consensus at iteration ([0-9]+) of ([0-9]+), open points ([0-9]+/[0-9]+/[0-9]+)"
)
OUTCOME_LIMIT = re.compile(
    r"limit-reached at iteration ([0-9]+) of ([0-9]+), open points ([0-9]+/[0-9]+/[0-9]+)"
)
OUTCOME_STOPPED = re.compile(r"stopped at iteration ([0-9]+) of ([0-9]+) \((.+)\)")
OPEN_VERDICTS = {"CHANGES_REQUESTED", "APPROVE_WITH_MINORS"}
LEGACY_NOTE = "legacy status format: honest-limit checks not applied"


def _subject_failure(subject: str) -> str:
    message = f"subject {subject!r} is not a plan-review step"
    suffixed = SUBJECT_SUFFIX.match(subject)
    if suffixed:
        message += (
            "; retries reuse the exact subject "
            f"plan-review({suffixed.group(1)}): {suffixed.group(2)} v{suffixed.group(3)}"
        )
    return message


class _Context:
    """State carried from one task commit to the next."""

    def __init__(self, strict: bool = False) -> None:
        self.strict = strict
        self.state: str | None = None
        self.previous_versions: dict[str, int] = {}
        self.previous_step: str | None = None
        self.previous_subject_version: int | None = None
        self.review_verdict: str | None = None
        self.closed_plan = 0
        self.feedback_unblocks_plan = False
        self.format = "legacy"
        self.limit: int | None = None
        self.outcome: str | None = None
        self.review: dict[str, Any] | None = None
        self.memory: str | None = None
        self.memory_text: str | None = None


def _validate_step(
    ctx: _Context,
    task: str,
    step: str,
    version: int,
    changed: set[str],
    read: Callable[[str], str | None],
    first: bool,
) -> list[str]:
    """Validate one task commit against the context and advance the context.

    `changed` holds repo-relative paths changed by the commit; `read(name)` returns the text
    of a task file as it is after the commit, or None when absent.
    """
    failures: list[str] = []
    if first and (step != "brief" or version != 1):
        failures.append(f"first task commit must be brief v1, got {step} v{version}")
    if ctx.previous_subject_version is None:
        expected_version = 1
    elif step in {"plan-start", "plan", "minors"}:
        latest_plan = ctx.previous_versions.get("plan.md", 0)
        latest_review = ctx.previous_versions.get("review.md", 0)
        ceiling = max(latest_review, ctx.closed_plan)
        expected_version = latest_plan if latest_plan > ceiling else ceiling + 1
    else:
        expected_version = ctx.previous_subject_version
    if version != expected_version:
        failures.append(
            f"{step} v{version} must be v{expected_version} after "
            f"{ctx.previous_step or 'the start of history'}"
        )

    allowed = {f"{task}/{name}" for name in STEP_FILES.get(step, {"status.md"})}
    required = {f"{task}/{name}" for name in STEP_REQUIRED.get(step, {"status.md"})}
    if changed - allowed:
        failures.append(f"{step} changed disallowed paths {sorted(changed - allowed)}")
    if required - changed:
        failures.append(f"{step} did not change required paths {sorted(required - changed)}")

    status = read("status.md")
    if status is None:
        failures.append("status.md is missing")
        ctx.state = None
        return failures
    if any(line.startswith(RECOVERY_PREFIX) for line in status.splitlines()):
        failures.append("committed status.md contains a recovery marker")
    if ctx.previous_subject_version is None and step == "brief":
        ctx.format = "current" if _header(status, "Outcome") is not None else "legacy"
        if ctx.strict and ctx.format == "legacy":
            failures.append("brief v1 status.md must record an Outcome line")
    new_state = _header(status, "State")
    expected_state = STEP_STATE[step] if STEP_STATE[step] is not None else ctx.state
    if new_state != expected_state:
        verb = "set" if STEP_STATE[step] is not None else "keep"
        failures.append(f"{step} must {verb} State {expected_state}, got {new_state}")
    if ctx.state not in STEP_PREVIOUS[step]:
        failures.append(f"{step} is not allowed after State {ctx.state}")
    iteration = ITERATION.fullmatch(_header(status, "Iteration") or "")
    if not iteration or int(iteration.group(1)) != version:
        failures.append(f"status.md Iteration does not match v{version}")
    elif int(iteration.group(1)) > int(iteration.group(2)):
        failures.append("Iteration exceeds its limit")

    shown: dict[str, str] = {}
    for name, steps in (("plan.md", {"plan", "minors"}), ("review.md", {"review"})):
        if step in steps:
            text = read(name) or ""
            shown[name] = text
            if _header(text, "Version") != str(version):
                failures.append(f"{name} Version does not match v{version}")
            ctx.previous_versions[name] = version
    if step == "review":
        ctx.review_verdict = _header(shown.get("review.md", ""), "Verdict")
        ctx.feedback_unblocks_plan = False
        if ctx.previous_versions.get("plan.md") != version:
            failures.append(f"review v{version} has no committed plan v{version}")

    if ctx.format == "current":
        failures.extend(_current_checks(ctx, step, version, status, iteration))
    else:
        failures.extend(_legacy_checks(ctx, step, version))
    failures.extend(_memory_checks(ctx, task, step, version, changed, read))

    outcome = _header(status, "Outcome")
    if iteration and ctx.format == "current":
        if step == "review":
            ctx.review = {
                "version": version,
                "verdict": ctx.review_verdict,
                "iteration": iteration.group(0),
                "open": _header(status, "Open points"),
            }
        if step in {"brief", "feedback"}:
            ctx.limit = int(iteration.group(2))
    elif iteration and step == "brief":
        ctx.limit = int(iteration.group(2))
    ctx.outcome = outcome
    ctx.state = new_state
    ctx.previous_step = step
    ctx.previous_subject_version = version
    return failures


def _legacy_checks(ctx: _Context, step: str, version: int) -> list[str]:
    """Rules for task folders whose status files carry no Outcome line."""
    failures: list[str] = []
    if (
        step == "feedback"
        and ctx.review_verdict == "APPROVE_WITH_MINORS"
        and ctx.closed_plan < ctx.previous_versions.get("review.md", 0)
    ):
        ctx.feedback_unblocks_plan = True
    if step == "minors":
        review_version = ctx.previous_versions.get("review.md")
        if (
            review_version is None
            or ctx.review_verdict != "APPROVE_WITH_MINORS"
            or review_version != version - 1
        ):
            got = "none" if review_version is None else f"v{review_version} {ctx.review_verdict}"
            failures.append(
                f"minors requires the latest committed review at version "
                f"{version - 1} with Verdict APPROVE_WITH_MINORS, got {got}"
            )
        else:
            ctx.closed_plan = version
    if (
        step in {"consensus", "limit-reached", "plan"}
        and ctx.review_verdict == "APPROVE_WITH_MINORS"
        and ctx.closed_plan < ctx.previous_versions.get("review.md", 0)
        and not (step == "plan" and ctx.feedback_unblocks_plan)
    ):
        failures.append(f"{step} is rejected after Verdict APPROVE_WITH_MINORS until minors commits")
    return failures


def _current_checks(
    ctx: _Context,
    step: str,
    version: int,
    status: str,
    iteration: re.Match[str] | None,
) -> list[str]:
    """Rules for task folders whose status files record an Outcome line."""
    failures: list[str] = []
    outcome = _header(status, "Outcome")
    open_points = _header(status, "Open points")
    if outcome is None:
        failures.append(f"{step} status.md must record an Outcome line")
    if step == "minors":
        failures.append(
            f"minors is not used in the current status format; Minors close commits plan "
            f"v{version} and is reviewed"
        )
    if iteration is None:
        return failures
    count, limit = int(iteration.group(1)), int(iteration.group(2))
    if ctx.limit is not None and limit != ctx.limit and step not in {"brief", "feedback"}:
        failures.append(
            f"{step} changed the limit from {ctx.limit} to {limit}; only feedback may change it"
        )

    review = ctx.review
    if step in {"plan-start", "plan"} and review and version == review["version"] + 1:
        if ctx.limit is not None and review["version"] >= ctx.limit:
            failures.append(
                f"{step} v{version} is rejected: review v{review['version']} reached the limit "
                f"{ctx.limit}; only feedback with an explicit K continues"
            )

    if step in {"brief", "feedback", "plan-start", "plan", "review-start", "review"}:
        if outcome is not None and outcome != "none":
            failures.append(f"{step} must set Outcome none, got {outcome}")
    elif step in MEMORY_STEPS:
        if outcome != ctx.outcome:
            failures.append(f"{step} must keep Outcome {ctx.outcome!r}, got {outcome!r}")
    elif step == "consensus":
        expected = f"consensus at iteration {count} of {limit}, open points 0/0/0"
        if not review or review["version"] != version or review["verdict"] != "NO_FURTHER_COMMENTS":
            got = "none" if not review else f"v{review['version']} {review['verdict']}"
            failures.append(
                f"consensus requires the latest committed review at version {version} with "
                f"Verdict NO_FURTHER_COMMENTS, got {got}"
            )
        if outcome != expected:
            failures.append(f"consensus must set Outcome {expected!r}, got {outcome!r}")
        if open_points != "0/0/0":
            failures.append(f"consensus must record Open points 0/0/0, got {open_points}")
    elif step == "limit-reached":
        if not review or review["version"] != version or review["verdict"] not in OPEN_VERDICTS:
            got = "none" if not review else f"v{review['version']} {review['verdict']}"
            failures.append(
                f"limit-reached requires the latest committed review at version {version} with "
                f"open points (CHANGES_REQUESTED or APPROVE_WITH_MINORS), got {got}"
            )
        else:
            review_limit = int(review["iteration"].split(" of ")[1])
            if version != review_limit:
                failures.append(
                    f"limit-reached requires review v{version} at its limit, "
                    f"got {review['iteration']}"
                )
            if iteration.group(0) != review["iteration"]:
                failures.append(
                    f"limit-reached Iteration {iteration.group(0)} must equal the review's "
                    f"{review['iteration']}"
                )
            if open_points != review["open"]:
                failures.append(
                    f"limit-reached Open points {open_points} must equal the review's "
                    f"{review['open']}"
                )
        if open_points is None or not COUNTS.fullmatch(open_points) or open_points == "0/0/0":
            failures.append(f"limit-reached must keep nonzero Open points, got {open_points}")
        expected = f"limit-reached at iteration {count} of {limit}, open points {open_points}"
        if outcome != expected:
            failures.append(f"limit-reached must set Outcome {expected!r}, got {outcome!r}")
    elif step == "stopped":
        reason = _header(status, "Stop reason")
        match = OUTCOME_STOPPED.fullmatch(outcome or "")
        if not match or (int(match.group(1)), int(match.group(2))) != (count, limit):
            failures.append(
                f"stopped must set Outcome 'stopped at iteration {count} of {limit} (<reason>)', "
                f"got {outcome!r}"
            )
        elif reason in {None, "none"} or match.group(3) != reason:
            failures.append(
                f"stopped Outcome reason {match.group(3)!r} must equal Stop reason {reason!r}"
            )
    elif step == "approved":
        if outcome != ctx.outcome or not OUTCOME_CONSENSUS.fullmatch(outcome or ""):
            failures.append(
                f"approved must keep the consensus Outcome {ctx.outcome!r}, got {outcome!r}"
            )
    return failures


def _memory_parts(text: str) -> tuple[dict[str, list[str]], list[str]]:
    """Split memory.md into its fixed sections (non-blank lines); report format failures."""
    failures: list[str] = []
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# Memory — "):
        failures.append("memory.md must start with '# Memory — <title>'")
    parts: dict[str, list[str]] = {}
    order: list[str] = []
    current: str | None = None
    for line in lines[1:]:
        if len(line) > MEMORY_LINE_CHARS:
            failures.append(f"memory.md has a line over {MEMORY_LINE_CHARS} characters")
        if line.startswith("Iteration:"):
            failures.append("memory.md must not record an Iteration line")
        if line.startswith("## "):
            current = line[3:].strip()
            order.append(current)
            parts[current] = []
        elif current is None:
            if line.strip():
                failures.append("memory.md has text before its first section")
        elif line.strip():
            parts[current].append(line)
    if tuple(order) != MEMORY_SECTIONS:
        failures.append(f"memory.md sections must be exactly {list(MEMORY_SECTIONS)}, got {order}")
        return {name: parts.get(name, []) for name in MEMORY_SECTIONS}, failures
    coverage = parts["Coverage"]
    if not coverage or not MEMORY_AUDIT.match(coverage[0]) or any(
        line.startswith("Audit:") for line in coverage[1:]
    ):
        failures.append("memory.md Coverage must start with one valid Audit line")
    for name in ("Coverage", "User decisions / retired IDs", "Round log"):
        if any(MEMORY_BLIND.search(line) for line in parts[name]):
            failures.append(f"memory.md {name} reveals progress or budget")
    return parts, failures


def _memory_size(text: str) -> tuple[int, int]:
    data = text.encode("utf-8")
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0), len(data)


def _over(size: tuple[int, int], limit: tuple[int, int]) -> bool:
    return size[0] >= limit[0] or size[1] >= limit[1]


def _audit_state(parts: dict[str, list[str]]) -> str | None:
    coverage = parts.get("Coverage") or [""]
    match = MEMORY_AUDIT.match(coverage[0])
    return match.group(1) if match else None


def _ids(lines: list[str], pattern: str = r"P[0-9]{3,}") -> set[str]:
    return {token for line in lines for token in re.findall(pattern, line)}


def _review_ids(text: str) -> set[str]:
    ids: set[str] = set()
    in_points = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_points = line.rstrip() == "## Points"
            continue
        match = re.match(r"^[0-9]+\. (P[0-9]{3,}) ", line)
        if in_points and match:
            ids.add(match.group(1))
    return ids


def _memory_checks(
    ctx: _Context,
    task: str,
    step: str,
    version: int,
    changed: set[str],
    read: Callable[[str], str | None],
) -> list[str]:
    """Rules for the orchestrator-owned memory.md, from the commit that starts it."""
    failures: list[str] = []
    text = read("memory.md")
    touched = f"{task}/memory.md" in changed
    previous = ctx.memory_text
    current = ctx.previous_subject_version or version
    if ctx.memory is None:
        if text is None:
            if step == "memory-compact":
                failures.append("memory-compact requires an existing memory.md")
            if ctx.strict and step == "brief":
                failures.append("brief v1 must create memory.md (memory_build.py init)")
            if ctx.strict and ctx.format == "current" and step in {"plan-start", "review-start"}:
                failures.append(
                    f"{step} requires memory.md; run memory_build.py seed and commit "
                    f"memory-seed v{current} first"
                )
            return failures
        if step not in {"brief", "memory-seed"}:
            failures.append(f"{step} cannot start memory.md; only brief v1 or memory-seed may")
        ctx.memory = "native" if step == "brief" else "seeded"
    elif text is None:
        failures.append("memory.md is missing after it was started")
        return failures
    elif step == "memory-seed":
        failures.append("memory-seed requires that memory.md is absent")
    if step == "review" and not touched:
        failures.append("review must update memory.md")

    parts, parse_failures = _memory_parts(text)
    if touched or previous is None:
        failures.extend(parse_failures)
    size = _memory_size(text)
    if step in {"plan-start", "review-start"} and _over(size, MEMORY_SOFT):
        failures.append(
            f"{step} is rejected: memory.md is {size[0]} lines, {size[1]} bytes, at or above the "
            f"soft limit ({MEMORY_SOFT[0]} lines or {MEMORY_SOFT[1]} bytes); commit "
            f"memory-compact v{current} first"
        )
    if touched and step != "review" and _over(size, MEMORY_HARD):
        failures.append(
            f"{step} leaves memory.md at {size[0]} lines, {size[1]} bytes, at or above the hard "
            f"limit ({MEMORY_HARD[0]} lines or {MEMORY_HARD[1]} bytes)"
        )
    if touched and previous is not None and not parse_failures:
        before, _ = _memory_parts(previous)
        closed_before, closed_now = _ids(before["Closed points"]), _ids(parts["Closed points"])
        if closed_before - closed_now:
            failures.append(f"memory.md lost closed point IDs {sorted(closed_before - closed_now)}")
        audit_before, audit_now = _audit_state(before), _audit_state(parts)
        if step == "review":
            open_ids = _ids(parts["Open points"], r"^- (P[0-9]{3,}) ")
            review_ids = _review_ids(read("review.md") or "")
            if open_ids != review_ids:
                failures.append(
                    f"memory.md Open points {sorted(open_ids)} differ from review.md Points "
                    f"{sorted(review_ids)}"
                )
            expected = f"done v{version}" if audit_before == "pending" else audit_before
            if audit_now != expected:
                failures.append(f"review must record 'Audit: {expected}', got {audit_now!r}")
        elif step == "feedback" and audit_now not in {audit_before, "pending"}:
            failures.append(f"feedback may only set Audit pending, got {audit_now!r}")
        elif step == "memory-compact":
            for name in MEMORY_KEPT:
                if parts[name] != before[name]:
                    failures.append(f"memory-compact changed the {name} section")
            if closed_now != closed_before:
                failures.append("memory-compact changed the closed point IDs")
            if _over(size, MEMORY_SOFT):
                failures.append("memory-compact must end below the soft limit")
    ctx.memory_text = text
    return failures


def _show_task_file(repo: Path, commit: str, task: str) -> Callable[[str], str | None]:
    return lambda name: _show(repo, commit, f"{task}/{name}")


def _replay(repo: Path, task: str, slug: str, strict: bool = False) -> tuple[_Context, list[str], list[str]]:
    """Replay every commit that touched the task folder; return context, failures, commits."""
    log = _git(
        repo, "log", "--reverse", "--full-history", "--format=%H%x1f%P%x1f%s%x1e", "--", task
    ).stdout.decode("utf-8", "replace")
    entries = [record.strip("\n").split("\x1f", 2) for record in log.split("\x1e") if record.strip()]
    commits = [entry[0] for entry in entries]
    failures: list[str] = []
    ctx = _Context(strict=strict)
    for index, (commit, parent_text, subject) in enumerate(entries):
        short = commit[:12]
        match = SUBJECT.match(subject)
        if not match:
            failures.append(f"{short}: {_subject_failure(subject)}")
            ctx.state = None
            continue
        subject_slug, step, version_text = match.groups()
        version = int(version_text)
        if subject_slug != slug:
            failures.append(f"{short}: subject slug {subject_slug} does not match task slug {slug}")

        parents = parent_text.split()
        if len(parents) != 1:
            failures.append(f"{short}: task commits must have exactly one parent, got {len(parents)}")
            continue
        changed: set[str] = set()
        if not strict:  # strict replay only carries context; its path failures are not reported
            changed_output = _git(
                repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", parents[0], commit
            ).stdout.decode("utf-8", "surrogateescape")
            changed = set(filter(None, changed_output.split("\0")))

        for message in _validate_step(
            ctx, task, step, version, changed, _show_task_file(repo, commit, task), index == 0
        ):
            failures.append(f"{short}: {message}")

        if strict:
            continue  # tree shape of earlier commits is reported by `history`, not re-checked here
        tree = _git(repo, "ls-tree", "-r", "-z", commit, "--", f"{task}/").stdout.decode(
            "utf-8", "surrogateescape"
        )
        present_files: set[str] = set()
        for record in filter(None, tree.split("\0")):
            meta, path = record.split("\t", 1)
            mode, kind, _blob = meta.split()
            name = path[len(task) + 1:]
            if name not in TASK_FILES or "/" in name:
                failures.append(f"{short}: unexpected task file {path}")
            elif mode != "100644" or kind != "blob":
                failures.append(f"{short}: {path} has mode {mode} {kind}, expected 100644 blob")
            else:
                present_files.add(name)
        persistent = {"brief.md", "status.md"} | ({"memory.md"} if ctx.memory else set())
        missing_persistent = persistent - present_files
        if missing_persistent:
            failures.append(
                f"{short}: persistent task files are missing {sorted(missing_persistent)}"
            )
    return ctx, failures, commits


def history(repo_value: str, task_value: str) -> dict[str, Any]:
    """Validate every commit touching a task folder from Git history alone."""
    repo = Path(repo_value).resolve(strict=True)
    task, slug = _task_relative(repo, task_value)
    ctx, failures, commits = _replay(repo, task, slug)
    if not commits:
        failures.append("no task commits found")
    result: dict[str, Any] = {
        "ok": not failures,
        "task": task,
        "commits": len(commits),
        "last_commit": commits[-1] if commits else None,
        "last_state": ctx.state,
        "format": ctx.format,
        "memory": ctx.memory or "absent",
        "recovery": recovery_marker(repo, task),
        "failures": failures,
    }
    if ctx.format == "legacy":
        result["note"] = LEGACY_NOTE
    if ctx.memory == "seeded":
        result["memory_note"] = MEMORY_NOTE
    return result


def _validate_staged_step(
    repo: Path, allowed: list[str], changed: list[str], subject: str
) -> None:
    """Reject a staged step that history would reject, before anything is committed."""
    match = SUBJECT.match(subject)
    if not match:
        raise CheckError(_subject_failure(subject))
    subject_slug, step, version_text = match.groups()
    statuses = [path for path in allowed if path.endswith("/status.md")]
    if len(statuses) != 1:
        raise CheckError("--subject requires exactly one task status.md among the allowed paths")
    task, slug = _task_relative(repo, statuses[0].rpartition("/")[0])
    if subject_slug != slug:
        raise CheckError(f"subject slug {subject_slug} does not match task slug {slug}")
    ctx, _failures, commits = _replay(repo, task, slug, strict=True)

    def read(name: str) -> str | None:
        result = _git(repo, "show", f":{task}/{name}", check=False)
        return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else None

    problems = _validate_step(
        ctx, task, step, int(version_text), set(changed), read, first=not commits
    )
    if problems:
        raise CheckError(f"staged {step} v{version_text} is invalid: " + "; ".join(problems))


def _canonical_json(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def write_manifest(manifest: dict[str, Any], output_value: str) -> str:
    data = _canonical_json(manifest)
    try:
        Path(output_value).write_bytes(data)
    except OSError as exc:
        raise CheckError(f"cannot write manifest {output_value}: {exc}") from exc
    return _sha256_bytes(data)


def load_manifest(path_value: str, expected_sha256: str) -> dict[str, Any]:
    try:
        data = Path(path_value).read_bytes()
    except OSError as exc:
        raise CheckError(f"cannot read manifest {path_value}: {exc}") from exc
    actual = _sha256_bytes(data)
    if actual != expected_sha256:
        raise CheckError(f"manifest integrity failure: expected {expected_sha256}, got {actual}")
    manifest = json.loads(data)
    if manifest.get("version") != MANIFEST_VERSION:
        raise CheckError(f"unsupported manifest version: {manifest.get('version')}")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    pre_parser = subparsers.add_parser("pre")
    pre_parser.add_argument("--repo", required=True)
    pre_parser.add_argument("--output", required=True)
    pre_parser.add_argument("--subject", required=True)
    pre_parser.add_argument("paths", nargs="+")

    post_parser = subparsers.add_parser("post")
    post_parser.add_argument("--repo", required=True)
    post_parser.add_argument("--baseline", required=True)
    post_parser.add_argument("--baseline-sha256", required=True)
    post_parser.add_argument("--status-file", required=True)

    history_parser = subparsers.add_parser("history")
    history_parser.add_argument("--repo", required=True)
    history_parser.add_argument("--task", required=True)
    return parser


def _post_command(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve(strict=True)
    pre_head = "unknown"
    try:
        manifest = load_manifest(args.baseline, args.baseline_sha256)
        pre_head = manifest["pre_head"]
        if manifest["repo"] != str(repo):
            raise CheckError("manifest repository does not match --repo")
        if _normalize(repo, args.status_file) not in manifest["files"]:
            raise CheckError("--status-file is not one of the committed allowed paths")
        result = post(manifest)
        code = 0 if result["ok"] else 1
    except CheckError as exc:
        print(f"commit_check: {exc}", file=sys.stderr)
        result, code = None, 2
    if code != 0:
        head = _out(repo, "rev-parse", "--verify", "HEAD")
        commit = "none" if head == pre_head else head
        try:
            marker = write_recovery_marker(repo, args.status_file, commit, pre_head)
            print(f"commit_check: wrote uncommitted marker: {marker}", file=sys.stderr)
        except (CheckError, OSError) as exc:
            print(f"commit_check: could not write recovery marker: {exc}", file=sys.stderr)
            code = 2
    if result is not None:
        print(json.dumps(result, indent=2, sort_keys=True))
    return code


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "pre":
            print(write_manifest(pre(args.repo, args.paths, args.subject), args.output))
            return 0
        if args.command == "post":
            return _post_command(args)
        result = history(args.repo, args.task)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] and result["recovery"] is None else 1
    except CheckError as exc:
        print(f"commit_check: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
