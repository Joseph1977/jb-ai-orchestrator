# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Harness adapter registry: detection + manifest collection."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from app.services.harness.base import HarnessAdapter, HarnessManifest
from app.services.harness.claude import ClaudeCodeAdapter
from app.services.harness.cursor import CursorAdapter
from app.services.harness.generic import GenericAdapter
from app.utils.logger import logger

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


def _render_catalog(manifest: HarnessManifest) -> str:
    """List lazily-loadable primitives (metadata + path only)."""
    sections: list[tuple[str, list]] = [
        ("Skills", manifest.skills),
        ("Commands", manifest.commands),
        ("Agents", manifest.agents),
        ("Rules", manifest.rules),
    ]
    lines: list[str] = []
    for title, refs in sections:
        if not refs:
            continue
        lines.append(f"### {title}")
        for r in refs:
            desc = f" — {r.description}" if r.description else ""
            lines.append(f"- `{r.name}` ({r.path}){desc}")
        lines.append("")
    if not lines:
        return ""
    header = (
        "## Available capabilities (NOT loaded yet)\n"
        f"{PROGRESSIVE_DISCLOSURE_INSTRUCTION}\n"
        "Paths are relative to the workspace root."
    )
    return header + "\n\n" + "\n".join(lines).strip()


def render_system_prompt(
    manifest: HarnessManifest, *, base: Optional[str] = None
) -> Optional[str]:
    """Build a system prompt: optional base, eager orchestration context, lazy catalog."""
    parts: list[str] = []
    if base:
        parts.append(base.strip())
    if manifest.eager_context:
        parts.append(manifest.eager_context.strip())
    catalog = _render_catalog(manifest)
    if catalog:
        parts.append(catalog)
    joined = "\n\n".join(p for p in parts if p)
    return joined or None


MAX_SUBAGENT_CATALOG_CHARS = 4000


def render_subagent_catalog(manifest: HarnessManifest) -> str:
    """Concise primitive listing for nested subagents (paths only, no full bodies)."""
    catalog = _render_catalog(manifest)
    if not catalog:
        return ""
    if len(catalog) > MAX_SUBAGENT_CATALOG_CHARS:
        return catalog[:MAX_SUBAGENT_CATALOG_CHARS] + "\n\n... [catalog truncated]"
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
