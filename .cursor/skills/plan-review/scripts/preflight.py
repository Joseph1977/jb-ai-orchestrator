#!/usr/bin/env python3
"""Portable preflight checks for the plan-review skill."""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path


class PreflightError(RuntimeError):
    """Raised when a portable preflight check cannot be proved."""


TASK_FILES = ("brief.md", "plan.md", "review.md", "status.md")


def resolve_skill_root(skill_file_value: str) -> Path:
    """Resolve a direct or symlinked SKILL.md and validate its runtime layout."""
    try:
        skill_file = Path(skill_file_value).resolve(strict=True)
        details = skill_file.lstat()
    except OSError as exc:
        raise PreflightError(f"cannot resolve SKILL.md: {exc}") from exc
    if skill_file.name != "SKILL.md" or not stat.S_ISREG(details.st_mode):
        raise PreflightError(f"canonical skill file is not a regular SKILL.md: {skill_file}")

    scripts = skill_file.parent / "scripts"
    try:
        scripts_details = scripts.lstat()
    except OSError as exc:
        raise PreflightError(f"cannot stat canonical scripts directory: {exc}") from exc
    if not stat.S_ISDIR(scripts_details.st_mode):
        raise PreflightError(f"canonical scripts path is not a real directory: {scripts}")
    return skill_file.parent


def proposed_task_files(repo_value: str, task_value: str) -> list[str]:
    repo = Path(repo_value).resolve(strict=True)
    task = Path(task_value)
    absolute = Path(os.path.abspath(task if task.is_absolute() else repo / task))
    try:
        relative = absolute.relative_to(repo)
    except ValueError as exc:
        raise PreflightError(f"task path is outside repository: {task_value}") from exc
    if relative == Path("."):
        raise PreflightError("task path cannot be the repository root")
    return [(relative / name).as_posix() for name in TASK_FILES]


def ignored_task_files(
    repo_value: str, task_value: str, *, env: dict[str, str] | None = None
) -> list[str]:
    """Return proposed task files ignored by Git; fail closed on Git errors."""
    repo = Path(repo_value).resolve(strict=True)
    ignored: list[str] = []
    for path in proposed_task_files(str(repo), task_value):
        result = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "--no-index", "-q", "--", path],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        if result.returncode == 0:
            ignored.append(path)
        elif result.returncode != 1:
            message = result.stderr.decode("utf-8", "replace").strip()
            raise PreflightError(
                f"git check-ignore failed for {path} with {result.returncode}: {message}"
            )
    return ignored


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    root_parser = subparsers.add_parser("skill-root")
    root_parser.add_argument("--skill-file", required=True)

    ignore_parser = subparsers.add_parser("check-ignore")
    ignore_parser.add_argument("--repo", required=True)
    ignore_parser.add_argument("--task", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "skill-root":
            print(resolve_skill_root(args.skill_file))
            return 0
        ignored = ignored_task_files(args.repo, args.task)
        if ignored:
            for path in ignored:
                print(f"ignored: {path}")
            return 1
        print("task paths are not ignored")
        return 0
    except (OSError, PreflightError) as exc:
        print(f"preflight: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
