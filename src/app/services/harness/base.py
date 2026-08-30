# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Base types and helpers for harness adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterator, List, Mapping, Optional
import os
import re

import yaml

from app.services.workspace_io import (
    UnsafePathError,
    WorkspaceEntryKind,
    WorkspacePath,
    WorkspaceReader,
)
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


class AddOutcome(str, Enum):
    """Why a discovery candidate did or did not become a catalog entry.

    A boolean would conflate "already catalogued" with "no room left", and a
    duplicate encountered while a bucket is full would then stop traversal as
    though the workspace had overflowed.
    """

    ADDED = "added"
    DUPLICATE = "duplicate"  # already catalogued; keep going
    CEILING = "ceiling"  # bucket full and this candidate is genuine overflow
    REJECTED = "rejected"  # unreadable, unsafe or invalid; skipped and warned


ROOT_INSTRUCTIONS_TOO_LARGE = "ROOT_INSTRUCTIONS_TOO_LARGE"
ROOT_INSTRUCTIONS_UNREADABLE = "ROOT_INSTRUCTIONS_UNREADABLE"


class RootInstructionError(Exception):
    """A root instruction file could not be loaded as required.

    Carries its own stable error code so controllers report the cause rather
    than inferring one from the exception type. A permissions failure and an
    over-budget playbook are different problems for whoever has to fix them.
    """

    code = ROOT_INSTRUCTIONS_TOO_LARGE


class RootInstructionTooLarge(RootInstructionError):
    """The file loaded but exceeds the eager budget."""

    code = ROOT_INSTRUCTIONS_TOO_LARGE


class RootInstructionUnreadable(RootInstructionError):
    """The file could not be read safely at all."""

    code = ROOT_INSTRUCTIONS_UNREADABLE


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


def read_text_capped(
    reader: WorkspaceReader, wp: WorkspacePath, cap: int = MAX_EAGER_FILE_CHARS
) -> str:
    """Bounded read for content a clip does not invalidate, such as a README."""
    result = reader.read_capped(wp, cap)
    if result is None:
        return ""
    text, overflowed = result
    if overflowed:
        return text + f"\n\n... [truncated at {cap} chars]"
    return text


def read_eager_rule(
    reader: WorkspaceReader, wp: WorkspacePath, cap: int = MAX_EAGER_FILE_CHARS
) -> tuple[str, bool]:
    """Bounded read for a rule body: whole or dropped, never clipped.

    Half a rule is worse than no rule -- the model cannot tell the second half
    is missing, so it follows an instruction the author never wrote. The caller
    reports the omission instead.
    """
    result = reader.read_capped(wp, cap)
    if result is None:
        return "", False
    text, overflowed = result
    if overflowed:
        return "", True
    return text, False


def read_root_instructions(
    reader: WorkspaceReader,
    wp: WorkspacePath,
    *,
    cap: int = MAX_ROOT_INSTRUCTION_CHARS,
) -> str:
    """Load root AGENTS.md / CLAUDE.md in full; fail explicitly if it cannot be.

    Size and readability are separate failures with separate codes: telling an
    operator their playbook is too large when it is actually unreadable sends
    them to fix the wrong thing.
    """
    try:
        chunk, overflowed = reader.read_strict(wp, cap)
    except FileNotFoundError:
        return ""
    except (OSError, UnsafePathError) as exc:
        raise RootInstructionUnreadable(
            f"Cannot read root instructions at {wp}: {exc}"
        ) from exc
    if overflowed:
        raise RootInstructionTooLarge(
            f"Root instructions at {wp} exceed {cap} chars; "
            "refusing silent truncation"
        )
    return chunk


def _strip_bom(text: str) -> str:
    if text.startswith("\ufeff"):
        return text[1:]
    return text


def _strip_html_comments(text: str) -> tuple[str, bool]:
    """Remove complete comments; report an unterminated *leading* one.

    A leading comment that never closes means the comment runs past the scan
    window, so everything visible is comment body rather than metadata.

    An unterminated marker anywhere else is just prose. Frontmatter above it
    has already been read, and treating that as truncation would throw away a
    perfectly good scan because of a stray "<!--" further down the file.
    """
    leading = text.lstrip()
    leading_unterminated = leading.startswith("<!--") and not _HTML_COMMENT_RE.match(leading)
    stripped = _HTML_COMMENT_RE.sub("", text)
    return stripped.strip(), leading_unterminated


