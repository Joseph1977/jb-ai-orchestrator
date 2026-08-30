# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Cursor harness adapter (.cursor/ + AGENTS.md)."""

from __future__ import annotations

from pathlib import Path
from typing import List

from app.services.harness.base import (
    HarnessAdapter,
    HarnessManifest,
    LoadingPolicy,
    PrimitiveRef,
    PrimitiveScan,
    first_description,
    read_root_instructions,
    normalize_catalog_path,
    rel,
    iter_pruned_files,
    scan_primitive,
)


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
        cursor_dir = workspace / ".cursor"
        capped: set[str] = set()

        # Rules: .cursor/rules/*.mdc. Only alwaysApply rules load eagerly; the
        # rest are catalogued so the model can pull them when they apply.
        # Plain .md here is intentionally skipped -- Cursor ignores it too,
        # since without frontmatter there is no activation to honour.
        rules_sections: List[tuple[str, str]] = []
        rules_dir = cursor_dir / "rules"
        if rules_dir.is_dir():
            for rule_file in iter_pruned_files(
                rules_dir, "*.mdc", recursive=True, workspace=workspace
            ):
                if self._kind_is_full(manifest, "rule"):
                    self._report_ceiling(manifest, "rule", capped)
                    break
                scan = scan_primitive(rule_file)
                policy, scope = _rule_policy(scan)
                added = self._append_primitive(
                    manifest,
                    "rule",
                    PrimitiveRef(
                        name=rule_file.stem,
                        path=normalize_catalog_path(rule_file, workspace),
                        description=scan.description,
                        kind="rule",
                        policy=policy,
                        scope=scope,
                        source="cursor-rules",
                    ),
                    capped,
                )
                if added and policy is LoadingPolicy.EAGER:
                    section = self._eager_rule_section(
                        f"Cursor Rule: {rule_file.stem}", rule_file, manifest
                    )
                    if section is not None:
                        rules_sections.append(section)

        # Legacy .cursorrules predates frontmatter, so it has no activation to
        # read and stays unconditional. Kept for backward compatibility only.
        legacy = workspace / ".cursorrules"
        if legacy.exists():
            self._append_primitive(
                manifest,
                "rule",
                PrimitiveRef(
                    name=".cursorrules",
                    path=normalize_catalog_path(legacy, workspace),
                    description=first_description(legacy),
                    kind="rule",
                    policy=LoadingPolicy.EAGER,
                    source="legacy-cursorrules",
                ),
                capped,
            )
            legacy_section = self._eager_rule_section(
                "Cursor Rules (legacy .cursorrules)", legacy, manifest
            )
            if legacy_section is not None:
                rules_sections.append(legacy_section)

        # Agents: .cursor/agents/*.md
        agents_dir = cursor_dir / "agents"
        if agents_dir.is_dir():
            for agent_file in sorted(agents_dir.glob("*.md")):
                if self._kind_is_full(manifest, "agent"):
                    self._report_ceiling(manifest, "agent", capped)
                    break
                self._append_primitive(
                    manifest,
                    "agent",
                    PrimitiveRef(
                        name=agent_file.stem,
                        path=normalize_catalog_path(agent_file, workspace),
                        description=first_description(agent_file),
                        kind="agent",
                    ),
                    capped,
                )

        self._discover_primitives(workspace, manifest, capped)

        # Hooks: parse and note; execution happens at tool/shell time via hooks.executor.
        hooks_file = cursor_dir / "hooks.json"
        if hooks_file.exists():
            from app.services.hooks import load_hooks_for_workspace

            cfg = load_hooks_for_workspace(str(workspace))
            if cfg.events:
                manifest.notes.append(
                    f"Loaded .cursor/hooks.json ({len(cfg.enabled_events)} events: "
                    f"{', '.join(cfg.enabled_events)})"
                )
            else:
                manifest.notes.append("Detected .cursor/hooks.json (no runnable command hooks)")

        # AGENTS.md at the root, or inside .cursor/ (some workflows keep it there).
        agents_md_path = workspace / "AGENTS.md"
        if not agents_md_path.exists() and (cursor_dir / "AGENTS.md").exists():
            agents_md_path = cursor_dir / "AGENTS.md"
        agents_md = read_root_instructions(agents_md_path) if agents_md_path.exists() else ""
        if agents_md_path.exists():
            self._append_primitive(
                manifest,
                "rule",
                PrimitiveRef(
                    name=rel(agents_md_path, workspace),
                    path=normalize_catalog_path(agents_md_path, workspace),
                    description="",
                    kind="rule",
                    policy=LoadingPolicy.EAGER,
                    source="root-instructions",
                ),
                capped,
            )
        self._discover_scoped_instructions(workspace, manifest, capped)

        manifest.eager_context = self._assemble_eager(
            [("Project Agents (AGENTS.md)", agents_md)],
            rules_sections,
            manifest=manifest,
        )
        return manifest
