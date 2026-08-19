# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Cursor harness adapter (.cursor/ + AGENTS.md)."""

from __future__ import annotations

from pathlib import Path
from typing import List

from app.services.harness.base import (
    HarnessAdapter,
    HarnessManifest,
    PrimitiveRef,
    first_description,
    read_root_instructions,
    read_text_capped,
    rel,
)


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

        # Rules: .cursor/rules/*.mdc (+ legacy .cursorrules). Injected eagerly.
        rules_sections: List[str] = []
        rules_dir = cursor_dir / "rules"
        if rules_dir.is_dir():
            for rule_file in sorted(rules_dir.rglob("*.mdc")):
                manifest.rules.append(
                    PrimitiveRef(
                        name=rule_file.stem,
                        path=rel(rule_file, workspace),
                        description=first_description(rule_file),
                        kind="rule",
                    )
                )
                rules_sections.append(read_text_capped(rule_file))
        legacy = workspace / ".cursorrules"
        if legacy.exists():
            rules_sections.append(read_text_capped(legacy))

        # Agents: .cursor/agents/*.md
        agents_dir = cursor_dir / "agents"
        if agents_dir.is_dir():
            for agent_file in sorted(agents_dir.glob("*.md")):
                manifest.agents.append(
                    PrimitiveRef(
                        name=agent_file.stem,
                        path=rel(agent_file, workspace),
                        description=first_description(agent_file),
                        kind="agent",
                    )
                )

        self._discover_primitives(workspace, manifest)

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

        manifest.eager_context = self._assemble_eager(
            [
                ("Project Agents (AGENTS.md)", agents_md),
                ("Cursor Rules", "\n\n---\n\n".join(rules_sections)),
            ]
        )
        return manifest
