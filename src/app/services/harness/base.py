# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Base types and helpers for harness adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, List, Mapping, Optional
import os
import re

import yaml

from app.utils.logger import logger

DEFAULT_EAGER_TOTAL_CHARS = 24000
EAGER_BUDGET_ENV_VAR = "HARNESS_EAGER_BUDGET_CHARS"


def eager_budget_from_env(default: int = DEFAULT_EAGER_TOTAL_CHARS) -> int:
    """Read the configured eager budget, falling back on unusable values.

    A bad value must not stop the service booting, so it is reported and
    ignored rather than raised at import time.
    """
    raw = os.getenv(EAGER_BUDGET_ENV_VAR)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r", EAGER_BUDGET_ENV_VAR, raw)
        return default
    if value <= 0:
        logger.warning("Ignoring non-positive %s=%r", EAGER_BUDGET_ENV_VAR, raw)
        return default
    return value


# Caps to keep eagerly-injected context from blowing the token budget.
MAX_EAGER_FILE_CHARS = 6000
# Total eager budget. Operators can raise it rather than being forced to split
# a large playbook; root instructions are measured against this same number.
MAX_EAGER_TOTAL_CHARS = eager_budget_from_env()
# Root playbook files (AGENTS.md / CLAUDE.md) must load in full or fail
# explicitly. This is the eager budget, not a separate larger limit: accepting
# a file up to 512,000 chars and then silently clipping it at 24,000 was a
# contradiction, and "load it fully" is not viable for a 400,000-char file
# either -- that is a ~100,000-token system prompt.
MAX_ROOT_INSTRUCTION_CHARS = MAX_EAGER_TOTAL_CHARS

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

# Directories never worth descending into when looking for capability files.
# Pruned during the walk, not filtered afterwards: Path.glob("**/...") would
# already have traversed node_modules by the time we could discard the results.
PRUNED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".mypy_cache",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "target",
        "venv",
        ".venv",
        "vendor",
    }
)

# Declarative capability roots. A "<root>/skills" directory anywhere in the
# tree is a skills root, walked recursively so category folders and monorepo
# packages are both covered.
SKILL_ROOT_DIRS = (".agents", ".claude", ".codex", ".cursor")

# Instruction files that carry scope by their location in the tree.
INSTRUCTION_FILE_NAMES = ("AGENTS.md", "CLAUDE.md")

# Where to look, as (subdirectory, filename glob, recursive, kind). Split into
# a directory and a filename pattern rather than one "**" glob string so the
# recursive cases can be walked with pruning instead of by Path.glob.
DISCOVERY_PATTERNS = (
    (".agents/skills", "*.md", False, "skill"),
    (".claude/skills", "*.md", False, "skill"),
    (".codex/skills", "*.md", False, "skill"),
    (".cursor/skills", "*.md", False, "skill"),
    (".cursor/commands", "*.md", False, "command"),
    (".claude/commands", "*.md", False, "command"),
    (".cursor/agents", "*.md", False, "agent"),
    (".claude/agents", "*.md", False, "agent"),
    (".claude/rules", "*.md", True, "rule"),
    (".cursor/rules", "*.mdc", True, "rule"),
    ("skills", "*.md", True, "skill"),
    ("agents", "*.md", True, "agent"),
    ("commands", "*.md", True, "command"),
    ("rules", "*.md", True, "rule"),
)

# Safety ceiling against runaway discovery in an unfamiliar workspace. This is
# not the token control -- the rendered-catalog budget in the registry is.
MAX_PRIMITIVES_PER_KIND = 200

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


