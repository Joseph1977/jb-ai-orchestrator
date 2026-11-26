#!/usr/bin/env python3
"""Render a plan-review prompt from the shipped template plus a fixed set of scalars.

A reviewer prompt also takes `--scope audit|converge`, which appends that scope's block; an audit
also takes `--checklist`, the item list `memory_build.py scope` prints.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SKILL_FILE = SKILL_ROOT / "SKILL.md"
PROMPTS_FILE = SKILL_ROOT / "references" / "prompts.md"

TASK_NAME = re.compile(r"^task-[a-z0-9]+(-[a-z0-9]+){1,4}_[0-9]{8}-[0-9]{6}Z-[0-9a-f]{4}$")
MODEL = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
POINT_ID = re.compile(r"^P[0-9]{3,}$")
ID_LIST = re.compile(r"^(none|P[0-9]{3,}(,P[0-9]{3,})*)$")
TOKEN = re.compile(r"\{\{([A-Z_]+)\}\}")
UNSAFE = re.compile(r"[\s`]")
FORMAT_ERROR = re.compile(r"[\x20-\x5f\x61-\x7a|~]{1,600}")
CHECKLIST = re.compile(r"[A-Z][A-Za-z0-9-]{0,15}(,[A-Z][A-Za-z0-9-]{0,15}){0,499}")

ALLOWED_SCALARS = (
    "TASK_FOLDER",
    "MODEL",
    "PLAN_CAP",
    "REVIEW_CAP",
    "NEXT_POINT_ID",
    "RETIRED_IDS",
    "PUSHED_BACK_IDS",
    "POLICY_PROBE_PY",
)
MODES = ("planner", "reviewer", "minors")


class BuildError(RuntimeError):
    """Raised when a prompt cannot be rendered exactly."""


def _read(path: Path) -> str:
    try:
        if not path.is_file() or path.is_symlink():
            raise BuildError(f"{path.name} is not a regular file")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BuildError(f"cannot read {path.name}: {exc}") from exc


def _between(text: str, name: str, source: str) -> str:
    begin, end = f"<!-- BEGIN {name} -->", f"<!-- END {name} -->"
    if text.count(begin) != 1 or text.count(end) != 1:
        raise BuildError(f"{source} must contain exactly one {name} block")
    before, _, rest = text.partition(begin)
    body, _, _after = rest.partition(end)
    if end in before or end not in rest:
        raise BuildError(f"{source} has a malformed {name} block")
    return body.strip()


def _probe_code(skill_text: str) -> str:
    body = _between(skill_text, "POLICY_PROBE_PY", "SKILL.md")
    fence_open, fence_close = "```python", "```"
    if not (body.startswith(fence_open) and body.endswith(fence_close)):
        raise BuildError("POLICY_PROBE_PY block is not a python fence")
    return body[len(fence_open):-len(fence_close)].strip()


def _clean(name: str, value: str, pattern: re.Pattern[str]) -> str:
    if UNSAFE.search(value):
        raise BuildError(f"{name} contains whitespace, a newline, or a backtick")
    if not pattern.fullmatch(value):
        raise BuildError(f"{name} is not an allowed value: {value!r}")
    return value


def _cap(name: str, value: int) -> str:
    if not 1 <= value <= 1000:
        raise BuildError(f"{name} must be between 1 and 1000")
    return str(value)


def _task_folder(value: str) -> str:
    _clean("--task", value, re.compile(r"/.+"))
    path = Path(value)
    try:
        canonical = path.resolve(strict=True)
    except OSError as exc:
        raise BuildError(f"--task does not exist: {value}") from exc
    if str(canonical) != value or not canonical.is_dir():
        raise BuildError("--task must be a canonical absolute directory")
    parent = canonical.parent
    if parent.name != "outcomes" or parent.parent.name != "workspace":
        raise BuildError("--task must be a direct child of workspace/outcomes")
    if not TASK_NAME.fullmatch(canonical.name):
        raise BuildError("--task name does not match the task folder name regex")
    return value


def scalars(args: argparse.Namespace, probe: str) -> dict[str, str]:
    values = {
        "TASK_FOLDER": _task_folder(args.task),
        "MODEL": _clean("--model", args.model, MODEL),
        "PLAN_CAP": _cap("--plan-cap", args.plan_cap),
        "REVIEW_CAP": _cap("--review-cap", args.review_cap),
        "POLICY_PROBE_PY": probe,
    }
    for key, flag, option, pattern in (
        ("NEXT_POINT_ID", "--next-id", args.next_id, POINT_ID),
        ("RETIRED_IDS", "--retired", args.retired, ID_LIST),
        ("PUSHED_BACK_IDS", "--pushed-back", args.pushed_back, ID_LIST),
    ):
        if option is not None:
            values[key] = _clean(flag, option, pattern)
    return values


def render(template: str, values: dict[str, str]) -> str:
    names = TOKEN.findall(template)
    for name in names:
        if name not in ALLOWED_SCALARS:
            raise BuildError(f"unknown token {{{{{name}}}}}")
        if name not in values:
            raise BuildError(f"token {{{{{name}}}}} was not supplied")
    stripped = TOKEN.sub("", template)
    if "{{" in stripped or "}}" in stripped:
        raise BuildError("template contains a stray brace")
    return TOKEN.sub(lambda match: values[match.group(1)], template)


SCOPE_BLOCKS = {"audit": "AUDIT_BLOCK", "converge": "CONVERGE_BLOCK"}


def _literal_line(prompts: str, name: str) -> str:
    line = _between(prompts, name, "prompts.md")
    if TOKEN.search(line) or "{{" in line or "}}" in line or "\n" in line:
        raise BuildError(f"{name} must be one literal line")
    return line


def build(mode: str, values: dict[str, str], scope: str | None = None,
          format_error: str | None = None, checklist: str | None = None) -> str:
    if (mode == "reviewer") != (scope is not None):
        raise BuildError("--scope is required for reviewer and refused for planner/minors")
    if (scope == "audit") != (checklist is not None):
        raise BuildError("--checklist is required for --scope audit and refused otherwise")
    prompts = _read(PROMPTS_FILE)
    template_name = "REVIEWER_PROMPT" if mode == "reviewer" else "PLANNER_PROMPT"
    prompt = render(_between(prompts, template_name, "prompts.md"), values)
    if mode == "minors":
        prompt = f"{prompt}\n{_literal_line(prompts, 'MINORS_LINE')}"
    if scope is not None:
        prompt = f"{prompt}\n{_literal_line(prompts, SCOPE_BLOCKS[scope])}"
    if checklist is not None:
        items = ", ".join(_checklist(checklist).split(","))
        prompt = f"{prompt}\n{_literal_line(prompts, 'CHECKLIST_LINE')} {items}"
    if format_error is not None:
        prompt = f"{prompt}\n{_literal_line(prompts, 'RETRY_LINE')} {format_error}"
    return prompt


def _format_error(value: str) -> str:
    if not FORMAT_ERROR.fullmatch(value):
        raise BuildError("--format-error must be one line of at most 600 printable characters "
                         "with no backtick or brace")
    return value


def _checklist(value: str) -> str:
    items = value.split(",")
    if not CHECKLIST.fullmatch(value) or len(set(items)) != len(items):
        raise BuildError("--checklist must be distinct comma-separated item IDs with no spaces")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=MODES)
    parser.add_argument("--task", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--plan-cap", required=True, type=int)
    parser.add_argument("--review-cap", required=True, type=int)
    parser.add_argument("--next-id")
    parser.add_argument("--retired")
    parser.add_argument("--pushed-back")
    parser.add_argument("--scope", choices=tuple(SCOPE_BLOCKS))
    parser.add_argument("--format-error")
    parser.add_argument("--checklist")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        probe = _probe_code(_read(SKILL_FILE))
        error = None if args.format_error is None else _format_error(args.format_error)
        prompt = build(args.mode, scalars(args, probe), args.scope, error, args.checklist)
    except BuildError as exc:
        print(f"prompt_build: {exc}", file=sys.stderr)
        return 2
    data = prompt.encode("utf-8")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    print(hashlib.sha256(data).hexdigest(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
