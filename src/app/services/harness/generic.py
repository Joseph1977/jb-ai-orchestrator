# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Generic harness adapter.

Fallback when no known orchestration type is detected. Loads AGENTS.md (root or
inside any dotted config dir) and README as the base playbook, and indexes any
markdown that looks like a skill/agent for lazy loading.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from app.services.harness.base import (
    HarnessAdapter,
    HarnessManifest,
    LoadingPolicy,
    PrimitiveRef,
    first_description,
    normalize_catalog_path,
    read_root_instructions,
    read_text_capped,
    rel,
)


class GenericAdapter(HarnessAdapter):
    type_id = "generic"

    def detect(self, workspace: Path) -> int:
        # Generic always applies as a floor, but with low confidence so any
        # specific adapter wins when present.
        if (workspace / "AGENTS.md").exists():
            return 15
        return 5

    def _find_agents_md(self, workspace: Path) -> Path | None:
        root = workspace / "AGENTS.md"
        if root.exists():
            return root
        # AGENTS.md inside a dotted config dir (e.g. .cursor/AGENTS.md).
        for dotted in sorted(workspace.glob(".*/AGENTS.md")):
            return dotted
        return None

    def collect(self, workspace: Path, *, detected: bool, confidence: int) -> HarnessManifest:
        manifest = HarnessManifest(
            orchestration_type=self.type_id,
            detected=detected,
            confidence=confidence,
        )

        agents_md_path = self._find_agents_md(workspace)
        agents_md = read_root_instructions(agents_md_path) if agents_md_path else ""

        readme = ""
        for name in ("README.md", "readme.md", "README", "README.MD"):
            candidate = workspace / name
            if candidate.exists():
                readme = read_text_capped(candidate)
                break

        # Index primitives via shared discovery (flat + dotted paths).
        self._discover_primitives(workspace, manifest)
        if agents_md_path is not None:
            manifest.rules.append(
                PrimitiveRef(
                    name=rel(agents_md_path, workspace),
                    path=normalize_catalog_path(agents_md_path, workspace),
                    description="",
                    kind="rule",
                    policy=LoadingPolicy.EAGER,
                    source="root-instructions",
                )
            )
        self._discover_scoped_instructions(workspace, manifest)

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