def read_capped(path: Path, cap: int) -> tuple[str, bool]:
    """Read at most ``cap + 1`` characters; report whether the file overflowed.

    Bounded at the handle. Reading the file in full and slicing afterwards
    would let a hostile multi-gigabyte rule exhaust memory before any cap could
    apply. The one extra character is what separates "exactly at the cap" from
    "longer than the cap" without a second metadata lookup.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            chunk = handle.read(cap + 1)
    except OSError:
        return "", False
    if len(chunk) > cap:
        return chunk[:cap], True
    return chunk, False


def read_text_capped(path: Path, cap: int = MAX_EAGER_FILE_CHARS) -> str:
    """Bounded read for content a clip does not invalidate, such as a README."""
    text, overflowed = read_capped(path, cap)
    if overflowed:
        return text + f"\n\n... [truncated at {cap} chars]"
    return text


def read_eager_rule(path: Path, cap: int = MAX_EAGER_FILE_CHARS) -> tuple[str, bool]:
    """Bounded read for a rule body: whole or dropped, never clipped.

    Half a rule is worse than no rule -- the model cannot tell the second half
    is missing, so it follows an instruction the author never wrote. The caller
    reports the omission instead.
    """
    text, overflowed = read_capped(path, cap)
    if overflowed:
        return "", True
    return text, False


def read_root_instructions(path: Path, *, cap: int = MAX_ROOT_INSTRUCTION_CHARS) -> str:
    """Load root AGENTS.md / CLAUDE.md in full; fail explicitly if too large."""
    if not path.exists():
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            chunk = handle.read(cap + 1)
    except OSError as exc:
        raise RootInstructionError(f"Cannot read root instructions at {path}: {exc}") from exc
    if len(chunk) > cap:
        raise RootInstructionError(
            f"Root instructions at {path} exceed {cap} chars; "
            "refusing silent truncation"
        )
    return chunk


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


def skill_policy(scan: PrimitiveScan) -> LoadingPolicy:
    """Loading policy for a model-selectable capability.

    ``disable-model-invocation`` is the cross-vendor opt-out: the capability
    stays invocable but must never be auto-selected, so it is withheld from the
    model-facing catalog.

    Commands are deliberately not routed through here. A slash command is
    reached by name, so withholding it from the catalog would hide it from the
    only mechanism that invokes it; commands stay model-discoverable whatever
    their frontmatter says.
    """
    if scan.metadata.get("disable-model-invocation") is True:
        return LoadingPolicy.EXPLICIT_ONLY
    return LoadingPolicy.MODEL_DISCOVERABLE


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


def walk_pruned(root: Path) -> Iterator[tuple[Path, list[str]]]:
    """Walk ``root`` top-down, pruning heavy directories before descending.

    Yields ``(directory, filenames)`` with both directory and file order
    sorted, so discovery is deterministic across platforms.
    """
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNED_DIR_NAMES)
        yield Path(dirpath), sorted(filenames)


def iter_pruned_files(root: Path, file_glob: str, *, recursive: bool) -> Iterator[Path]:
    """Files under ``root`` matching ``file_glob``, never entering pruned dirs.

    ``Path.glob("**/...")`` has already walked node_modules by the time its
    results could be filtered, so recursive discovery goes through the pruning
    walker instead. Results are sorted so ordering matches the previous
    ``sorted(workspace.glob(...))`` behaviour exactly.
    """
    if not root.is_dir():
        return
    if not recursive:
        yield from sorted(p for p in root.glob(file_glob) if p.is_file())
        return
    found: List[Path] = []
    for directory, filenames in walk_pruned(root):
        for name in filenames:
            if fnmatch(name, file_glob):
                found.append(directory / name)
    yield from sorted(found)


def _under_skills_root(directory: Path, workspace: Path) -> bool:
    """True when ``directory`` sits inside a ``<root>/skills`` tree."""
    try:
        parts = directory.relative_to(workspace).parts
    except ValueError:
        return False
    return any(
        parts[i] in SKILL_ROOT_DIRS and parts[i + 1] == "skills"
        for i in range(len(parts) - 1)
    )


def iter_scoped_instructions(workspace: Path) -> Iterator[Path]:
    """Instruction files below the workspace root.

    Both harnesses support scoped instructions: Claude loads a nested CLAUDE.md
    when it touches that subtree, and .claude/CLAUDE.md is a project-level
    location in its own right. A stateless service cannot observe "when it
    touches", so these are catalogued for on-demand reading instead of injected
    -- which is the honest equivalent of lazy loading.
    """
    for directory, filenames in walk_pruned(workspace):
        if directory == workspace:
            continue  # root files are eager and handled by the adapter
        for name in INSTRUCTION_FILE_NAMES:
            if name in filenames:
                yield directory / name


def iter_skill_files(workspace: Path) -> Iterator[Path]:
    """Every SKILL.md under any capability root, at any depth.

    Covers category folders (``.cursor/skills/shipping/land-it/SKILL.md``) and
    monorepo packages (``apps/web/.cursor/skills/deploy/SKILL.md``) alike.
    """
    for directory, filenames in walk_pruned(workspace):
        if "SKILL.md" in filenames and _under_skills_root(directory, workspace):
            yield directory / "SKILL.md"


def bucket_for(manifest: HarnessManifest, kind: str) -> List[PrimitiveRef]:
    """The manifest list that holds primitives of ``kind``."""
    return {
        "skill": manifest.skills,
        "command": manifest.commands,
        "agent": manifest.agents,
        "rule": manifest.rules,
    }[kind]


class HarnessAdapter:
    """Base class for harness adapters."""

    type_id: str = "generic"

    def _report_ceiling(
        self,
        manifest: HarnessManifest,
        kind: str,
        capped: set[str],
    ) -> None:
        """Announce the per-kind ceiling once, however discovery reached it."""
        if kind in capped:
            return
        capped.add(kind)
        logger.warning(
            "Discovery ceiling reached for %ss (%d); further %ss ignored",
            kind,
            MAX_PRIMITIVES_PER_KIND,
            kind,
        )
        manifest.notes.append(
            f"Discovery ceiling reached: more than {MAX_PRIMITIVES_PER_KIND} "
            f"{kind}s found; the rest were ignored"
        )

    def _append_primitive(
        self,
        manifest: HarnessManifest,
        kind: str,
        ref: PrimitiveRef,
        capped: set[str],
    ) -> bool:
        """Append within the ceiling, reporting the first entry it refuses.

        Every path that adds a primitive goes through here. An adapter that
        appended directly would enumerate without limit, leaving the safety
        ceiling covering only the part of discovery that happened to use it.
        """
        bucket = bucket_for(manifest, kind)
        if len(bucket) >= MAX_PRIMITIVES_PER_KIND:
            self._report_ceiling(manifest, kind, capped)
            return False
        bucket.append(ref)
        return True

    def detect(self, workspace: Path) -> int:
        """Return a confidence score (0-100) that this adapter fits."""
        raise NotImplementedError

    def collect(self, workspace: Path, *, detected: bool, confidence: int) -> HarnessManifest:
        """Build the manifest of eager context + lazy primitives."""
        raise NotImplementedError

    def _eager_rule_section(
        self,
        title: str,
        path: Path,
        manifest: Optional[HarnessManifest] = None,
    ) -> Optional[tuple[str, str]]:
        """An eager rule body ready for injection, or ``None`` if oversized.

        Keeps the whole-or-drop guarantee at the point of reading, so no caller
        can accidentally inject a clipped rule.
        """
        body, overflowed = read_eager_rule(path)
        if overflowed:
            logger.warning(
                "Eager rule %s exceeds %d chars; omitted whole rather than truncated",
                path,
                MAX_EAGER_FILE_CHARS,
            )
            if manifest is not None:
                manifest.notes.append(
                    f"Rule '{title}' exceeds {MAX_EAGER_FILE_CHARS} chars; "
                    "omitted from eager context rather than truncated"
                )
            return None
        if not body:
            return None
        return (title, body)

    def _assemble_eager(
        self,
        mandatory: List[tuple[str, str]],
        optional: List[tuple[str, str]] = (),
        *,
        budget: int = None,
        manifest: Optional[HarnessManifest] = None,
    ) -> str:
        """Assemble eager context: root instructions first, rules if they fit.

        Root instructions are one mandatory allocation. They load whole or the
        collection fails; truncating a playbook silently is how a rule the user
        relies on disappears without trace.

        Optional entries then take what is left, each included whole or dropped
        whole. Half a rule is worse than no rule, because the model cannot tell
        that the second half is missing.
        """
        budget = MAX_EAGER_TOTAL_CHARS if budget is None else budget
        blocks: List[str] = []
        used = 0
        for title, body in mandatory:
            if not body:
                continue
            block = f"## {title}\n\n{body}".strip()
            blocks.append(block)
            used += len(block) + 2

        if used > budget:
            raise RootInstructionError(
                f"Root instructions total {used} chars, over the eager budget of "
                f"{budget}. Split them, or raise HARNESS_EAGER_BUDGET_CHARS; "
                "refusing to truncate them silently."
            )

        omitted: List[str] = []
        for title, body in optional:
            if not body:
                continue
            block = f"## {title}\n\n{body}".strip()
            if used + len(block) + 2 > budget:
                omitted.append(title)
                continue
            blocks.append(block)
            used += len(block) + 2

        if omitted:
            detail = ", ".join(omitted)
            logger.warning("Eager budget reached; omitted %s", detail)
            if manifest is not None:
                manifest.notes.append(f"Eager budget reached; omitted {detail}")
        return "\n\n".join(blocks)

    def _discover_scoped_instructions(
        self,
        workspace: Path,
        manifest: HarnessManifest,
        capped: Optional[set[str]] = None,
    ) -> None:
        """Catalog nested AGENTS.md / CLAUDE.md as scoped, lazily-read rules.

        They live in the rules bucket rather than a bucket of their own: they
        are scoped instructions, the policy field already carries the meaning a
        separate bucket would have, and the API summary schema stays stable.
        """
        seen_paths: set[str] = {
            ref.path.replace("\\", "/")
            for bucket in (manifest.skills, manifest.agents, manifest.commands, manifest.rules)
            for ref in bucket
        }
        capped = set() if capped is None else capped
        for path in iter_scoped_instructions(workspace):
            norm_path = normalize_catalog_path(path, workspace)
            if norm_path in seen_paths:
                continue
            directory = str(Path(norm_path).parent).replace("\\", "/")
            scan = scan_primitive(path)
            added = self._append_primitive(
                manifest,
                "rule",
                PrimitiveRef(
                    name=norm_path,
                    path=norm_path,
                    description=scan.description,
                    kind="rule",
                    policy=LoadingPolicy.SCOPED,
                    scope=(f"{directory}/**",),
                    source="scoped-instructions",
                ),
                capped,
            )
            if not added:
                # The rule bucket is full. Stop walking, but never silently:
                # a scoped instruction that vanishes without a note looks
                # identical to one that was never written.
                break
            seen_paths.add(norm_path)

    def _discover_primitives(
        self,
        workspace: Path,
        manifest: HarnessManifest,
        capped: Optional[set[str]] = None,
    ) -> None:
        """Discover skills/commands/rules/agents across all capability roots."""
        capped = set() if capped is None else capped
        # Adapters may already have catalogued some of these paths explicitly;
        # seed from the manifest so discovery never lists a primitive twice.
        seen_paths: set[str] = {
            ref.path.replace("\\", "/")
            for bucket in (manifest.skills, manifest.agents, manifest.commands, manifest.rules)
            for ref in bucket
        }

        def add(md: Path, kind: str) -> None:
            if not md.is_file():
                return
            norm_path = normalize_catalog_path(md, workspace)
            if norm_path in seen_paths:
                return
            scan = scan_primitive(md)
            policy = (
                LoadingPolicy.MODEL_DISCOVERABLE
                if kind == "command"
                else skill_policy(scan)
            )
            added = self._append_primitive(
                manifest,
                kind,
                PrimitiveRef(
                    name=scan.name,
                    path=norm_path,
                    description=scan.description,
                    kind=kind,
                    policy=policy,
                    source="discovery",
                ),
                capped,
            )
            if added:
                seen_paths.add(norm_path)

        for skill_md in iter_skill_files(workspace):
            add(skill_md, "skill")
        for subdir, file_glob, recursive, kind in DISCOVERY_PATTERNS:
            for md in iter_pruned_files(workspace / subdir, file_glob, recursive=recursive):
                add(md, kind)
