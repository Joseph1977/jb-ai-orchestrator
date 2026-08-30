# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Claude Code harness adapter (.claude/ + CLAUDE.md)."""

from __future__ import annotations

from pathlib import Path

from typing import List

from app.services.harness.base import (
    AddOutcome,
    DiscoveryContext,
    HarnessAdapter,
    HarnessManifest,
    PRUNED_DIR_NAMES,
    LoadingPolicy,
    PrimitiveFacts,
    PrimitiveScan,
    read_root_instructions,
    iter_pruned_files,
    skill_policy,
)
from app.services.workspace_io import (
    UnsafePathError,
    WorkspaceEntryKind,
    WorkspacePath,
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
        with self._begin(workspace, manifest) as ctx:
            return self._collect(ctx, manifest, workspace)

    def _collect(
        self,
        ctx: DiscoveryContext,
        manifest: HarnessManifest,
        workspace: Path,
    ) -> HarnessManifest:
        claude_dir = WorkspacePath.parse(".claude")

        # Agents: .claude/agents/*.md. Model-selectable, so the invocation
        # opt-out applies here as it does to skills.
        for wp in iter_pruned_files(
            ctx.reader, claude_dir.child("agents"), "*.md", recursive=False
        ):
            outcome = ctx.consider(
                "agent",
                wp,
                lambda scan, wp=wp: PrimitiveFacts(
                    name=wp.stem,
                    description=scan.description,
                    policy=skill_policy(scan),
                    source="claude-agents",
                ),
            ).outcome
            if outcome is AddOutcome.CEILING:
                break

        # Commands: .claude/commands/*.md. Invoked by name, so they stay
        # model-discoverable regardless of disable-model-invocation.
        for wp in iter_pruned_files(
            ctx.reader, claude_dir.child("commands"), "*.md", recursive=False
        ):
            outcome = ctx.consider(
                "command",
                wp,
                lambda scan, wp=wp: PrimitiveFacts(
                    name=wp.stem,
                    description=scan.description,
                    policy=LoadingPolicy.MODEL_DISCOVERABLE,
                    source="claude-commands",
                ),
            ).outcome
            if outcome is AddOutcome.CEILING:
                break

        # Skills: .claude/skills/<name>/SKILL.md and .claude/skills/*.md.
        # Adapter entries win during deduplication, so the invocation opt-out
        # has to be honoured here -- shared discovery never gets the chance.
        skills_dir = claude_dir.child("skills")
        for entry in ctx.reader.scandir(skills_dir):
            if entry.kind is not WorkspaceEntryKind.DIRECTORY:
                continue
            if entry.name in PRUNED_DIR_NAMES:
                continue
            try:
                wp = skills_dir.child(entry.name).child("SKILL.md")
            except UnsafePathError:
                continue
            outcome = ctx.consider(
                "skill",
                wp,
                lambda scan, wp=wp: PrimitiveFacts(
                    name=wp.parent.name,
                    description=scan.description,
                    policy=skill_policy(scan),
                    source="claude-skills",
                ),
            ).outcome
            if outcome is AddOutcome.CEILING:
                break
        for wp in iter_pruned_files(ctx.reader, skills_dir, "*.md", recursive=False):
            outcome = ctx.consider(
                "skill",
                wp,
                lambda scan, wp=wp: PrimitiveFacts(
                    name=wp.stem,
                    description=scan.description,
                    policy=skill_policy(scan),
                    source="claude-skills",
                ),
            ).outcome
            if outcome is AddOutcome.CEILING:
                break

        # Rules: .claude/rules/**/*.md, discovered recursively. Unscoped rules
        # load up front; path-scoped ones are catalogued with their patterns.
        rules_sections: List[tuple[str, str]] = []
        for wp in iter_pruned_files(
            ctx.reader, claude_dir.child("rules"), "*.md", recursive=True
        ):
            result = ctx.consider(
                "rule",
                wp,
                lambda scan: PrimitiveFacts(
                    name=scan.name,
                    description=scan.description,
                    policy=_rule_policy(scan)[0],
                    scope=_rule_policy(scan)[1],
                    source="claude-rules",
                ),
            )
            if result.outcome is AddOutcome.CEILING:
                break
            if result.added and result.facts.policy is LoadingPolicy.EAGER:
                section = self._eager_rule_section(
                    ctx, f"Claude Rule: {result.facts.name}", wp
                )
                if section is not None:
                    rules_sections.append(section)

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

        # Claude treats ./CLAUDE.md and ./.claude/CLAUDE.md as project scope.
        claude_md_wp = WorkspacePath.parse("CLAUDE.md")
        if not ctx.reader.exists(claude_md_wp):
            claude_md_wp = claude_dir.child("CLAUDE.md")
        has_claude_md = ctx.reader.exists(claude_md_wp)
        claude_md = read_root_instructions(ctx.reader, claude_md_wp) if has_claude_md else ""
        agents_md = read_root_instructions(ctx.reader, WorkspacePath.parse("AGENTS.md"))

        self._discover_primitives(ctx)
        if has_claude_md:
            ctx.consider(
                "rule",
                claude_md_wp,
                lambda scan: PrimitiveFacts(
                    name=claude_md_wp.posix,
                    description="",
                    policy=LoadingPolicy.EAGER,
                    source="root-instructions",
                ),
            )
        self._discover_scoped_instructions(ctx)

        manifest.eager_context = self._assemble_eager(
            [
                ("Claude Instructions (CLAUDE.md)", claude_md),
                ("Project Agents (AGENTS.md)", agents_md),
            ],
            rules_sections,
            manifest=manifest,
        )
        return manifest
