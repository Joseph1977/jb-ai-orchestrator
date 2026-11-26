#!/usr/bin/env python3
"""Check plan anchors for the plan-review loop."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

TASK_NAME = re.compile(r"^task-[a-z0-9]+(-[a-z0-9]+){1,4}_[0-9]{8}-[0-9]{6}Z-[0-9a-f]{4}$")
POINT_ID = re.compile(r"P[0-9]{3,}")
ANCHOR = r"[A-OQ-Z][0-9]+"
ANCHOR_ONLY = re.compile(rf"^{ANCHOR}$")
ANCHOR_LINE = re.compile(rf"^(?:[-*] |[0-9]+\. )?\**({ANCHOR})\b")
ANCHOR_HEADING = re.compile(rf"^#{{3,4}} +\**({ANCHOR})\b")
HEADING = re.compile(r"^#{3,} ")
BRIEF_ITEM = re.compile(r"^\s*(?:[-*] )?\**(C-[0-9]+|In-[0-9]+|Out-[0-9]+)\b")
BRIEF_ITEM_ONLY = re.compile(r"^(C-[0-9]+|In-[0-9]+|Out-[0-9]+|Q[0-9]+|Done)$")
QUESTION = re.compile(r"^([0-9]+)\. ")
CHANGE = re.compile(rf"^- ({ANCHOR}) (added|changed|removed) \(([^()]*)\): (.+)$")
RESPONSE = re.compile(r"^\|\s*(P[0-9]{3,})\s*\|\s*([^|]+?)\s*\|(.*)$")
PLAN_SECTIONS = ("## Response to review", "## Changes", "## Plan")
CHANGE_CHARS = 300


class BuildError(RuntimeError):
    """Usage or internal error: the orchestrator stops (exit 2)."""


class FormatError(RuntimeError):
    """Agent output breaks a format rule: a format failure (exit 1)."""

    def __init__(self, failures: list[str]) -> None:
        super().__init__("; ".join(failures))
        self.failures = failures


def sections(text: str) -> dict[str, list[str]]:
    """Map each `## ` heading line to its body lines; text before the first heading is ''."""
    result: dict[str, list[str]] = {"": []}
    current = ""
    for line in text.splitlines():
        if line.startswith("## "):
            current = line.rstrip()
            result.setdefault(current, [])
            continue
        result[current].append(line)
    return result


def section(text: str, prefix: str) -> list[str] | None:
    for heading, body in sections(text).items():
        if heading == prefix or heading.startswith(prefix + " "):
            return body
    return None


def header(text: str, name: str) -> str | None:
    values = [line[len(name) + 2:] for line in text.splitlines() if line.startswith(f"{name}: ")]
    return values[0] if len(values) == 1 else None


def brief_items(text: str) -> list[str]:
    """Checklist items in brief order: C-/In-/Out- lines, numbered Goal questions, Done."""
    items: list[str] = []
    goal = False
    for line in text.splitlines():
        if line.startswith("## "):
            goal = line.rstrip() == "## Goal"
            continue
        match = BRIEF_ITEM.match(line)
        if match:
            items.append(match.group(1))
        elif goal and QUESTION.match(line):
            items.append(f"Q{QUESTION.match(line).group(1)}")
        if line.startswith("Done means"):
            items.append("Done")
    seen: set[str] = set()
    return [item for item in items if not (item in seen or seen.add(item))]


def plan_anchors(text: str) -> tuple[dict[str, str], list[str]]:
    """Return anchor → block text for `## Plan`, plus anchoring failures."""
    body = section(text, "## Plan")
    if body is None:
        return {}, ["plan.md has no `## Plan` section"]
    anchors: dict[str, list[str]] = {}
    failures: list[str] = []
    current: str | None = None
    heading_anchor: str | None = None
    fenced = False
    for number, raw in enumerate(body, 1):
        line = raw.rstrip()
        if fenced:
            if current is None:
                failures.append(f"## Plan line {number} is outside any anchor")
            else:
                anchors[current].append(line)
            fenced = not line.lstrip().startswith("```")
            continue
        if not line.strip():
            continue
        heading = ANCHOR_HEADING.match(line)
        if heading or HEADING.match(line):
            name = heading.group(1) if heading else None
            heading_anchor = current = name
            if name is not None:
                if name in anchors:
                    failures.append(f"anchor {name} is defined twice")
                anchors[name] = [line]
            continue
        match = ANCHOR_LINE.match(line)
        if match and not line[:1].isspace():
            name = match.group(1)
            if name in anchors:
                failures.append(f"anchor {name} is defined twice")
            anchors[name] = [line]
            current = name
        elif line[:1].isspace() and current is not None:
            anchors[current].append(line)
        elif heading_anchor is not None:
            anchors[heading_anchor].append(line)
            current = heading_anchor
        else:
            failures.append(f"## Plan line {number} is outside any anchor: {line[:80]!r}")
            continue
        if line.lstrip().startswith("```"):
            fenced = True
    return {name: "\n".join(lines) for name, lines in anchors.items()}, failures


def changes(text: str) -> tuple[dict[str, tuple[str, list[str], str]], list[str]]:
    """Parse `## Changes`: anchor → (kind, cited tokens, text), plus failures."""
    body = section(text, "## Changes") or []
    parsed: dict[str, tuple[str, list[str], str]] = {}
    failures: list[str] = []
    for line in (raw.rstrip() for raw in body):
        if not line:
            continue
        if len(line) > CHANGE_CHARS:
            failures.append(f"## Changes line exceeds {CHANGE_CHARS} characters: {line[:40]!r}")
        match = CHANGE.match(line)
        if not match:
            failures.append(f"malformed ## Changes line: {line[:80]!r}")
            continue
        anchor, kind, cited, what = match.groups()
        tokens = [token.strip() for token in cited.split(",") if token.strip()]
        for token in tokens:
            if not (POINT_ID.fullmatch(token) or token in {"consistency", "observation"}):
                failures.append(f"## Changes {anchor} cites {token!r}")
        if anchor in parsed:
            failures.append(f"## Changes lists {anchor} twice")
        parsed[anchor] = (kind, tokens, what)
    return parsed, failures


def responses(text: str) -> dict[str, tuple[str, str]]:
    """Parse `## Response to review` rows: point → (decision, reason)."""
    body = section(text, "## Response to review") or []
    rows: dict[str, tuple[str, str]] = {}
    for line in body:
        match = RESPONSE.match(line.strip())
        if match:
            point, decision, reason = match.groups()
            rows[point] = (decision.strip(), reason.strip().rstrip("|").strip())
    return rows


def review_point_ids(text: str) -> list[str]:
    body = section(text, "## Points") or []
    found = []
    for line in body:
        match = re.match(r"^[0-9]+\. (P[0-9]{3,}) ", line)
        if match:
            found.append(match.group(1))
    return found


def _repo_task(repo_value: str, task_value: str) -> tuple[Path, str]:
    repo = Path(repo_value).resolve(strict=True)
    candidate = Path(task_value)
    absolute = Path(os.path.abspath(candidate if candidate.is_absolute() else repo / candidate))
    try:
        task = absolute.relative_to(repo).as_posix()
    except ValueError as exc:
        raise BuildError(f"task is outside the repository: {task_value}") from exc
    parent, _, name = task.rpartition("/")
    if parent != "workspace/outcomes" or not TASK_NAME.fullmatch(name):
        raise BuildError(f"not a plan-review task folder: {task_value}")
    return repo, task


def committed(repo: Path, task: str, name: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{task}/{name}"],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.decode("utf-8") if result.returncode == 0 else None


def worktree(repo: Path, task: str, name: str) -> str | None:
    path = repo / task / name
    try:
        if not path.is_file() or path.is_symlink():
            raise BuildError(f"{task}/{name} is not a regular file")
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise BuildError(f"cannot read {task}/{name}: {exc}") from exc


def check_plan(plan: str, previous_plan: str | None, previous_review: str | None) -> dict[str, Any]:
    """Validate anchors, sections and `## Changes` of a new plan against the committed one."""
    failures: list[str] = []
    for heading in sections(plan):
        if heading and not heading.startswith(PLAN_SECTIONS):
            failures.append(f"plan.md section {heading!r} is not allowed")
    anchors, anchor_failures = plan_anchors(plan)
    failures += anchor_failures
    listed, change_failures = changes(plan)
    has_changes = section(plan, "## Changes") is not None
    if previous_review is None:
        if has_changes:
            failures.append("## Changes is only written when a prior review.md exists")
    else:
        failures += change_failures
        if not has_changes:
            failures.append("## Changes is required when a prior review.md exists")
        review_ids = set(review_point_ids(previous_review))
        for anchor, (kind, tokens, _what) in listed.items():
            for token in tokens:
                if POINT_ID.fullmatch(token) and token not in review_ids:
                    failures.append(f"## Changes {anchor} cites {token}, not a prior review point")
            if kind == "removed" and anchor in anchors:
                failures.append(f"## Changes marks {anchor} removed but it is still defined")
            if kind != "removed" and anchor not in anchors:
                failures.append(f"## Changes lists {anchor} {kind} but it is not defined")
        cited = {token for _kind, tokens, _what in listed.values() for token in tokens}
        for point, (decision, _reason) in responses(plan).items():
            if decision == "accepted" and point not in cited:
                failures.append(f"accepted {point} is cited by no ## Changes line")
        previous_anchors, previous_failures = plan_anchors(previous_plan or "")
        if previous_plan is not None and not previous_failures:
            actual: dict[str, str] = {}
            for anchor in sorted(set(anchors) | set(previous_anchors)):
                if anchor not in previous_anchors:
                    actual[anchor] = "added"
                elif anchor not in anchors:
                    actual[anchor] = "removed"
                elif anchors[anchor] != previous_anchors[anchor]:
                    actual[anchor] = "changed"
            for anchor, kind in actual.items():
                if anchor not in listed:
                    failures.append(f"{anchor} was {kind} but ## Changes does not list it")
                elif listed[anchor][0] != kind:
                    failures.append(f"## Changes marks {anchor} {listed[anchor][0]}, but it was {kind}")
            for anchor in listed:
                if anchor not in actual:
                    failures.append(f"## Changes lists {anchor}, whose text did not change")
    if failures:
        raise FormatError(failures)
    return {"anchors": len(anchors), "changes": len(listed)}


def plan_check(repo: Path, task: str) -> dict[str, Any]:
    plan = worktree(repo, task, "plan.md")
    if plan is None:
        raise BuildError("plan.md is missing")
    return check_plan(plan, committed(repo, task, "plan.md"), committed(repo, task, "review.md"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    check = subparsers.add_parser("plan-check")
    check.add_argument("--repo", required=True)
    check.add_argument("--task", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        repo, task = _repo_task(args.repo, args.task)
        result = plan_check(repo, task)
    except FormatError as exc:
        for failure in exc.failures:
            print(f"memory_build: {failure}", file=sys.stderr)
        return 1
    except BuildError as exc:
        print(f"memory_build: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
