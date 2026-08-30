# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Harness adapter registry: detection + manifest collection."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from app.services.harness.base import HarnessAdapter, HarnessManifest, LoadingPolicy
from app.services.harness.claude import ClaudeCodeAdapter
from app.services.harness.cursor import CursorAdapter
from app.services.harness.generic import GenericAdapter
from app.utils.logger import logger

# Total rendered-catalog budgets. A per-kind entry ceiling is a safety net
# against runaway discovery, not a token control: 200 entries per kind is still
# roughly 800 prompt lines, so the character budget is what actually bounds the
# prompt. The two surfaces get different budgets but share one allocator.
MAX_CATALOG_CHARS = 12000
MAX_SUBAGENT_CATALOG_CHARS = 4000

# Order matters only for stable tie-breaking; scoring drives selection.
_ADAPTERS: Dict[str, HarnessAdapter] = {
    CursorAdapter.type_id: CursorAdapter(),
    ClaudeCodeAdapter.type_id: ClaudeCodeAdapter(),
    GenericAdapter.type_id: GenericAdapter(),
}

_ALIASES = {
    "cursor": "cursor",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "claudecode": "claude-code",
    "generic": "generic",
    "auto": "",
}

PROGRESSIVE_DISCLOSURE_INSTRUCTION = (
    "Catalog entries list names, relative paths, and short descriptions only. "
    "They are references — not loaded content. Before following any skill, command, "
    "rule, or agent entry, read the referenced file with your available filesystem "
    "tools and apply what you read."
)


def get_adapter(orchestration_type: str) -> Optional[HarnessAdapter]:
    key = _ALIASES.get((orchestration_type or "").strip().lower(), (orchestration_type or "").strip().lower())
    return _ADAPTERS.get(key)


def detect_adapter(workspace: Path) -> Tuple[HarnessAdapter, int]:
    """Return the best-fitting adapter and its confidence score."""
    best: Tuple[HarnessAdapter, int] = (_ADAPTERS["generic"], 0)
    for adapter in _ADAPTERS.values():
        try:
            score = adapter.detect(workspace)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Adapter %s detect() failed: %s", adapter.type_id, exc)
            score = 0
        if score > best[1]:
            best = (adapter, score)
    return best


def collect_manifest(
    workspace_path: str,
    orchestration_type: Optional[str] = None,
) -> HarnessManifest:
    """Detect (or honour explicit) orchestration type and build its manifest."""
    workspace = Path(workspace_path)

    explicit = (orchestration_type or "").strip()
    if explicit and _ALIASES.get(explicit.lower(), explicit.lower()):
        adapter = get_adapter(explicit)
        if adapter is None:
            raise ValueError(
                f"Unknown orchestrationType '{orchestration_type}'. "
                f"Supported: {', '.join(sorted(_ADAPTERS))}"
            )
        confidence = adapter.detect(workspace)
        logger.info("Using explicit orchestration type '%s'", adapter.type_id)
        return adapter.collect(workspace, detected=False, confidence=confidence)

    adapter, confidence = detect_adapter(workspace)
    logger.info(
        "Auto-detected orchestration type '%s' (confidence=%s)",
        adapter.type_id,
        confidence,
    )
    return adapter.collect(workspace, detected=True, confidence=confidence)


def _is_catalogued(ref) -> bool:
    """Whether a primitive belongs in the model-selectable catalog."""
    if ref.policy is LoadingPolicy.EAGER:
        # Already in the prompt verbatim; listing it again is a double charge.
        return False
    if ref.policy is LoadingPolicy.EXPLICIT_ONLY:
        # Labelling an entry "manual only" does not stop the model choosing it,
        # so it is omitted instead. Commands are exempt: they are user-invoked
        # by nature, and the model still has to know they exist to answer /help.
        return ref.kind == "command"
    return True


def _entry_line(ref) -> str:
    scope = f" [applies to: {', '.join(ref.scope)}]" if ref.scope else ""
    desc = f" — {ref.description}" if ref.description else ""
    return f"- `{ref.name}` ({ref.path}){scope}{desc}"


# "### Title\n" plus the blank line that follows a section.
_SECTION_OVERHEAD = 6
# Room reserved for "- ... N more not shown (catalog budget reached)".
_OMISSION_NOTICE_CHARS = 56


def _fill(entry_lines: list[str], available: int) -> list[str]:
    kept: list[str] = []
    for line in entry_lines:
        cost = len(line) + 1
        if cost > available:
            break
        kept.append(line)
        available -= cost
    return kept


