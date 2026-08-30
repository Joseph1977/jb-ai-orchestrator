# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Base types and helpers for harness adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import List, Mapping, Optional
import re

import yaml

# Caps to keep eagerly-injected context from blowing the token budget.
MAX_EAGER_FILE_CHARS = 6000
MAX_EAGER_TOTAL_CHARS = 24000
# Root playbook files (AGENTS.md / CLAUDE.md) must load in full or fail explicitly.
MAX_ROOT_INSTRUCTION_CHARS = 512_000

# Two independent bounds. The metadata window is what we read from the file
# handle; the description bound applies to the prose region after frontmatter.
# They are deliberately not shared with MAX_EAGER_FILE_CHARS: eager content is a
# separate capped read, so changing one cap can never silently reshape the other.
MAX_FRONTMATTER_SCAN_CHARS = 8192
MAX_DESCRIPTION_SCAN_CHARS = 4096
# Per-field cap for anything lifted out of frontmatter into the catalog.
MAX_FIELD_CHARS = 200
# Upper bound on scope patterns retained from a single `globs`/`paths` field.
MAX_SCOPE_ITEMS = 64

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)

# Whitelist of frontmatter keys the scanner will retain, by normalized shape.
# Anything outside this list is discarded before it can reach an adapter.
_SCALAR_FIELDS = ("name", "description")
_BOOL_FIELDS = ("alwaysApply", "disable-model-invocation")
_STR_TUPLE_FIELDS = ("globs", "paths")


class FrontmatterStatus(str, Enum):
    """Outcome of scanning a capability file's metadata block."""

    ABSENT = "absent"
    OK = "ok"
    TRUNCATED = "truncated"  # metadata region larger than the scan window
    MALFORMED = "malformed"


class LoadingPolicy(str, Enum):
    """How a primitive reaches the model.

    Vendor-neutral on purpose. Adapters translate their own conventions
    (Cursor's ``alwaysApply``/``globs``, Claude's ``paths``) into these, so no
    harness-specific vocabulary leaks into the core or the renderer.
    """

    EAGER = "eager"  # injected up front; excluded from the catalog
    SCOPED = "scoped"  # lazy, announced with the paths it applies to
    MODEL_DISCOVERABLE = "model-discoverable"  # lazy, model may select it
    EXPLICIT_ONLY = "explicit-only"  # lazy, only on explicit invocation


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
    # Internal routing metadata. Deliberately absent from ``summary()`` so the
    # caller-facing schema is unchanged.
    policy: LoadingPolicy = LoadingPolicy.MODEL_DISCOVERABLE
    scope: tuple[str, ...] = ()  # paths/globs this primitive applies to
    source: str = ""  # provenance, e.g. "cursor-rules", "legacy-cursorrules"


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


def _strip_html_comments(text: str) -> tuple[str, bool]:
    """Remove complete comments; report whether an unterminated one remains.

    An unterminated ``<!--`` means the comment runs past the scan window, so
    everything we can see is comment body rather than metadata.
    """
    stripped = _HTML_COMMENT_RE.sub("", text)
    return stripped.strip(), "<!--" in stripped


