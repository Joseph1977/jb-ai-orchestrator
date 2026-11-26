#!/usr/bin/env python3
"""Check plan anchors and build the orchestrator-owned memory.md for the plan-review loop.

Modes: init, plan-check, scope, update, feedback, seed, compact. Inputs are committed task files
(`git show HEAD:`) plus the one worktree file the current step validates. Exit 0 is success,
1 is a format failure of agent output (or a memory floor over the soft limit), 2 stops.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

TASK_NAME = re.compile(r"^task-[a-z0-9]+(-[a-z0-9]+){1,4}_[0-9]{8}-[0-9]{6}Z-[0-9a-f]{4}$")
POINT_ID = re.compile(r"P[0-9]{3,}")
ANCHOR = r"[A-OQ-Z][0-9]+"
ANCHOR_LINE = re.compile(rf"^(?:[-*] |[0-9]+\. )?\**({ANCHOR})\b")
ANCHOR_HEADING = re.compile(rf"^#{{3,4}} +\**({ANCHOR})\b")
HEADING = re.compile(r"^#{3,} ")
BRIEF_ITEM = re.compile(r"^\s*(?:[-*] )?\**(C-[0-9]+|In-[0-9]+|Out-[0-9]+)\b")
QUESTION = re.compile(r"^([0-9]+)\. ")
CHANGE = re.compile(rf"^- ({ANCHOR}) (added|changed|removed) \(([^()]*)\): (.+)$")
RESPONSE = re.compile(r"^\|\s*(P[0-9]{3,})\s*\|\s*([^|]+?)\s*\|(.*)$")
POINT = re.compile(
    r"^([0-9]+)\. (P[0-9]{3,}) \[(blocker|major|minor)\]( \[late\])? \(([^()]+)\) (.+) → (.+)$"
)
LEGACY_POINT = re.compile(r"^[0-9]+\. (P[0-9]{3,}) \[(blocker|major|minor)\] (.+?)(?: → .*)?$")
COVERAGE_OK = re.compile(r"^- ok: (.+)$")
COVERAGE_POINTS = re.compile(r"^- (\S+): (P[0-9]{3,}(?:, P[0-9]{3,})*)$")
OBSERVATION = re.compile(r"^- \(([^()]+)\) (.+)$")
ACCEPT = re.compile(r"^(P[0-9]{3,}): accept rebuttal$")
PLAN_SECTIONS = ("## Response to review", "## Changes", "## Plan")
CHANGE_CHARS = 300

MEMORY_SECTIONS = (
    "Settled decisions", "Coverage", "Open points", "Observations", "Closed points",
    "User decisions / retired IDs", "Round log",
)
KEPT_SECTIONS = (
    "Settled decisions", "Coverage", "Open points", "Observations", "User decisions / retired IDs",
)
SOFT_LINES, SOFT_BYTES = 200, 16 * 1024
HARD_LINES, HARD_BYTES = 300, 24 * 1024
TARGET_LINES, TARGET_BYTES = 100, 8 * 1024
LINE_CHARS = 600
ISSUE_CHARS = 160
KEEP_ROUNDS = 3
OPEN_LINE = re.compile(
    r"^- (P[0-9]{3,}) \[(blocker|major|minor)\]( \[late\])? \(([^()]*)\) "
    r"(new|late|maintained|seeded) v([0-9]+): (.*)$"
)
CLOSED_LINE = re.compile(
    r"^- (P[0-9]{3,}) \[(blocker|major|minor)\]( \[late\])? \(([^()]*)\) "
    r"closed v([0-9]+) (fixed|rebuttal accepted): (.*)$"
)
CLOSED_GROUP = re.compile(
    r"^- (\S+): ((?:P[0-9]{3,}, )*P[0-9]{3,}) closed v([0-9]+)(?:–v([0-9]+))? "
    r"\(fixed ([0-9]+), rebuttal ([0-9]+)\)$"
)
SETTLED_LINE = re.compile(r"^- (\S+) — (.+?): (.*)$")
COVERAGE_LINE = re.compile(r"^- (verified v[0-9]+|decided v[0-9]+|unverified|open (\S+)): (.+)$")
AUDIT_LINE = re.compile(r"^Audit: (pending|done v[0-9]+|waived at seed v[0-9]+)$")
ROUND_LINE = re.compile(
    r"^- v([0-9]+) (audit|converge): .*; new ([0-9]+), late ([0-9]+), observations ([0-9]+), "
    r"maintained ([0-9]+), closed ([0-9]+)$"
)
SUMMARY_LINE = re.compile(
    r"^- v([0-9]+)–v([0-9]+) summary: rounds ([0-9]+), new ([0-9]+), late ([0-9]+), "
    r"observations ([0-9]+), closed ([0-9]+)$"
)
BLIND = re.compile(r"(?i)\blimit\b|\bremaining\b|\bbudget\b|\bfinal\b|\b[0-9]+ of [0-9]+\b")


class BuildError(RuntimeError):
    """Usage or internal error: the orchestrator stops (exit 2)."""


class FormatError(RuntimeError):
    """Agent output breaks a format rule, or memory cannot shrink (exit 1)."""

    def __init__(self, failures: list[str]) -> None:
        super().__init__("; ".join(failures))
        self.failures = failures


# Shared Markdown parsing


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


def title(text: str) -> str:
    first = text.splitlines()[0] if text.strip() else ""
    return first.split(" — ", 1)[1].strip() if " — " in first else "untitled"


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
    """Return anchor → block text for `## Plan` (in plan order), plus anchoring failures."""
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
    found = []
    for line in section(text, "## Points") or []:
        match = re.match(r"^[0-9]+\. (P[0-9]{3,}) ", line)
        if match:
            found.append(match.group(1))
    return found


def id_list(value: str | None) -> list[str]:
    if not value or value.strip() == "none":
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def measure(text: str) -> tuple[int, int]:
    """Lines and bytes as the policy probe counts them."""
    data = text.encode("utf-8")
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0), len(data)


def at_or_over(text: str, lines: int, size: int) -> bool:
    counted_lines, counted_bytes = measure(text)
    return counted_lines >= lines or counted_bytes >= size


def number(point: str) -> int:
    return int(point[1:])


def point_id(value: int) -> str:
    return f"P{value:03d}"


# Plan check


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


# Memory model


class Memory:
    """Parsed memory.md. Kept sections stay as raw lines; derived sections are re-rendered."""

    def __init__(self, name: str) -> None:
        self.title = name
        self.settled: dict[str, tuple[list[str], str]] = {}
        self.audit = "pending"
        self.coverage: dict[str, str] = {}
        self.open: list[dict[str, Any]] = []
        self.observations: list[str] = []
        self.closed: list[str] = []
        self.decisions: list[str] = []
        self.log: list[str] = []

    @classmethod
    def parse(cls, text: str) -> "Memory":
        parts, failures = memory_sections(text)
        if failures:
            raise BuildError("memory.md is malformed: " + "; ".join(failures))
        memory = cls(title(text))
        for line in parts["Settled decisions"]:
            match = SETTLED_LINE.match(line)
            if not match:
                raise BuildError(f"memory.md has a malformed settled line: {line[:80]!r}")
            memory.settled[match.group(1)] = (match.group(2).split(", "), match.group(3))
        for line in parts["Coverage"]:
            audit = AUDIT_LINE.match(line)
            if audit:
                memory.audit = audit.group(1)
                continue
            match = COVERAGE_LINE.match(line)
            if not match:
                raise BuildError(f"memory.md has a malformed coverage line: {line[:80]!r}")
            state, item = match.group(1), match.group(2)
            if item:
                memory.coverage[item] = "open " + match.group(3)
            else:
                for name in match.group(3).split(", "):
                    memory.coverage[name] = state
        for line in parts["Open points"]:
            match = OPEN_LINE.match(line)
            if not match:
                raise BuildError(f"memory.md has a malformed open point: {line[:80]!r}")
            memory.open.append({
                "id": match.group(1), "severity": match.group(2), "late": bool(match.group(3)),
                "anchors": [a for a in match.group(4).split(", ") if a],
                "status": match.group(5), "version": int(match.group(6)), "issue": match.group(7),
            })
        memory.observations = list(parts["Observations"])
        memory.closed = list(parts["Closed points"])
        memory.decisions = list(parts["User decisions / retired IDs"])
        memory.log = list(parts["Round log"])
        return memory

    def closed_ids(self) -> set[str]:
        return {token for line in self.closed for token in POINT_ID.findall(line)}

    def known_ids(self) -> set[str]:
        return self.closed_ids() | {point["id"] for point in self.open} | {
            token for line in self.decisions for token in POINT_ID.findall(line)
        }

    def render(self) -> str:
        lines = [f"# Memory — {self.title}", "", "## Settled decisions"]
        for anchor, (refs, text) in self.settled.items():
            lines.append(short(f"- {anchor} — {', '.join(refs)}: {text}", LINE_CHARS))
        lines.append("## Coverage")
        lines.append(f"Audit: {self.audit}")
        lines += render_coverage(self.coverage)
        lines.append("## Open points")
        for point in self.open:
            late = " [late]" if point["late"] else ""
            lines.append(short(
                f"- {point['id']} [{point['severity']}]{late} ({', '.join(point['anchors'])}) "
                f"{point['status']} v{point['version']}: {point['issue']}", LINE_CHARS))
        lines.append("## Observations")
        lines += self.observations
        lines.append("## Closed points")
        lines += self.closed
        lines.append("## User decisions / retired IDs")
        lines += self.decisions
        lines.append("## Round log")
        lines += self.log
        return "\n".join(lines) + "\n"


def memory_sections(text: str) -> tuple[dict[str, list[str]], list[str]]:
    """Split memory.md into its fixed sections; report structure, line and blindness failures."""
    failures: list[str] = []
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# Memory — "):
        failures.append("memory.md must start with '# Memory — <title>'")
    parts: dict[str, list[str]] = {}
    current: str | None = None
    order: list[str] = []
    for line in lines[1:]:
        if len(line) > LINE_CHARS:
            failures.append(f"memory.md line exceeds {LINE_CHARS} characters: {line[:40]!r}")
        if line.startswith("Iteration:"):
            failures.append("memory.md must not record an Iteration line")
        if line.startswith("## "):
            current = line[3:].strip()
            order.append(current)
            parts[current] = []
            continue
        if current is None:
            if line.strip():
                failures.append(f"memory.md has text before its first section: {line[:40]!r}")
            continue
        if line.strip():
            parts[current].append(line)
    if tuple(order) != MEMORY_SECTIONS:
        failures.append(f"memory.md sections must be exactly {list(MEMORY_SECTIONS)}, got {order}")
        return {name: parts.get(name, []) for name in MEMORY_SECTIONS}, failures
    audits = [line for line in parts["Coverage"] if line.startswith("Audit:")]
    if len(audits) != 1 or not AUDIT_LINE.match(audits[0]) or parts["Coverage"][0] != audits[0]:
        failures.append("memory.md Coverage must start with one valid Audit line")
    for name in ("Coverage", "User decisions / retired IDs", "Round log"):
        for line in parts[name]:
            if BLIND.search(line):
                failures.append(f"memory.md {name} reveals progress or budget: {line[:60]!r}")
    return parts, failures


def render_coverage(coverage: dict[str, str]) -> list[str]:
    groups: dict[str, list[str]] = {}
    opened: list[str] = []
    for item, state in coverage.items():
        if state.startswith("open "):
            opened.append(f"- open {item}: {state[5:]}")
        else:
            groups.setdefault(state, []).append(item)

    def rank(state: str) -> tuple[int, int]:
        version = int(state.rsplit(" v", 1)[1]) if " v" in state else 0
        return ({"verified": 0, "decided": 1}.get(state.split(" ")[0], 2), version)

    lines: list[str] = []
    for state in sorted(groups, key=rank):
        lines += wrap(f"- {state}: ", groups[state])
    return lines + opened


def wrap(prefix: str, items: list[str]) -> list[str]:
    lines, current = [], []
    for item in items:
        if current and len(prefix) + len(", ".join(current + [item])) > LINE_CHARS:
            lines.append(prefix + ", ".join(current))
            current = []
        current.append(item)
    if current:
        lines.append(prefix + ", ".join(current))
    return lines


def canonical_coverage(memory: Memory, brief: str, plan: str | None) -> dict[str, str]:
    """Order coverage as brief items then plan anchors; drop items no longer defined."""
    items = brief_items(brief) + (list(plan_anchors(plan)[0]) if plan else [])
    return {item: memory.coverage.get(item, "unverified") for item in dict.fromkeys(items)}


# Review parsing and memory update


def parse_review(review: str) -> tuple[list[dict[str, Any]], dict[str, list[str]],
                                      list[tuple[list[str], str]], set[str], list[str]]:
    """Return points, coverage (item → IDs, [] for ok), observations, accepted rebuttals, failures."""
    failures: list[str] = []
    points: list[dict[str, Any]] = []
    for line in (raw.rstrip() for raw in section(review, "## Points") or []):
        if not line:
            continue
        match = POINT.match(line)
        if not match:
            failures.append(f"point is not anchored or malformed: {line[:80]!r}")
            continue
        _ordinal, point, severity, late, anchors, issue, fix = match.groups()
        points.append({"id": point, "severity": severity, "late": bool(late),
                       "anchors": [a.strip() for a in anchors.split(",") if a.strip()],
                       "issue": issue, "fix": fix})
    coverage: dict[str, list[str]] = {}
    body = section(review, "## Coverage")
    if body is None:
        failures.append("review.md has no `## Coverage` section")
    for line in (raw.rstrip() for raw in body or []):
        if not line:
            continue
        ok, pointed = COVERAGE_OK.match(line), COVERAGE_POINTS.match(line)
        entries = ([(item.strip(), []) for item in ok.group(1).split(",")] if ok
                   else [(pointed.group(1), pointed.group(2).split(", "))] if pointed else None)
        if entries is None:
            failures.append(f"malformed coverage line: {line[:80]!r}")
            continue
        for item, ids in entries:
            if item in coverage:
                failures.append(f"coverage lists {item} twice")
            coverage[item] = ids
    observations: list[tuple[list[str], str]] = []
    observation_body = section(review, "## Observations")
    for line in (raw.rstrip() for raw in observation_body or []):
        if not line:
            continue
        match = OBSERVATION.match(line)
        if not match:
            failures.append(f"malformed observation: {line[:80]!r}")
            continue
        observations.append(([a.strip() for a in match.group(1).split(",")], match.group(2)))
    if observation_body is not None and not observations:
        failures.append("## Observations is present but empty")
    accepted = {ACCEPT.match(line.strip()).group(1)
                for line in section(review, "## Pushback assessment") or []
                if ACCEPT.match(line.strip())}
    return points, coverage, observations, accepted, failures


def apply_review(memory: Memory, brief: str, plan: str, review: str, scope: str,
                 retired: list[str], next_id: str | None) -> dict[str, Any]:
    """Validate review.md against scope rules and fold it into memory; return status counts."""
    version = int(header(review, "Version") or 0)
    points, coverage, observations, accepted, failures = parse_review(review)
    anchors = plan_anchors(plan)[0]
    checklist = brief_items(brief) + list(anchors)
    known = set(checklist)
    listed = [a for a, (kind, _t, _w) in changes(plan)[0].items() if kind != "removed"]
    point_ids = [point["id"] for point in points]
    previous = {point["id"]: point for point in memory.open}
    retired_set = set(retired)
    if len(set(point_ids)) != len(point_ids):
        failures.append("review.md repeats a point ID")
    for point in points:
        unknown = [a for a in point["anchors"] if a not in known]
        if unknown:
            failures.append(f"{point['id']} cites unknown anchors {unknown}")
        if point["id"] in previous:
            continue
        if point["id"] in memory.closed_ids() | retired_set:
            failures.append(f"{point['id']} reuses a closed or retired point ID")
        if point["late"]:
            if scope == "audit":
                failures.append(f"{point['id']} is tagged [late] in an audit review")
            elif point["severity"] != "blocker":
                failures.append(f"{point['id']} is a late finding below blocker; write it under "
                                "## Observations")
            elif " Missed: " not in f" {point['fix']}":
                failures.append(f"{point['id']} is late but gives no 'Missed: <reason>'")
        elif scope == "converge" and not set(point["anchors"]) & set(listed):
            failures.append(f"new {point['id']} names no anchor listed in ## Changes; raise it as "
                            "[blocker] [late] or write it under ## Observations")
    for item in coverage:
        if item not in known:
            failures.append(f"coverage lists unknown item {item}")
    for item, ids in coverage.items():
        for point in ids:
            if point not in point_ids:
                failures.append(f"coverage {item} cites {point}, which is not in ## Points")
    if scope == "audit":
        missing = [item for item in checklist if item not in coverage]
        if missing:
            failures.append(f"audit coverage misses {missing}")
        covered = {point for ids in coverage.values() for point in ids}
        for point in point_ids:
            if point not in covered:
                failures.append(f"audit coverage lists no item for {point}")
        if section(review, "## Observations") is not None:
            failures.append("an audit review writes no ## Observations")
    else:
        if sorted(coverage) != sorted(listed):
            failures.append(f"converge coverage must list exactly the ## Changes anchors {listed}, "
                            f"got {sorted(coverage)}")
    for anchors_cited, _text in observations:
        unknown = [a for a in anchors_cited if a not in known]
        if unknown:
            failures.append(f"observation cites unknown anchors {unknown}")
    if failures:
        raise FormatError(failures)

    response_rows = responses(plan)
    listed_changes = changes(plan)[0]
    closed_now = [pid for pid in previous if pid not in point_ids and pid not in retired_set]
    for pid in closed_now:
        old = previous[pid]
        kind = "rebuttal accepted" if pid in accepted else "fixed"
        late = " [late]" if old["late"] else ""
        memory.closed.append(short(
            f"- {pid} [{old['severity']}]{late} ({', '.join(old['anchors'])}) closed v{version} "
            f"{kind}: {old['issue']}", LINE_CHARS))
        if kind == "fixed":
            for anchor, (change_kind, tokens, what) in listed_changes.items():
                if pid in tokens:
                    upsert(memory, anchor, f"{pid} (v{version})",
                           "removed" if change_kind == "removed" else what)
        else:
            reason = response_rows.get(pid, ("", ""))[1]
            for anchor in old["anchors"]:
                if anchor != "legacy":
                    upsert(memory, anchor, f"{pid} (v{version})",
                           "rebuttal accepted: " + short(reason, 200))
    for anchor, (change_kind, _tokens, what) in listed_changes.items():
        if anchor in memory.settled:
            refs, _old = memory.settled[anchor]
            memory.settled[anchor] = (refs, "removed" if change_kind == "removed" else what)

    cov = canonical_coverage(memory, brief, plan)
    for item, ids in coverage.items():
        cov[item] = f"open {', '.join(ids)}" if ids else f"verified v{version}"
    if scope == "audit":
        memory.audit = f"done v{version}"
    for item, state in list(cov.items()):
        ids = [p for p in state[5:].split(", ")] if state.startswith("open ") else []
        ids = [p for p in ids if p in point_ids]
        ids += [p["id"] for p in points if item in p["anchors"] and p["id"] not in ids]
        if ids:
            cov[item] = "open " + ", ".join(ids)
        elif state.startswith("open "):
            cov[item] = f"verified v{version}"
    memory.coverage = cov

    memory.open = []
    counts = {"new": 0, "late": 0, "maintained": 0}
    for point in points:
        status = ("maintained" if point["id"] in previous
                  else "late" if point["late"] else "new")
        counts[status] += 1
        memory.open.append({**point, "status": status, "version": version,
                            "issue": short(point["issue"], ISSUE_CHARS)})
    for anchors_cited, text in observations:
        memory.observations.append(short(f"- v{version} ({', '.join(anchors_cited)}): {text}",
                                         LINE_CHARS))
    decisions = [row[0] for pid, row in response_rows.items()]
    verdict = header(review, "Verdict") or "unknown"
    severities = [point["severity"] for point in points]
    open_counts = "/".join(str(severities.count(s)) for s in ("blocker", "major", "minor"))
    memory.log.append(
        f"- v{version} {scope}: responses {len(decisions)} (accepted {decisions.count('accepted')}, "
        f"partly {decisions.count('partly accepted')}, pushed back {decisions.count('pushed back')}, "
        f"closed by user {decisions.count('closed by user')}); {verdict} open {open_counts}; "
        f"new {counts['new']}, late {counts['late']}, observations {len(observations)}, "
        f"maintained {counts['maintained']}, closed {len(closed_now)}"
    )
    highest = max([number(p) for p in memory.known_ids() | set(point_ids) | retired_set] or [0])
    floor = number(next_id) if next_id and POINT_ID.fullmatch(next_id) else 1
    return {
        "open": open_counts, "new": counts["new"], "late": counts["late"],
        "observations": len(observations), "maintained": counts["maintained"],
        "closed": len(closed_now), "next_id": point_id(max(highest + 1, floor)),
    }


def upsert(memory: Memory, anchor: str, ref: str, text: str) -> None:
    refs, _old = memory.settled.get(anchor, ([], ""))
    memory.settled[anchor] = (refs + ([ref] if ref not in refs else []), short(text, 300))


# Compaction


def compact_text(text: str) -> str:
    """Summarise Closed points and Round log; keep every other section byte for byte."""
    parts, failures = memory_sections(text)
    if failures:
        raise BuildError("memory.md is malformed: " + "; ".join(failures))
    head = text.split("\n## Closed points\n", 1)[0]
    tail_index = text.index("\n## User decisions / retired IDs\n")
    decisions_and_log = text[tail_index:].split("\n## Round log\n", 1)[0]

    kept_closed: list[str] = []
    groups: dict[str, dict[str, Any]] = {}
    for line in parts["Closed points"]:
        group, single = CLOSED_GROUP.match(line), CLOSED_LINE.match(line)
        if group:
            key, ids = group.group(1), group.group(2).split(", ")
            low = int(group.group(3))
            high = int(group.group(4) or low)
            fixed, rebuttal = int(group.group(5)), int(group.group(6))
        elif single:
            anchors = [a for a in single.group(4).split(", ") if a]
            key, ids = (anchors[0] if anchors else "legacy"), [single.group(1)]
            low = high = int(single.group(5))
            fixed, rebuttal = (1, 0) if single.group(6) == "fixed" else (0, 1)
        else:
            kept_closed.append(line)
            continue
        entry = groups.setdefault(key, {"ids": [], "low": low, "high": high, "fixed": 0,
                                        "rebuttal": 0})
        entry["ids"] += [pid for pid in ids if pid not in entry["ids"]]
        entry["low"], entry["high"] = min(entry["low"], low), max(entry["high"], high)
        entry["fixed"] += fixed
        entry["rebuttal"] += rebuttal
    closed = list(kept_closed)
    for key, entry in groups.items():
        span = f"v{entry['low']}" + (f"–v{entry['high']}" if entry["high"] != entry["low"] else "")
        suffix = f" closed {span} (fixed {entry['fixed']}, rebuttal {entry['rebuttal']})"
        for chunk in wrap("", entry["ids"]):
            if len(f"- {key}: {chunk}{suffix}") > LINE_CHARS:
                raise BuildError(f"closed group {key} cannot fit one line")
            closed.append(f"- {key}: {chunk}{suffix}")

    other, rounds, summary = [], [], None
    for line in parts["Round log"]:
        if ROUND_LINE.match(line):
            rounds.append(line)
        elif SUMMARY_LINE.match(line):
            summary = line
        else:
            other.append(line)
    folded, kept = rounds[:-KEEP_ROUNDS], rounds[-KEEP_ROUNDS:]
    log = list(other)
    if folded or summary:
        totals = {"rounds": 0, "new": 0, "late": 0, "observations": 0, "closed": 0}
        versions: list[int] = []
        if summary:
            match = SUMMARY_LINE.match(summary)
            versions += [int(match.group(1)), int(match.group(2))]
            for key, index in (("rounds", 3), ("new", 4), ("late", 5), ("observations", 6),
                               ("closed", 7)):
                totals[key] += int(match.group(index))
        for line in folded:
            match = ROUND_LINE.match(line)
            versions.append(int(match.group(1)))
            totals["rounds"] += 1
            for key, index in (("new", 3), ("late", 4), ("observations", 5), ("closed", 7)):
                totals[key] += int(match.group(index))
        log.append(
            f"- v{min(versions)}–v{max(versions)} summary: rounds {totals['rounds']}, "
            f"new {totals['new']}, late {totals['late']}, observations {totals['observations']}, "
            f"closed {totals['closed']}"
        )
    log += kept
    body = [head, "## Closed points", *closed]
    result = "\n".join(body) + decisions_and_log + "\n## Round log\n"
    result += "".join(line + "\n" for line in log)
    new_parts, new_failures = memory_sections(result)
    if new_failures:
        raise BuildError("compaction produced malformed memory.md: " + "; ".join(new_failures))
    for name in KEPT_SECTIONS:
        if new_parts[name] != parts[name]:
            raise BuildError(f"compaction changed the {name} section")
    if Memory.parse(result).closed_ids() != Memory.parse(text).closed_ids():
        raise BuildError("compaction changed the closed point IDs")
    return result


# Seed


def seed_text(brief: str, plan: str | None, review: str | None, status: str,
              audit: str) -> str:
    """Start memory.md for a task that predates it, from committed files only."""
    iteration = re.match(r"([0-9]+) of ", header(status, "Iteration") or "")
    if not iteration:
        raise BuildError("status.md has no Iteration line")
    version = int(iteration.group(1))
    memory = Memory(title(brief))
    memory.audit = "pending" if audit == "pending" else f"waived at seed v{version}"
    memory.coverage = canonical_coverage(memory, brief, plan) if plan else {
        item: "unverified" for item in brief_items(brief)}
    if plan:
        lenient: dict[str, str] = {}
        for line in section(plan, "## Plan") or []:
            match = ANCHOR_LINE.match(line) or ANCHOR_HEADING.match(line)
            if match and not line[:1].isspace():
                lenient.setdefault(match.group(1), "unverified")
        for item in lenient:
            memory.coverage.setdefault(item, "unverified")
    retired = id_list(header(status, "Retired point IDs"))
    open_ids: list[str] = []
    for line in (section(review or "", "## Points") or []):
        modern, legacy = POINT.match(line.rstrip()), LEGACY_POINT.match(line.rstrip())
        if modern:
            _o, pid, severity, late, anchors, issue, _fix = modern.groups()
            cited = [a.strip() for a in anchors.split(",") if a.strip()]
        elif legacy:
            pid, severity, issue = legacy.groups()
            late, cited = None, ["legacy"]
        else:
            continue
        open_ids.append(pid)
        memory.open.append({"id": pid, "severity": severity, "late": bool(late),
                            "anchors": cited, "status": "seeded", "version": version,
                            "issue": short(issue, ISSUE_CHARS)})
        for anchor in cited:
            if anchor in memory.coverage:
                state = memory.coverage[anchor]
                ids = state[5:].split(", ") if state.startswith("open ") else []
                memory.coverage[anchor] = "open " + ", ".join(ids + [pid])
    next_id = header(status, "Next point ID") or ""
    highest = number(next_id) - 1 if POINT_ID.fullmatch(next_id) else max(
        [number(p) for p in open_ids + retired] or [0])
    closed = [point_id(n) for n in range(1, highest + 1)
              if point_id(n) not in open_ids and point_id(n) not in retired]
    for chunk in wrap("", closed):
        memory.closed.append(f"- pre-memory: {chunk} closed before memory (no detail carried)")
    if retired:
        memory.decisions.append(f"- seed v{version}: retired {', '.join(retired)}")
    trend = header(status, "Trend")
    if trend and trend.strip() not in {"", "none"} and not BLIND.search(trend):
        memory.log.append(short(f"- pre-memory trend: {trend.strip()}", LINE_CHARS))
    memory.log.append(f"- seed v{version}: memory started, audit {memory.audit.split(' ')[0]}")
    return memory.render()


# Files and commands


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
        details = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(details.st_mode):
        raise BuildError(f"{task}/{name} is not a regular file")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BuildError(f"cannot read {task}/{name}: {exc}") from exc


def write_memory(repo: Path, task: str, text: str) -> dict[str, Any]:
    _parts, failures = memory_sections(text)
    if failures:
        raise BuildError("refusing to write malformed memory.md: " + "; ".join(failures))
    directory = repo / task
    path = directory / "memory.md"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise BuildError("memory.md is not a regular file")
    fd, temporary = tempfile.mkstemp(prefix=".memory.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    lines, size = measure(text)
    return {"lines": lines, "bytes": size,
            "compact": at_or_over(text, SOFT_LINES, SOFT_BYTES)}


def require(text: str | None, name: str) -> str:
    if text is None:
        raise BuildError(f"{name} is not committed")
    return text


def run(args: argparse.Namespace) -> dict[str, Any]:
    repo, task = _repo_task(args.repo, args.task)
    mode = args.mode
    if mode == "plan-check":
        plan = worktree(repo, task, "plan.md")
        if plan is None:
            raise BuildError("plan.md is missing")
        return check_plan(plan, committed(repo, task, "plan.md"), committed(repo, task, "review.md"))
    if mode == "init":
        if committed(repo, task, "memory.md") is not None or worktree(repo, task, "memory.md"):
            raise BuildError("memory.md already exists")
        brief = worktree(repo, task, "brief.md")
        if brief is None:
            raise BuildError("brief.md is missing")
        memory = Memory(title(brief))
        memory.coverage = {item: "unverified" for item in brief_items(brief)}
        return write_memory(repo, task, memory.render())
    if mode == "seed":
        if committed(repo, task, "memory.md") is not None:
            raise BuildError("memory.md already exists; seed only starts it")
        text = seed_text(require(committed(repo, task, "brief.md"), "brief.md"),
                         committed(repo, task, "plan.md"), committed(repo, task, "review.md"),
                         require(committed(repo, task, "status.md"), "status.md"), args.audit)
        return write_memory(repo, task, text)
    memory_text = require(committed(repo, task, "memory.md"), "memory.md")
    memory = Memory.parse(memory_text)
    if mode == "scope":
        return {"scope": "audit" if memory.audit == "pending" else "converge"}
    if mode == "compact":
        if not at_or_over(memory_text, SOFT_LINES, SOFT_BYTES):
            raise BuildError("memory.md is below the soft limit; nothing to compact")
        result = compact_text(memory_text)
        if at_or_over(result, SOFT_LINES, SOFT_BYTES):
            lines, size = measure(result)
            raise FormatError([f"memory floor is {lines} lines, {size} bytes, at or above the soft "
                               "limit; retire or decide points with the user"])
        written = write_memory(repo, task, result)
        written["target_met"] = not (written["lines"] > TARGET_LINES
                                     or written["bytes"] > TARGET_BYTES)
        return written
    if mode == "update":
        expected = "audit" if memory.audit == "pending" else "converge"
        if args.scope != expected:
            raise BuildError(f"--scope {args.scope} does not match memory.md (expected {expected})")
        brief = require(committed(repo, task, "brief.md"), "brief.md")
        plan = require(committed(repo, task, "plan.md"), "plan.md")
        status = require(committed(repo, task, "status.md"), "status.md")
        review = worktree(repo, task, "review.md")
        if review is None:
            raise BuildError("review.md is missing")
        if header(review, "Version") != header(plan, "Version"):
            raise FormatError(["review.md Version must equal the committed plan.md Version"])
        summary = apply_review(memory, brief, plan, review, args.scope,
                               id_list(header(status, "Retired point IDs")),
                               header(status, "Next point ID"))
        return {**summary, **write_memory(repo, task, memory.render())}
    if mode == "feedback":
        status = worktree(repo, task, "status.md") or ""
        iteration = re.match(r"([0-9]+) of ", header(status, "Iteration") or "")
        if not iteration:
            raise BuildError("status.md has no Iteration line")
        version = iteration.group(1)
        brief = worktree(repo, task, "brief.md") or ""
        retired = id_list(args.retired)
        for pid in retired:
            if not POINT_ID.fullmatch(pid):
                raise BuildError(f"--retired has a bad ID: {pid}")
        if retired:
            memory.decisions.append(f"- feedback v{version}: retired {', '.join(retired)}")
            memory.open = [point for point in memory.open if point["id"] not in retired]
            for item, state in list(memory.coverage.items()):
                if state.startswith("open "):
                    ids = [p for p in state[5:].split(", ") if p not in retired]
                    memory.coverage[item] = ("open " + ", ".join(ids)) if ids else f"decided v{version}"
        if brief != committed(repo, task, "brief.md"):
            if args.no_audit:
                memory.decisions.append(
                    f"- feedback v{version}: point decisions only; audit not required")
            else:
                memory.audit = "pending"
                memory.decisions.append(f"- feedback v{version}: brief changed; audit pending")
        memory.coverage = canonical_coverage(memory, brief, committed(repo, task, "plan.md"))
        text = memory.render()
        if text == memory_text:
            return {"changed": False}
        return {"changed": True, **write_memory(repo, task, text)}
    raise BuildError(f"unknown mode {mode}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("init", "plan-check", "scope", "update", "feedback", "seed", "compact"):
        command = subparsers.add_parser(mode)
        command.add_argument("--repo", required=True)
        command.add_argument("--task", required=True)
        if mode == "update":
            command.add_argument("--scope", required=True, choices=("audit", "converge"))
        if mode == "feedback":
            command.add_argument("--retired")
            command.add_argument("--no-audit", action="store_true")
        if mode == "seed":
            command.add_argument("--audit", required=True, choices=("pending", "waived"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run(args)
    except FormatError as exc:
        for failure in exc.failures:
            print(f"memory_build: {failure}", file=sys.stderr)
        return 1
    except (BuildError, OSError) as exc:
        print(f"memory_build: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