def _allocate(demands: Dict[str, int], budget: int) -> Dict[str, int]:
    """Max-min fair split of ``budget`` across kinds.

    Every kind gets an equal share; those needing less release the surplus,
    which is redistributed until the budget or the demand runs out. Keeps a
    single crowded kind from consuming the catalog, and is order-independent.
    """
    allocation = {kind: 0 for kind in demands}
    pending = {kind: need for kind, need in demands.items() if need > 0}
    remaining = budget
    while pending and remaining > 0:
        share = remaining // len(pending)
        if share == 0:
            break
        for kind in sorted(pending):
            take = min(share, pending[kind])
            allocation[kind] += take
            pending[kind] -= take
            remaining -= take
        pending = {kind: need for kind, need in pending.items() if need > 0}
    return allocation


def _render_catalog(
    manifest: HarnessManifest, *, budget: int
) -> Tuple[str, Dict[str, int]]:
    """Render lazily-loadable primitives within a total character budget.

    Returns the text and a per-kind count of entries the budget excluded. The
    budget is shared out fairly rather than applied as a tail cut, so a large
    catalog no longer loses whichever kinds happen to render last.
    """
    sections: list[tuple[str, list]] = [
        ("Skills", manifest.skills),
        ("Commands", manifest.commands),
        ("Agents", manifest.agents),
        ("Rules", manifest.rules),
    ]
    visible = {
        title: [ref for ref in refs if _is_catalogued(ref)] for title, refs in sections
    }
    rendered = {
        title: [_entry_line(ref) for ref in refs] for title, refs in visible.items()
    }
    demands = {
        title: sum(len(line) + 1 for line in lines) + _SECTION_OVERHEAD + len(title)
        for title, lines in rendered.items()
        if lines
    }
    if not demands:
        return "", {}

    header = (
        "## Available capabilities (NOT loaded yet)\n"
        f"{PROGRESSIVE_DISCLOSURE_INSTRUCTION}\n"
        "Paths are relative to the workspace root."
    )
    # The budget covers everything we emit, header included, so the caller's
    # number is the real ceiling rather than the entry lines alone.
    allocation = _allocate(demands, max(budget - len(header) - 2, 0))

    lines: list[str] = []
    omitted: Dict[str, int] = {}
    for title, entry_lines in rendered.items():
        if not entry_lines:
            continue
        available = allocation.get(title, 0) - (_SECTION_OVERHEAD + len(title))
        kept = _fill(entry_lines, available)
        if len(kept) < len(entry_lines):
            # Re-fill leaving room for the notice, which is itself rendered.
            kept = _fill(entry_lines, available - _OMISSION_NOTICE_CHARS)
        skipped = len(entry_lines) - len(kept)
        if skipped:
            omitted[title.lower()] = skipped
        if not kept:
            continue
        lines.append(f"### {title}")
        lines.extend(kept)
        if skipped:
            lines.append(f"- ... {skipped} more not shown (catalog budget reached)")
        lines.append("")

    if not lines:
        return "", omitted
    return header + "\n\n" + "\n".join(lines).strip(), omitted


def _report_omissions(manifest: HarnessManifest, omitted: Dict[str, int], where: str) -> None:
    if not omitted:
        return
    detail = ", ".join(f"{count} {kind}" for kind, count in sorted(omitted.items()))
    logger.warning("Catalog budget reached for %s; omitted %s", where, detail)
    manifest.notes.append(f"Catalog budget reached ({where}); omitted {detail}")


def render_system_prompt(
    manifest: HarnessManifest, *, base: Optional[str] = None
) -> Optional[str]:
    """Build a system prompt: optional base, eager orchestration context, lazy catalog."""
    parts: list[str] = []
    if base:
        parts.append(base.strip())
    if manifest.eager_context:
        parts.append(manifest.eager_context.strip())
    catalog, omitted = _render_catalog(manifest, budget=MAX_CATALOG_CHARS)
    _report_omissions(manifest, omitted, "system prompt")
    if catalog:
        parts.append(catalog)
    joined = "\n\n".join(p for p in parts if p)
    return joined or None


def render_subagent_catalog(manifest: HarnessManifest) -> str:
    """Concise primitive listing for nested subagents (paths only, no full bodies)."""
    catalog, omitted = _render_catalog(manifest, budget=MAX_SUBAGENT_CATALOG_CHARS)
    if omitted:
        detail = ", ".join(f"{count} {kind}" for kind, count in sorted(omitted.items()))
        logger.warning("Subagent catalog budget reached; omitted %s", detail)
    return catalog


def enrich_system_prompt(
    base_prompt: Optional[str],
    *,
    workspace_path: Optional[str] = None,
    manifest: Optional[HarnessManifest] = None,
    user_prompt: Optional[str] = None,
    active_command: Optional[str] = None,
) -> Optional[str]:
    """Return base prompt unchanged — lazy harness never injects skill/memory bodies."""
    if base_prompt and base_prompt.strip():
        return base_prompt.strip()
    return None
