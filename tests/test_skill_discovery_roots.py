# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Capability roots, nested discovery, pruning and the per-kind ceiling."""

import pytest

from app.services.harness.base import (
    MAX_PRIMITIVES_PER_KIND,
    LoadingPolicy,
)
from harness_helpers import skill_files, workspace_files
from app.services.harness.registry import collect_manifest


def skill(tmp_path, relative, name=None, extra=""):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    label = name or path.parent.name
    path.write_text(f"---\nname: {label}\ndescription: {label} desc\n{extra}---\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "root",
    [".cursor", ".agents", ".claude", ".codex"],
)
def test_every_capability_root_is_discovered(tmp_path, root):
    skill(tmp_path, f"{root}/skills/deploy/SKILL.md", name="deploy")
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    manifest = collect_manifest(str(tmp_path))
    assert "deploy" in {s.name for s in manifest.skills}


def test_category_subfolders_are_walked(tmp_path):
    """The skill's identity is the folder holding SKILL.md, not the category."""
    skill(tmp_path, ".cursor/skills/shipping/land-it/SKILL.md", name="land-it")
    skill(tmp_path, ".cursor/skills/workflow/tdd/SKILL.md", name="tdd")
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    manifest = collect_manifest(str(tmp_path))
    assert {"land-it", "tdd"} <= {s.name for s in manifest.skills}


def test_monorepo_nested_roots_are_discovered(tmp_path):
    skill(tmp_path, ".cursor/skills/repo-wide/SKILL.md", name="repo-wide")
    skill(tmp_path, "apps/web/.cursor/skills/deploy-web/SKILL.md", name="deploy-web")
    skill(tmp_path, "packages/api/.claude/skills/api-lint/SKILL.md", name="api-lint")
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    manifest = collect_manifest(str(tmp_path))
    assert {"repo-wide", "deploy-web", "api-lint"} <= {s.name for s in manifest.skills}


def test_nested_skills_are_listed_once(tmp_path):
    skill(tmp_path, ".claude/skills/help/SKILL.md", name="help")
    (tmp_path / "CLAUDE.md").write_text("# Playbook\n", encoding="utf-8")
    manifest = collect_manifest(str(tmp_path))
    assert [s.name for s in manifest.skills].count("help") == 1


def test_disable_model_invocation_is_explicit_only(tmp_path):
    skill(tmp_path, ".cursor/skills/danger/SKILL.md", name="danger",
          extra="disable-model-invocation: true\n")
    skill(tmp_path, ".cursor/skills/safe/SKILL.md", name="safe")
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    manifest = collect_manifest(str(tmp_path))
    policies = {s.name: s.policy for s in manifest.skills}
    assert policies["danger"] is LoadingPolicy.EXPLICIT_ONLY
    assert policies["safe"] is LoadingPolicy.MODEL_DISCOVERABLE


# --- pruning ------------------------------------------------------------------


def test_walk_prunes_heavy_directories(tmp_path):
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "x.md").write_text("x", encoding="utf-8")
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "y.md").write_text("y", encoding="utf-8")

    visited = set(workspace_files(tmp_path))
    assert "src/y.md" in visited
    assert not any(v.startswith("node_modules") for v in visited)
    assert not any(v.startswith(".git") for v in visited)


def test_skills_inside_pruned_directories_are_not_discovered(tmp_path):
    skill(tmp_path, "node_modules/dep/.cursor/skills/vendored/SKILL.md", name="vendored")
    skill(tmp_path, ".cursor/skills/ours/SKILL.md", name="ours")
    found = {p.rsplit("/", 2)[-2] for p in skill_files(tmp_path)}
    assert "ours" in found
    assert "vendored" not in found


def test_discovery_order_is_deterministic(tmp_path):
    for n in ("charlie", "alpha", "bravo"):
        skill(tmp_path, f".cursor/skills/{n}/SKILL.md", name=n)
    first = skill_files(tmp_path)
    second = skill_files(tmp_path)
    assert first == second == sorted(first)


# --- ceiling ------------------------------------------------------------------


def test_per_kind_ceiling_caps_and_reports(tmp_path):
    for i in range(MAX_PRIMITIVES_PER_KIND + 25):
        skill(tmp_path, f".cursor/skills/s{i:04d}/SKILL.md", name=f"s{i:04d}")
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")

    manifest = collect_manifest(str(tmp_path))
    assert len(manifest.skills) == MAX_PRIMITIVES_PER_KIND
    assert any("Discovery ceiling reached" in n for n in manifest.notes)
    # The ceiling is reported once, not once per skipped file.
    assert sum("Discovery ceiling reached" in n for n in manifest.notes) == 1


def test_ceiling_on_one_kind_does_not_affect_another(tmp_path):
    for i in range(MAX_PRIMITIVES_PER_KIND + 5):
        skill(tmp_path, f".cursor/skills/s{i:04d}/SKILL.md", name=f"s{i:04d}")
    commands = tmp_path / ".cursor" / "commands"
    commands.mkdir(parents=True)
    (commands / "help.md").write_text("# Help\nShow the guide\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")

    manifest = collect_manifest(str(tmp_path))
    assert len(manifest.skills) == MAX_PRIMITIVES_PER_KIND
    assert "help" in {c.name for c in manifest.commands}