def _read_metadata_window(reader: WorkspaceReader, wp: WorkspacePath) -> Optional[str]:
    """At most ``MAX_FRONTMATTER_SCAN_CHARS`` from the file, or ``None``.

    Bounded at the handle rather than by slicing a full read, so an oversized
    capability file never costs more than the window. ``None`` means the file
    could not be read safely, which is not the same as an empty file: one is
    skipped, the other is catalogued with fallback metadata.
    """
    result = reader.read_capped(wp, MAX_FRONTMATTER_SCAN_CHARS)
    if result is None:
        return None
    return result[0]


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


def _fallback_name(wp: WorkspacePath) -> str:
    if wp.name == "SKILL.md":
        return wp.parent.name
    return wp.stem


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


def scan_primitive(
    reader: WorkspaceReader, wp: WorkspacePath
) -> Optional[PrimitiveScan]:
    """Metadata for one capability file, or ``None`` if it cannot be read.

    A single bounded read. ``None`` is reserved for unreadable or unsafe files;
    a readable file with no frontmatter still gets its filename/prose fallback.
    """
    raw = _read_metadata_window(reader, wp)
    if raw is None:
        return None
    if not raw:
        return PrimitiveScan(
            name=_fallback_name(wp),
            description="",
            metadata=MappingProxyType({}),
            status=FrontmatterStatus.ABSENT,
        )

    text, unterminated_comment = _strip_html_comments(_strip_bom(raw))
    if unterminated_comment:
        return PrimitiveScan(
            name=_fallback_name(wp),
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
        name=str(metadata.get("name") or _fallback_name(wp)),
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


def iter_workspace_files(
    reader: WorkspaceReader,
    root: WorkspacePath,
    *,
    recursive: bool = True,
) -> Iterator[WorkspacePath]:
    """Files under ``root`` in lexicographic path-component order.

    Depth-first over each directory's entries sorted by name, descending as
    each directory is met. That is exactly lexicographic order over path
    components, so output matches ``sorted()`` over the same paths while
    holding only the entries along the current frontier -- O(depth x directory
    width) rather than one entry per match in the tree.

    An explicit stack rather than recursion: the filesystem accepts far deeper
    trees than the interpreter's recursion limit (2,045 levels against 1,000 on
    Linux), and a recursive walker would abort discovery on one.

    Symlinks are skipped here as an optimization, not as the safety boundary --
    ``WorkspaceReader`` refuses them again at open time. The walker is not the
    containment authority, so a discovery path that never walks is equally
    protected.
    """
    # Entries are pushed reversed so they pop in sorted order, and files are
    # queued alongside directories so a subtree is exhausted at the exact point
    # its name sorts -- which is what makes this match sorted() output.
    stack: List[tuple[bool, WorkspacePath]] = [(True, root)]
    while stack:
        is_dir, current = stack.pop()
        if not is_dir:
            yield current
            continue
        frontier: List[tuple[bool, WorkspacePath]] = []
        for entry in reader.scandir(current):
            try:
                child = current.child(entry.name)
            except UnsafePathError as exc:
                logger.warning("Skipping entry with unsafe name: %s", exc)
                continue
            if entry.kind is WorkspaceEntryKind.DIRECTORY:
                if recursive and entry.name not in PRUNED_DIR_NAMES:
                    frontier.append((True, child))
            elif entry.kind is WorkspaceEntryKind.FILE:
                frontier.append((False, child))
        stack.extend(reversed(frontier))


def iter_pruned_files(
    reader: WorkspaceReader,
    root: WorkspacePath,
    file_glob: str,
    *,
    recursive: bool,
) -> Iterator[WorkspacePath]:
    """Files under ``root`` matching ``file_glob``, never entering pruned dirs.

    ``Path.glob("**/...")`` has already walked node_modules by the time its
    results could be filtered, so discovery goes through the bounded walker.
    """
    for wp in iter_workspace_files(reader, root, recursive=recursive):
        if fnmatch(wp.name, file_glob):
            yield wp


def _under_skills_root(directory: WorkspacePath) -> bool:
    """True when ``directory`` sits inside a ``<root>/skills`` tree."""
    parts = directory.parts
    return any(
        parts[i] in SKILL_ROOT_DIRS and parts[i + 1] == "skills"
        for i in range(len(parts) - 1)
    )


def iter_scoped_instructions(reader: WorkspaceReader) -> Iterator[WorkspacePath]:
    """Instruction files below the workspace root.

    Both harnesses support scoped instructions: Claude loads a nested CLAUDE.md
    when it touches that subtree, and .claude/CLAUDE.md is a project-level
    location in its own right. A stateless service cannot observe "when it
    touches", so these are catalogued for on-demand reading instead of injected
    -- which is the honest equivalent of lazy loading.
    """
    for wp in iter_workspace_files(reader, WorkspacePath()):
        if wp.name in INSTRUCTION_FILE_NAMES and len(wp.parts) > 1:
            yield wp  # root files are eager and handled by the adapter


def iter_skill_files(reader: WorkspaceReader) -> Iterator[WorkspacePath]:
    """Every SKILL.md under any capability root, at any depth.

    Covers category folders (``.cursor/skills/shipping/land-it/SKILL.md``) and
    monorepo packages (``apps/web/.cursor/skills/deploy/SKILL.md``) alike.
    """
    for wp in iter_workspace_files(reader, WorkspacePath()):
        if wp.name == "SKILL.md" and _under_skills_root(wp.parent):
            yield wp


def bucket_for(manifest: HarnessManifest, kind: str) -> List[PrimitiveRef]:
    """The manifest list that holds primitives of ``kind``."""
    return {
        "skill": manifest.skills,
        "command": manifest.commands,
        "agent": manifest.agents,
        "rule": manifest.rules,
    }[kind]


ALL_KINDS = ("skill", "command", "agent", "rule")


@dataclass(frozen=True)
class PrimitiveFacts:
    """The provider-specific half of a catalog entry.

    Deliberately excludes ``path`` and ``kind``. The transaction sets those
    itself from the candidate it validated, so an adapter cannot file an entry
    under a path it never opened or a kind it was not asked for.
    """

    name: str
    description: str = ""
    policy: LoadingPolicy = LoadingPolicy.MODEL_DISCOVERABLE
    scope: tuple[str, ...] = ()
    source: str = ""


@dataclass(frozen=True)
class ConsiderResult:
    """Outcome of one candidate, plus what was learned if it was added."""

    outcome: AddOutcome
    facts: Optional[PrimitiveFacts] = None
    scan: Optional[PrimitiveScan] = None

    @property
    def added(self) -> bool:
        return self.outcome is AddOutcome.ADDED


class DiscoveryContext:
    """Owns the whole candidate transaction for one ``collect()`` call.

    Every catalog entry in every adapter goes through :meth:`consider`. That is
    what makes the ordering guarantees structural instead of conventions each
    call site has to remember: a capped kind cannot cost a read, because the
    read happens after the ceiling check inside this one method.
    """

    def __init__(
        self,
        workspace: Path,
        manifest: HarnessManifest,
        reader: WorkspaceReader,
    ) -> None:
        self.workspace = workspace
        self.manifest = manifest
        self.reader = reader
        self.capped: set[str] = set()
        self.seen: set[str] = {
            ref.path.replace("\\", "/")
            for bucket in (manifest.skills, manifest.agents, manifest.commands, manifest.rules)
            for ref in bucket
        }

    def is_capped(self, kind: str) -> bool:
        """True once this kind has *confirmed* overflow, not merely filled."""
        return kind in self.capped

    def all_capped(self) -> bool:
        return all(kind in self.capped for kind in ALL_KINDS)

    def _is_full(self, kind: str) -> bool:
        return len(bucket_for(self.manifest, kind)) >= MAX_PRIMITIVES_PER_KIND

    def report_ceiling(self, kind: str) -> None:
        """Announce the per-kind ceiling once, however discovery reached it."""
        if kind in self.capped:
            return
        self.capped.add(kind)
        logger.warning(
            "Discovery ceiling reached for %ss (%d); further %ss ignored",
            kind,
            MAX_PRIMITIVES_PER_KIND,
            kind,
        )
        self.manifest.notes.append(
            f"Discovery ceiling reached: more than {MAX_PRIMITIVES_PER_KIND} "
            f"{kind}s found; the rest were ignored"
        )

    def consider(
        self,
        kind: str,
        wp: WorkspacePath,
        facts_for: Callable[[PrimitiveScan], PrimitiveFacts],
    ) -> ConsiderResult:
        """Normalize, deduplicate, check the ceiling, read, then append.

        The order is the point. Duplicates are settled before the ceiling so a
        path already catalogued is never mistaken for overflow and never stops
        traversal. The ceiling is settled before the read so the 201st
        candidate is reported without being opened.
        """
        if kind not in ALL_KINDS:
            raise ValueError(f"Unknown primitive kind: {kind!r}")

        norm_path = wp.posix
        if norm_path in self.seen:
            return ConsiderResult(AddOutcome.DUPLICATE)
        if self._is_full(kind):
            self.report_ceiling(kind)
            return ConsiderResult(AddOutcome.CEILING)

        scan = scan_primitive(self.reader, wp)
        if scan is None:
            # Unreadable or unsafe. Skipped rather than catalogued with a
            # filename guess: an entry the model cannot read is worse than no
            # entry, because it will try.
            logger.warning("Skipping unreadable or unsafe %s file %s", kind, norm_path)
            return ConsiderResult(AddOutcome.REJECTED)

        facts = facts_for(scan)
        bucket_for(self.manifest, kind).append(
            PrimitiveRef(
                name=facts.name,
                path=norm_path,
                description=facts.description,
                kind=kind,
                policy=facts.policy,
                scope=facts.scope,
                source=facts.source,
            )
        )
        self.seen.add(norm_path)
        return ConsiderResult(AddOutcome.ADDED, facts=facts, scan=scan)


class HarnessAdapter:
    """Base class for harness adapters."""

    type_id: str = "generic"

    def _begin(self, workspace: Path, manifest: HarnessManifest) -> DiscoveryContext:
        """Open the workspace reader and start the discovery transaction."""
        return DiscoveryContext(workspace, manifest, WorkspaceReader(workspace))

    def detect(self, workspace: Path) -> int:
        """Return a confidence score (0-100) that this adapter fits."""
        raise NotImplementedError

    def collect(self, workspace: Path, *, detected: bool, confidence: int) -> HarnessManifest:
        """Build the manifest of eager context + lazy primitives."""
        raise NotImplementedError

    def _eager_rule_section(
        self,
        ctx: "DiscoveryContext",
        title: str,
        wp: WorkspacePath,
    ) -> Optional[tuple[str, str]]:
        """An eager rule body ready for injection, or ``None`` if oversized.

        Only ever called after a candidate is ADDED, so content is never read
        for an entry that did not make the catalog.
        """
        body, overflowed = read_eager_rule(ctx.reader, wp)
        if overflowed:
            logger.warning(
                "Eager rule %s exceeds %d chars; omitted whole rather than truncated",
                wp,
                MAX_EAGER_FILE_CHARS,
            )
            ctx.manifest.notes.append(
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
            raise RootInstructionTooLarge(
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

    def _discover_scoped_instructions(self, ctx: DiscoveryContext) -> None:
        """Catalog nested AGENTS.md / CLAUDE.md as scoped, lazily-read rules.

        They live in the rules bucket rather than a bucket of their own: they
        are scoped instructions, the policy field already carries the meaning a
        separate bucket would have, and the API summary schema stays stable.
        """
        if ctx.is_capped("rule"):
            return  # confirmed overflow; walking the workspace would find nothing usable

        def facts_for(wp: WorkspacePath):
            directory = wp.parent.posix

            def build(scan: PrimitiveScan) -> PrimitiveFacts:
                return PrimitiveFacts(
                    name=wp.posix,
                    description=scan.description,
                    policy=LoadingPolicy.SCOPED,
                    scope=(f"{directory}/**",),
                    source="scoped-instructions",
                )

            return build

        for wp in iter_scoped_instructions(ctx.reader):
            # Stop on genuine overflow, but never silently: a scoped
            # instruction that vanishes without a note looks identical to one
            # that was never written.
            if ctx.consider("rule", wp, facts_for(wp)).outcome is AddOutcome.CEILING:
                break

    def _discover_primitives(self, ctx: DiscoveryContext) -> None:
        """Discover skills/commands/rules/agents across all capability roots."""
        if ctx.all_capped():
            return

        def build_for(kind: str) -> Callable[[PrimitiveScan], PrimitiveFacts]:
            def build(scan: PrimitiveScan) -> PrimitiveFacts:
                return PrimitiveFacts(
                    name=scan.name,
                    description=scan.description,
                    policy=(
                        LoadingPolicy.MODEL_DISCOVERABLE
                        if kind == "command"
                        else skill_policy(scan)
                    ),
                    source="discovery",
                )

            return build

        if not ctx.is_capped("skill"):
            for wp in iter_skill_files(ctx.reader):
                if ctx.consider("skill", wp, build_for("skill")).outcome is AddOutcome.CEILING:
                    break

        for subdir, file_glob, recursive, kind in DISCOVERY_PATTERNS:
            if ctx.is_capped(kind):
                continue  # already overflowed; do not even open the directory
            if ctx.all_capped():
                return
            try:
                root = WorkspacePath.parse(subdir)
            except UnsafePathError:  # pragma: no cover - patterns are constants
                continue
            for wp in iter_pruned_files(ctx.reader, root, file_glob, recursive=recursive):
                if ctx.consider(kind, wp, build_for(kind)).outcome is AddOutcome.CEILING:
                    break
