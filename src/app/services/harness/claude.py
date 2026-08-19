# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Claude Code harness adapter (.claude/ + CLAUDE.md)."""

from __future__ import annotations

from pathlib import Path

from app.services.harness.base import (
    HarnessAdapter,
    HarnessManifest,
    PrimitiveRef,
    first_description,
    read_root_instructions,
    read_text_capped,
    rel,
)


class ClaudeCodeAdapter(HarnessAdapter):
    type_id = "claude-code"

    def detect(self, workspace: Path) -> int:
        score = 0
        if (workspace / ".claude").is_dir():
            score += 70
        if (workspace / "CLAUDE.md").exists():
            score += 30
        return min(score, 100)

    def collect(self, workspace: Path, *, detected: bool, confidence: int) -> HarnessManifest:
        manifest = HarnessManifest(
            orchestration_type=self.type_id,
            detected=detected,
            confidence=confidence,
        )
        claude_dir = workspace / ".claude"

        # Agents: .claude/agents/*.md
        agents_dir = claude_dir / "agents"
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

        # Commands: .claude/commands/*.md
        commands_dir = claude_dir / "commands"
        if commands_dir.is_dir():
            for cmd_file in sorted(commands_dir.glob("*.md")):
                manifest.commands.append(
                    PrimitiveRef(
                        name=cmd_file.stem,
                        path=rel(cmd_file, workspace),
                        description=first_description(cmd_file),
                        kind="command",
                    )
                )

        # Skills: .claude/skills/*/SKILL.md or *.md
        skills_dir = claude_dir / "skills"
        if skills_dir.is_dir():
            for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
                manifest.skills.append(
                    PrimitiveRef(
                        name=skill_md.parent.name,
                        path=rel(skill_md, workspace),
                        description=first_description(skill_md),
                        kind="skill",
                    )
                )
            for skill_md in sorted(skills_dir.glob("*.md")):
                manifest.skills.append(
                    PrimitiveRef(
                        name=skill_md.stem,
                        path=rel(skill_md, workspace),
                        description=first_description(skill_md),
                        kind="skill",
                    )
                )

        if (claude_dir / "settings.json").exists() or (claude_dir / "hooks.json").exists():
            from app.services.hooks import load_hooks_for_workspace

            cfg = load_hooks_for_workspace(str(workspace))
            if cfg.source == "claude" and cfg.events:
                manifest.notes.append(
                    f"Loaded Claude hooks ({len(cfg.enabled_events)} events: "
                    f"{', '.join(cfg.enabled_events)})"
                )
            else:
                manifest.notes.append(
                    "Detected .claude settings/hooks (no runnable command hooks found)"
                )

        claude_md = read_root_instructions(workspace / "CLAUDE.md") if (workspace / "CLAUDE.md").exists() else ""
        agents_md = read_root_instructions(workspace / "AGENTS.md") if (workspace / "AGENTS.md").exists() else ""

        self._discover_primitives(workspace, manifest)

        manifest.eager_context = self._assemble_eager(
            [
                ("Claude Instructions (CLAUDE.md)", claude_md),
                ("Project Agents (AGENTS.md)", agents_md),
            ]
        )
        return manifest
