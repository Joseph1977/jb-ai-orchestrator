# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Claude Code harness adapter (.claude/ + CLAUDE.md)."""

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
    normalize_catalog_path,
    read_root_instructions,
    read_text_capped,
    rel,
    scan_primitive,
)


def _rule_policy(scan: PrimitiveScan) -> tuple[LoadingPolicy, tuple[str, ...]]:
    """Map Claude rule frontmatter onto a generic loading policy.

    Claude loads `.claude/rules/` recursively: a rule carrying `paths` applies
    only when a matching file is touched, and one without it is unconditional,
    at the same priority as `.claude/CLAUDE.md`.
    """
    paths = scan.metadata.get("paths") or ()
    if paths:
        return LoadingPolicy.SCOPED, tuple(paths)
    return LoadingPolicy.EAGER, ()


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
                        path=normalize_catalog_path(agent_file, workspace),
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
                        path=normalize_catalog_path(cmd_file, workspace),
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
                        path=normalize_catalog_path(skill_md, workspace),
                        description=first_description(skill_md),
                        kind="skill",
                    )
                )
            for skill_md in sorted(skills_dir.glob("*.md")):
                manifest.skills.append(
                    PrimitiveRef(
                        name=skill_md.stem,
                        path=normalize_catalog_path(skill_md, workspace),
                        description=first_description(skill_md),
                        kind="skill",
                    )
                )

        # Rules: .claude/rules/**/*.md, discovered recursively. Unscoped rules
        # load up front; path-scoped ones are catalogued with their patterns.
        rules_sections: List[tuple[str, str]] = []
        rules_dir = claude_dir / "rules"
        if rules_dir.is_dir():
            for rule_file in sorted(rules_dir.rglob("*.md")):
                if not rule_file.is_file():
                    continue
                scan = scan_primitive(rule_file)
                policy, scope = _rule_policy(scan)
                manifest.rules.append(
                    PrimitiveRef(
                        name=scan.name,
                        path=normalize_catalog_path(rule_file, workspace),
                        description=scan.description,
                        kind="rule",
                        policy=policy,
                        scope=scope,
                        source="claude-rules",
                    )
                )
                if policy is LoadingPolicy.EAGER:
                    rules_sections.append(
                        (f"Claude Rule: {scan.name}", read_text_capped(rule_file))
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

        # Claude treats ./CLAUDE.md and ./.claude/CLAUDE.md as project scope.
        claude_md_path = workspace / "CLAUDE.md"
        if not claude_md_path.exists():
            claude_md_path = claude_dir / "CLAUDE.md"
        claude_md = read_root_instructions(claude_md_path) if claude_md_path.exists() else ""
        agents_md = read_root_instructions(workspace / "AGENTS.md") if (workspace / "AGENTS.md").exists() else ""

        self._discover_primitives(workspace, manifest)
        if claude_md_path.exists():
            manifest.rules.append(
                PrimitiveRef(
                    name=rel(claude_md_path, workspace),
                    path=normalize_catalog_path(claude_md_path, workspace),
                    description="",
                    kind="rule",
                    policy=LoadingPolicy.EAGER,
                    source="root-instructions",
                )
            )
        self._discover_scoped_instructions(workspace, manifest)

        manifest.eager_context = self._assemble_eager(
            [
                ("Claude Instructions (CLAUDE.md)", claude_md),
                ("Project Agents (AGENTS.md)", agents_md),
            ],
            rules_sections,
            manifest=manifest,
        )
        return manifest
