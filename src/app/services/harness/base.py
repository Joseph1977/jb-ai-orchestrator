# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Base types and helpers for harness adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
import re

import yaml

# Caps to keep eagerly-injected context from blowing the token budget.
MAX_EAGER_FILE_CHARS = 6000
MAX_EAGER_TOTAL_CHARS = 24000
# Root playbook files (AGENTS.md / CLAUDE.md) must load in full or fail explicitly.
MAX_ROOT_INSTRUCTION_CHARS = 512_000
# Bounded metadata scan for catalog descriptions (never load full SKILL.md bodies).
MAX_DESCRIPTION_SCAN_CHARS = 4096

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


class RootInstructionError(Exception):
    """Raised when a root instruction file exceeds the allowed size."""


@dataclass
class PrimitiveRef:
    """A lazily-loadable capability/artifact (skill, agent, rule, command).

    Only ``name``/``path``/``description`` are surfaced up front; the full
    content is read on demand via the local ``read_file`` tool (lazy loading).
    """

    name: str
    path: str  # workspace-relative path
    description: str = ""
    kind: str = "skill"  # skill | agent | rule | command | doc


@dataclass
class HarnessManifest:
    orchestration_type: str
    detected: bool
    confidence: int = 0
    eager_context: str = ""
    agents: List[PrimitiveRef] = field(default_factory=list)
    skills: List[PrimitiveRef] = field(default_factory=list)
    rules: List[PrimitiveRef] = field(default_factory=list)
    commands: List[PrimitiveRef] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "orchestrationType": self.orchestration_type,
            "detected": self.detected,
            "confidence": self.confidence,
            "agents": [p.name for p in self.agents],
            "skills": [p.name for p in self.skills],
            "rules": [p.name for p in self.rules],
            "commands": [p.name for p in self.commands],
            "notes": self.notes,
        }


def read_text_capped(path: Path, cap: int = MAX_EAGER_FILE_CHARS) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > cap:
        return text[:cap] + f"\n\n... [truncated, {len(text) - cap} more chars]"
    return text


def read_root_instructions(path: Path, *, cap: int = MAX_ROOT_INSTRUCTION_CHARS) -> str:
    """Load root AGENTS.md / CLAUDE.md in full; fail explicitly if too large."""
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RootInstructionError(f"Cannot read root instructions at {path}: {exc}") from exc
    if len(text) > cap:
        raise RootInstructionError(
            f"Root instructions at {path} exceed {cap} chars ({len(text)}); "
            "refusing silent truncation"
        )
    return text


def _strip_bom(text: str) -> str:
    if text.startswith("\ufeff"):
        return text[1:]
    return text


def _strip_html_comments(text: str) -> str:
    return _HTML_COMMENT_RE.sub("", text).strip()


def _read_description_scan(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    raw = _strip_bom(raw)
    raw = _strip_html_comments(raw)
    return raw[:MAX_DESCRIPTION_SCAN_CHARS]


def parse_frontmatter_fields(path: Path) -> dict[str, str]:
    """Parse YAML frontmatter name/description when present (bounded scan)."""
    text = _read_description_scan(path)
    if not text.startswith("---"):
        return {}
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        # A malformed skill file degrades its catalog entry; it never breaks discovery.
        return {}
    if not isinstance(loaded, dict):
        return {}

    fields: dict[str, str] = {}
    for key in ("name", "description"):
        value = loaded.get(key)
        if value is None or isinstance(value, (dict, list, bool)):
            continue
        text_value = " ".join(str(value).split())
        if text_value:
            fields[key] = text_value[:200]
    return fields


def first_description(path: Path) -> str:
    """Extract catalog metadata: frontmatter, first substantive heading, or line."""
    text = _read_description_scan(path)
    if not text:
        return ""

    fm = parse_frontmatter_fields(path)
    if fm.get("description"):
        return fm["description"]

    lines = text.splitlines()
    fm_end = 0
    if text.startswith("---"):
        match = _FRONTMATTER_RE.match(text)
        if match:
            # match.end() sits on the closing delimiter, so skip past that line too.
            fm_end = text[: match.end()].count("\n") + 1

    for line in lines[fm_end:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading:
                return heading[:200]
            continue
        return stripped[:200]
    return ""


def primitive_name_from_path(path: Path) -> str:
    fm = parse_frontmatter_fields(path)
    if fm.get("name"):
        return fm["name"]
    if path.name == "SKILL.md":
        return path.parent.name
    return path.stem


def rel(path: Path, workspace: Path) -> str:
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        return str(path)


def normalize_catalog_path(path: Path, workspace: Path) -> str:
    """Normalized workspace-relative path for catalog deduplication."""
    return rel(path, workspace).replace("\\", "/")


class HarnessAdapter:
    """Base class for harness adapters."""

    type_id: str = "generic"

    def detect(self, workspace: Path) -> int:
        """Return a confidence score (0-100) that this adapter fits."""
        raise NotImplementedError

    def collect(self, workspace: Path, *, detected: bool, confidence: int) -> HarnessManifest:
        """Build the manifest of eager context + lazy primitives."""
        raise NotImplementedError

    def _assemble_eager(self, sections: List[tuple[str, str]]) -> str:
        """Join titled sections respecting the total cap."""
        out: List[str] = []
        used = 0
        for title, body in sections:
            if not body:
                continue
            block = f"## {title}\n\n{body}".strip()
            if used + len(block) > MAX_EAGER_TOTAL_CHARS:
                remaining = MAX_EAGER_TOTAL_CHARS - used
                if remaining > 200:
                    out.append(block[:remaining] + "\n\n... [truncated]")
                break
            out.append(block)
            used += len(block)
        return "\n\n".join(out)

    def _discover_primitives(
        self,
        workspace: Path,
        manifest: HarnessManifest,
    ) -> None:
        """Discover flat and dotted skills/commands/rules/agents paths."""
        patterns = [
            (".cursor/skills/*/SKILL.md", "skill"),
            (".cursor/skills/*.md", "skill"),
            (".claude/skills/*/SKILL.md", "skill"),
            (".claude/skills/*.md", "skill"),
            (".cursor/commands/*.md", "command"),
            (".claude/commands/*.md", "command"),
            (".cursor/agents/*.md", "agent"),
            (".claude/agents/*.md", "agent"),
            ("skills/**/*.md", "skill"),
            ("agents/**/*.md", "agent"),
            ("commands/**/*.md", "command"),
            ("rules/**/*.md", "rule"),
        ]
        # Adapters may already have catalogued some of these paths explicitly;
        # seed from the manifest so discovery never lists a primitive twice.
        seen_paths: set[str] = {
            ref.path.replace("\\", "/")
            for bucket in (manifest.skills, manifest.agents, manifest.commands, manifest.rules)
            for ref in bucket
        }
        for pattern, kind in patterns:
            for md in sorted(workspace.glob(pattern)):
                if not md.is_file():
                    continue
                norm_path = normalize_catalog_path(md, workspace)
                if norm_path in seen_paths:
                    continue
                seen_paths.add(norm_path)
                ref = PrimitiveRef(
                    name=primitive_name_from_path(md),
                    path=norm_path,
                    description=first_description(md),
                    kind=kind,
                )
                bucket = {
                    "skill": manifest.skills,
                    "command": manifest.commands,
                    "agent": manifest.agents,
                    "rule": manifest.rules,
                }[kind]
                bucket.append(ref)
