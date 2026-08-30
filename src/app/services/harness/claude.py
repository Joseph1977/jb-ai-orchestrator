# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Claude Code harness adapter (.claude/ + CLAUDE.md)."""

from __future__ import annotations

from pathlib import Path

from typing import List

from app.services.harness.base import (
    HarnessAdapter,
    HarnessManifest,
    PRUNED_DIR_NAMES,
    LoadingPolicy,
    PrimitiveRef,
    PrimitiveScan,
    normalize_catalog_path,
    read_root_instructions,
    rel,
    iter_pruned_files,
    scan_primitive,
    skill_policy,
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
        capped: set[str] = set()

        # Agents: .claude/agents/*.md. Model-selectable, so the invocation
        # opt-out applies here as it does to skills.
        agents_dir = claude_dir / "agents"
        if agents_dir.is_dir():
            for agent_file in sorted(agents_dir.glob("*.md")):
                if self._kind_is_full(manifest, "agent"):
                    self._report_ceiling(manifest, "agent", capped)
                    break
                scan = scan_primitive(agent_file)
                self._append_primitive(
                    manifest,
                    "agent",
                    PrimitiveRef(
                        name=agent_file.stem,
                        path=normalize_catalog_path(agent_file, workspace),
                        description=scan.description,
                        kind="agent",
                        policy=skill_policy(scan),
                        source="claude-agents",
                    ),
                    capped,
                )

        # Commands: .claude/commands/*.md. Invoked by name, so they stay
        # model-discoverable regardless of disable-model-invocation.
        commands_dir = claude_dir / "commands"
        if commands_dir.is_dir():
            for cmd_file in sorted(commands_dir.glob("*.md")):
                if self._kind_is_full(manifest, "command"):
                    self._report_ceiling(manifest, "command", capped)
                    break
                scan = scan_primitive(cmd_file)
                self._append_primitive(
                    manifest,
                    "command",
                    PrimitiveRef(
                        name=cmd_file.stem,
                        path=normalize_catalog_path(cmd_file, workspace),
                        description=scan.description,
                        kind="command",
                        policy=LoadingPolicy.MODEL_DISCOVERABLE,
                        source="claude-commands",
                    ),
                    capped,
                )

        # Skills: .claude/skills/*/SKILL.md or *.md. Adapter entries win during
        # deduplication, so the invocation opt-out has to be honoured here --
        # shared discovery never gets the chance to correct it.
        skills_dir = claude_dir / "skills"
        if skills_dir.is_dir():
            for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
                # "*" matches a pruned directory sitting directly under
                # skills/, which the glob would otherwise walk straight into.
                if skill_md.parent.name in PRUNED_DIR_NAMES:
                    continue
                if self._kind_is_full(manifest, "skill"):
                    self._report_ceiling(manifest, "skill", capped)
                    break
                scan = scan_primitive(skill_md)
                self._append_primitive(
                    manifest,
                    "skill",
                    PrimitiveRef(
                        name=skill_md.parent.name,
                        path=normalize_catalog_path(skill_md, workspace),
                        description=scan.description,
                        kind="skill",
                        policy=skill_policy(scan),
                        source="claude-skills",
                    ),
                    capped,
                )
            for skill_md in sorted(skills_dir.glob("*.md")):
                if self._kind_is_full(manifest, "skill"):
                    self._report_ceiling(manifest, "skill", capped)
                    break
                scan = scan_primitive(skill_md)
                self._append_primitive(
                    manifest,
                    "skill",
                    PrimitiveRef(
                        name=skill_md.stem,
                        path=normalize_catalog_path(skill_md, workspace),
                        description=scan.description,
                        kind="skill",
                        policy=skill_policy(scan),
                        source="claude-skills",
                    ),
                    capped,
                )

        # Rules: .claude/rules/**/*.md, discovered recursively. Unscoped rules
        # load up front; path-scoped ones are catalogued with their patterns.
        rules_sections: List[tuple[str, str]] = []
        rules_dir = claude_dir / "rules"
        if rules_dir.is_dir():
            for rule_file in iter_pruned_files(
                rules_dir, "*.md", recursive=True, workspace=workspace
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
                        name=scan.name,
                        path=normalize_catalog_path(rule_file, workspace),
                        description=scan.description,
                        kind="rule",
                        policy=policy,
                        scope=scope,
                        source="claude-rules",
                    ),
                    capped,
                )
                if added and policy is LoadingPolicy.EAGER:
                    section = self._eager_rule_section(
                        f"Claude Rule: {scan.name}", rule_file, manifest
                    )
                    if section is not None:
                        rules_sections.append(section)

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

        self._discover_primitives(workspace, manifest, capped)
        if claude_md_path.exists():
            self._append_primitive(
                manifest,
                "rule",
                PrimitiveRef(
                    name=rel(claude_md_path, workspace),
                    path=normalize_catalog_path(claude_md_path, workspace),
                    description="",
                    kind="rule",
                    policy=LoadingPolicy.EAGER,
                    source="root-instructions",
                ),
                capped,
            )
        self._discover_scoped_instructions(workspace, manifest, capped)

        manifest.eager_context = self._assemble_eager(
            [
                ("Claude Instructions (CLAUDE.md)", claude_md),
                ("Project Agents (AGENTS.md)", agents_md),
            ],
            rules_sections,
            manifest=manifest,
        )
        return manifest