def _read_metadata_window(path: Path) -> str:
    """Read at most ``MAX_FRONTMATTER_SCAN_CHARS`` from the file handle.

    Bounded at the handle rather than by slicing a full read, so an oversized
    capability file never costs more than the window.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(MAX_FRONTMATTER_SCAN_CHARS)
    except OSError:
        return ""


def _normalize_scalar(value: object) -> str:
    """Collapse a YAML scalar to a capped single-line string.

    Containers never reach ``str()``: a YAML alias graph is cheap to load but
    astronomically expensive to walk or render, so it is rejected by shape.
    """
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return ""
    return " ".join(str(value).split())[:MAX_FIELD_CHARS]


def _normalize_bool(value: object) -> Optional[bool]:
    """Accept only real booleans; truthiness is not an activation signal."""
    return value if value is True or value is False else None


def _normalize_str_tuple(value: object) -> tuple[str, ...]:
    """Validate a scope field as a shallow string or list of strings.

    Deliberately non-recursive. Slicing and ``isinstance`` are safe against the
    self-referential lists that YAML aliases can produce, whereas any structural
    walk of such a value would not terminate in useful time.
    """
    if isinstance(value, str):
        items: list[object] = [value]
    elif isinstance(value, list):
        items = value[:MAX_SCOPE_ITEMS]
    else:
        return ()
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        cleaned = " ".join(item.split())[:MAX_FIELD_CHARS]
        if cleaned:
            out.append(cleaned)
    return tuple(out)


def _whitelist_metadata(loaded: Mapping) -> dict[str, object]:
    """Keep only known keys, normalized to scalars or immutable string tuples."""
    meta: dict[str, object] = {}
    for key in _SCALAR_FIELDS:
        value = _normalize_scalar(loaded.get(key))
        if value:
            meta[key] = value
    for key in _BOOL_FIELDS:
        value = _normalize_bool(loaded.get(key))
        if value is not None:
            meta[key] = value
    for key in _STR_TUPLE_FIELDS:
        patterns = _normalize_str_tuple(loaded.get(key))
        if patterns:
            meta[key] = patterns
    return meta


def _load_frontmatter(text: str) -> tuple[dict, "FrontmatterStatus"]:
    if not text.startswith("---"):
        return {}, FrontmatterStatus.ABSENT
    match = _FRONTMATTER_RE.match(text)
    if not match:
        # Opening delimiter with no closing one inside the window: the metadata
        # region is bigger than the scan, so the lines we can see are YAML keys
        # rather than prose. Reporting ABSENT here would surface "---".
        return {}, FrontmatterStatus.TRUNCATED
    try:
        loaded = yaml.safe_load(match.group(1))
    except (yaml.YAMLError, RecursionError):
        # RecursionError is a RuntimeError, not a YAMLError, and deeply nested
        # sequences reach it well inside the scan window. One bad file degrades
        # its own catalog entry; it never aborts discovery for the workspace.
        return {}, FrontmatterStatus.MALFORMED
    if not isinstance(loaded, dict):
        return {}, FrontmatterStatus.MALFORMED
    return loaded, FrontmatterStatus.OK


def _fallback_name(path: Path) -> str:
    if path.name == "SKILL.md":
        return path.parent.name
    return path.stem


def _prose_description(text: str) -> str:
    """First substantive heading or line after any frontmatter block."""
    remainder = text
    if text.startswith("---"):
        match = _FRONTMATTER_RE.match(text)
        if match:
            remainder = text[match.end():]
    for line in remainder[:MAX_DESCRIPTION_SCAN_CHARS].splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading:
                return heading[:MAX_FIELD_CHARS]
            continue
        return stripped[:MAX_FIELD_CHARS]
    return ""


@dataclass(frozen=True)
class PrimitiveScan:
    """Validated metadata for one capability file, from a single bounded read.

    Reports facts only. Mapping vendor fields onto a loading policy is the
    adapter's job, so nothing here encodes Cursor or Claude semantics.
    """

    name: str
    description: str
    metadata: Mapping[str, object]
    status: FrontmatterStatus


def scan_primitive(path: Path) -> PrimitiveScan:
    """Read a capability file's metadata once, within the scan window."""
    raw = _read_metadata_window(path)
    if not raw:
        return PrimitiveScan(
            name=_fallback_name(path),
            description="",
            metadata=MappingProxyType({}),
            status=FrontmatterStatus.ABSENT,
        )

    text, unterminated_comment = _strip_html_comments(_strip_bom(raw))
    if unterminated_comment:
        return PrimitiveScan(
            name=_fallback_name(path),
            description="",
            metadata=MappingProxyType({}),
            status=FrontmatterStatus.TRUNCATED,
        )

    loaded, status = _load_frontmatter(text)
    metadata = _whitelist_metadata(loaded) if status is FrontmatterStatus.OK else {}

    description = str(metadata.get("description", ""))
    if not description and status is not FrontmatterStatus.TRUNCATED:
        description = _prose_description(text)

    return PrimitiveScan(
        name=str(metadata.get("name") or _fallback_name(path)),
        description=description,
        metadata=MappingProxyType(metadata),
        status=status,
    )


def parse_frontmatter_fields(path: Path) -> dict[str, str]:
    """Frontmatter name/description as strings, without the prose fallback."""
    scan = scan_primitive(path)
    return {
        key: value
        for key, value in scan.metadata.items()
        if key in _SCALAR_FIELDS and isinstance(value, str)
    }


def first_description(path: Path) -> str:
    """Catalog description: frontmatter, else first heading or line."""
    return scan_primitive(path).description


def primitive_name_from_path(path: Path) -> str:
    return scan_primitive(path).name


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
                scan = scan_primitive(md)
                ref = PrimitiveRef(
                    name=scan.name,
                    path=norm_path,
                    description=scan.description,
                    kind=kind,
                )
                bucket = {
                    "skill": manifest.skills,
                    "command": manifest.commands,
                    "agent": manifest.agents,
                    "rule": manifest.rules,
                }[kind]
                bucket.append(ref)
