# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Scoped AGENTS.md / CLAUDE.md discovery beyond the repository root."""

from app.services.harness.base import LoadingPolicy
from app.services.harness.registry import collect_manifest, render_system_prompt


def rules_by_path(manifest):
    return {r.path: r for r in manifest.rules}


def test_nested_claude_md_is_scoped_not_injected(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / "CLAUDE.md").write_text("# Root\nRoot guidance.\n", encoding="utf-8")
    nested = tmp_path / "packages" / "api"
    nested.mkdir(parents=True)
    (nested / "CLAUDE.md").write_text("# API\nNESTED DIRECTIVE\n", encoding="utf-8")

    manifest = collect_manifest(str(tmp_path))
    entry = rules_by_path(manifest)["packages/api/CLAUDE.md"]
    assert entry.policy is LoadingPolicy.SCOPED
    assert entry.scope == ("packages/api/**",)
    assert "NESTED DIRECTIVE" not in manifest.eager_context

    prompt = render_system_prompt(manifest) or ""
    assert "packages/api/CLAUDE.md" in prompt
    assert "applies to: packages/api/**" in prompt


def test_nested_agents_md_is_scoped(tmp_path):
    (tmp_path / ".cursor").mkdir()
    (tmp_path / "AGENTS.md").write_text("# Root\nRoot guidance.\n", encoding="utf-8")
    nested = tmp_path / "apps" / "web"
    nested.mkdir(parents=True)
    (nested / "AGENTS.md").write_text("# Web\nWEB DIRECTIVE\n", encoding="utf-8")

    manifest = collect_manifest(str(tmp_path))
    entry = rules_by_path(manifest)["apps/web/AGENTS.md"]
    assert entry.policy is LoadingPolicy.SCOPED
    assert entry.scope == ("apps/web/**",)
    assert "WEB DIRECTIVE" not in manifest.eager_context


def test_root_instructions_stay_eager_and_are_not_re_catalogued(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / "CLAUDE.md").write_text("# Root\nROOT DIRECTIVE\n", encoding="utf-8")

    manifest = collect_manifest(str(tmp_path))
    entry = rules_by_path(manifest)["CLAUDE.md"]
    assert entry.policy is LoadingPolicy.EAGER
    assert entry.source == "root-instructions"
    assert "ROOT DIRECTIVE" in manifest.eager_context
    assert [r.path for r in manifest.rules].count("CLAUDE.md") == 1

    prompt = render_system_prompt(manifest) or ""
    # Eager: injected verbatim, never listed as a lazy capability.
    assert "ROOT DIRECTIVE" in prompt
    assert "`CLAUDE.md`" not in prompt


def test_dot_claude_claude_md_is_project_scope(tmp_path):
    """Claude treats ./.claude/CLAUDE.md as project-level, like ./CLAUDE.md."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").write_text("# Project\nDOT CLAUDE DIRECTIVE\n", encoding="utf-8")

    manifest = collect_manifest(str(tmp_path))
    assert manifest.orchestration_type == "claude-code"
    assert "DOT CLAUDE DIRECTIVE" in manifest.eager_context
    assert rules_by_path(manifest)[".claude/CLAUDE.md"].policy is LoadingPolicy.EAGER


def test_root_claude_md_wins_over_dot_claude(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (tmp_path / "CLAUDE.md").write_text("# Root\nROOT WINS\n", encoding="utf-8")
    (claude_dir / "CLAUDE.md").write_text("# Other\nSECONDARY\n", encoding="utf-8")

    manifest = collect_manifest(str(tmp_path))
    assert "ROOT WINS" in manifest.eager_context
    assert "SECONDARY" not in manifest.eager_context
    # The unused one is still catalogued rather than silently dropped.
    assert ".claude/CLAUDE.md" in rules_by_path(manifest)


def test_nested_instructions_in_pruned_dirs_are_ignored(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / "CLAUDE.md").write_text("# Root\n", encoding="utf-8")
    vendored = tmp_path / "node_modules" / "dep"
    vendored.mkdir(parents=True)
    (vendored / "CLAUDE.md").write_text("# Vendored\n", encoding="utf-8")

    manifest = collect_manifest(str(tmp_path))
    assert not any("node_modules" in r.path for r in manifest.rules)


def test_catalog_paths_use_forward_slashes(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / "CLAUDE.md").write_text("# Root\n", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "AGENTS.md").write_text("# Deep\n", encoding="utf-8")

    manifest = collect_manifest(str(tmp_path))
    for bucket in (manifest.skills, manifest.commands, manifest.agents, manifest.rules):
        for ref in bucket:
            assert "\\" not in ref.path
