# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Cursor harness adapter (.cursor/ + AGENTS.md)."""

from __future__ import annotations

from pathlib import Path
from typing import List

from app.services.harness.base import (
    AddOutcome,
    DiscoveryContext,
    HarnessAdapter,
    HarnessManifest,
    LoadingPolicy,
    PrimitiveFacts,
    PrimitiveScan,
    read_root_instructions,
    iter_pruned_files,
)
from app.services.workspace_io import WorkspacePath


def _rule_policy(scan: PrimitiveScan) -> tuple[LoadingPolicy, tuple[str, ...]]:
    """Map Cursor's frontmatter onto a generic loading policy.

    Cursor derives four activation modes from ``alwaysApply``, ``globs`` and
    ``description``; only the first is unconditional. Injecting every rule
    turned agent-requested and manual rules into always-on ones, which is how
    contradictory instructions ended up in the same prompt.
    """
    metadata = scan.metadata
    if metadata.get("alwaysApply") is True:
        return LoadingPolicy.EAGER, ()
    globs = metadata.get("globs") or ()
    if globs:
        return LoadingPolicy.SCOPED, tuple(globs)
    if metadata.get("description"):
        return LoadingPolicy.MODEL_DISCOVERABLE, ()
    return LoadingPolicy.EXPLICIT_ONLY, ()


class CursorAdapter(HarnessAdapter):
    type_id = "cursor"

    def detect(self, workspace: Path) -> int:
        score = 0
        if (workspace / ".cursor").is_dir():
            score += 80
        if (workspace / ".cursor" / "rules").is_dir():
            score += 10
        if (workspace / ".cursorrules").exists():
            score += 20
        if (workspace / "AGENTS.md").exists():
            score += 5
        return min(score, 100)

    def collect(self, workspace: Path, *, detected: bool, confidence: int) -> HarnessManifest:
        manifest = HarnessManifest(
            orchestration_type=self.type_id,
            detected=detected,
            confidence=confidence,
        )
        ctx = self._begin(workspace, manifest)
        try:
            return self._collect(ctx, manifest, workspace)
        finally:
            ctx.reader.close()

    def _collect(
        self,
        ctx: DiscoveryContext,
        manifest: HarnessManifest,
        workspace: Path,
    ) -> HarnessManifest:
        cursor_dir = WorkspacePath.parse(".cursor")

        # Rules: .cursor/rules/*.mdc. Only alwaysApply rules load eagerly; the
        # rest are catalogued so the model can pull them when they apply.
        # Plain .md here is intentionally skipped -- Cursor ignores it too,
        # since without frontmatter there is no activation to honour.
        rules_sections: List[tuple[str, str]] = []
        for wp in iter_pruned_files(
            ctx.reader, cursor_dir.child("rules"), "*.mdc", recursive=True
        ):
            result = ctx.consider(
                "rule",
                wp,
                lambda scan, wp=wp: PrimitiveFacts(
                    name=wp.stem,
                    description=scan.description,
                    policy=_rule_policy(scan)[0],
                    scope=_rule_policy(scan)[1],
                    source="cursor-rules",
                ),
            )
            if result.outcome is AddOutcome.CEILING:
                break
            if result.added and result.facts.policy is LoadingPolicy.EAGER:
                section = self._eager_rule_section(
                    ctx, f"Cursor Rule: {wp.stem}", wp
                )
                if section is not None:
                    rules_sections.append(section)

        # Legacy .cursorrules predates frontmatter, so it has no activation to
        # read and stays unconditional. Kept for backward compatibility only.
        legacy = WorkspacePath.parse(".cursorrules")
        legacy_result = ctx.consider(
            "rule",
            legacy,
            lambda scan: PrimitiveFacts(
                name=".cursorrules",
                description=scan.description,
                policy=LoadingPolicy.EAGER,
                source="legacy-cursorrules",
            ),
        )
        if legacy_result.added:
            legacy_section = self._eager_rule_section(
                ctx, "Cursor Rules (legacy .cursorrules)", legacy
            )
            if legacy_section is not None:
                rules_sections.append(legacy_section)

        # Agents: .cursor/agents/*.md
        for wp in iter_pruned_files(
            ctx.reader, cursor_dir.child("agents"), "*.md", recursive=False
        ):
            outcome = ctx.consider(
                "agent",
                wp,
                lambda scan, wp=wp: PrimitiveFacts(
                    name=wp.stem,
                    description=scan.description,
                ),
            ).outcome
            if outcome is AddOutcome.CEILING:
                break

        self._discover_primitives(ctx)

        # Hooks: parse and note; execution happens at tool/shell time via hooks.executor.
        from app.services.hooks import load_hooks_for_workspace

        cfg = load_hooks_for_workspace(str(workspace), reader=ctx.reader)
        manifest.notes.extend(cfg.diagnostics)
        if cfg.source != "none":
            if cfg.events:
                manifest.notes.append(
                    f"Loaded {cfg.source.title()} hooks "
                    f"({len(cfg.enabled_events)} events: "
                    f"{', '.join(cfg.enabled_events)})"
                )
            else:
                manifest.notes.append(
                    f"Detected {cfg.source.title()} hook configuration "
                    "(no runnable command hooks found)"
                )

        # AGENTS.md at the root, or inside .cursor/ (some workflows keep it there).
        agents_md_wp = WorkspacePath.parse("AGENTS.md")
        if not ctx.reader.exists(agents_md_wp):
            candidate = cursor_dir.child("AGENTS.md")
            if ctx.reader.exists(candidate):
                agents_md_wp = candidate
        has_agents_md = ctx.reader.exists(agents_md_wp)
        agents_md = read_root_instructions(ctx.reader, agents_md_wp) if has_agents_md else ""
        if has_agents_md:
            ctx.consider(
                "rule",
                agents_md_wp,
                lambda scan: PrimitiveFacts(
                    name=agents_md_wp.posix,
                    description="",
                    policy=LoadingPolicy.EAGER,
                    source="root-instructions",
                ),
            )
        self._discover_scoped_instructions(ctx)

        manifest.eager_context = self._assemble_eager(
            [("Project Agents (AGENTS.md)", agents_md)],
            rules_sections,
            manifest=manifest,
        )
        return manifest
