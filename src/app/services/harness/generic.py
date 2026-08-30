# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Generic harness adapter.

Fallback when no known orchestration type is detected. Loads AGENTS.md (root or
inside any dotted config dir) and README as the base playbook, and indexes any
markdown that looks like a skill/agent for lazy loading.
"""

from __future__ import annotations

from pathlib import Path

from app.services.harness.base import (
    DiscoveryContext,
    HarnessAdapter,
    HarnessManifest,
    LoadingPolicy,
    PrimitiveFacts,
    read_root_instructions,
    read_text_capped,
)
from app.services.workspace_io import (
    UnsafePathError,
    WorkspaceEntryKind,
    WorkspacePath,
)


class GenericAdapter(HarnessAdapter):
    type_id = "generic"

    def detect(self, workspace: Path) -> int:
        # Generic always applies as a floor, but with low confidence so any
        # specific adapter wins when present.
        if (workspace / "AGENTS.md").exists():
            return 15
        return 5

    def _find_agents_md(self, ctx: DiscoveryContext) -> WorkspacePath | None:
        root = WorkspacePath.parse("AGENTS.md")
        if ctx.reader.exists(root):
            return root
        # AGENTS.md inside a dotted config dir (e.g. .cursor/AGENTS.md).
        for entry in ctx.reader.scandir(WorkspacePath()):
            if (
                not entry.name.startswith(".")
                or entry.kind is not WorkspaceEntryKind.DIRECTORY
            ):
                continue
            try:
                candidate = WorkspacePath().child(entry.name).child("AGENTS.md")
            except UnsafePathError:
                continue
            if ctx.reader.exists(candidate):
                return candidate
        return None

    def collect(self, workspace: Path, *, detected: bool, confidence: int) -> HarnessManifest:
        manifest = HarnessManifest(
            orchestration_type=self.type_id,
            detected=detected,
            confidence=confidence,
        )
        ctx = self._begin(workspace, manifest)
        try:
            return self._collect(ctx, manifest)
        finally:
            ctx.reader.close()

    def _collect(self, ctx: DiscoveryContext, manifest: HarnessManifest) -> HarnessManifest:
        agents_md_wp = self._find_agents_md(ctx)
        agents_md = read_root_instructions(ctx.reader, agents_md_wp) if agents_md_wp else ""

        readme = ""
        for name in ("README.md", "readme.md", "README", "README.MD"):
            candidate = WorkspacePath.parse(name)
            if ctx.reader.exists(candidate):
                readme = read_text_capped(ctx.reader, candidate)
                break

        # Index primitives via shared discovery (flat + dotted paths).
        self._discover_primitives(ctx)
        if agents_md_wp is not None:
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
            [("README", readme)],
            manifest=manifest,
        )
        if not agents_md and not readme:
            manifest.notes.append(
                "No AGENTS.md/README found; running with minimal base context."
            )
        return manifest
