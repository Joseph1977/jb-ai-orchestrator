# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Claude .claude/rules/ discovery and path scoping."""

from app.services.harness.base import LoadingPolicy
from app.services.harness.registry import collect_manifest, render_system_prompt


def claude_workspace(tmp_path):
    (tmp_path / ".claude").mkdir(exist_ok=True)
    (tmp_path / "CLAUDE.md").write_text("# Playbook\nRoot guidance.\n", encoding="utf-8")
    return tmp_path


def rule(tmp_path, relative, text):
    path = tmp_path / ".claude" / "rules" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def rules_by_name(manifest):
    return {r.name: r for r in manifest.rules}


def test_unscoped_rule_loads_eagerly(tmp_path):
    claude_workspace(tmp_path)
    rule(tmp_path, "security.md", "# Security\nNever log secrets.\n")
    manifest = collect_manifest(str(tmp_path))
    entry = rules_by_name(manifest)["security"]
    assert entry.policy is LoadingPolicy.EAGER
    assert "Never log secrets." in manifest.eager_context


def test_path_scoped_rule_is_lazy_and_labelled(tmp_path):
    claude_workspace(tmp_path)
    rule(
        tmp_path,
        "testing.md",
        "---\npaths: ['**/*.test.tsx', 'tests/**']\n---\n# Testing\nUse the harness.\n",
    )
    manifest = collect_manifest(str(tmp_path))
    entry = rules_by_name(manifest)["testing"]
    assert entry.policy is LoadingPolicy.SCOPED
    assert entry.scope == ("**/*.test.tsx", "tests/**")
    assert "Use the harness." not in manifest.eager_context

    prompt = render_system_prompt(manifest) or ""
    assert "applies to: **/*.test.tsx, tests/**" in prompt


def test_rules_are_discovered_recursively(tmp_path):
    claude_workspace(tmp_path)
    rule(tmp_path, "backend/api.md", "# API\nVersion every endpoint.\n")
    rule(tmp_path, "frontend/state.md", "# State\nColocate reducers.\n")
    manifest = collect_manifest(str(tmp_path))
    assert {"api", "state"} <= set(rules_by_name(manifest))


def test_rule_is_tagged_with_its_source(tmp_path):
    claude_workspace(tmp_path)
    rule(tmp_path, "a.md", "# A\nbody\n")
    manifest = collect_manifest(str(tmp_path))
    assert rules_by_name(manifest)["a"].source == "claude-rules"


def test_rules_are_not_listed_twice(tmp_path):
    claude_workspace(tmp_path)
    rule(tmp_path, "dup.md", "---\npaths: ['src/**']\n---\n# Dup\n")
    manifest = collect_manifest(str(tmp_path))
    assert [r.name for r in manifest.rules].count("dup") == 1


def test_scoped_rule_stays_out_of_the_eager_budget(tmp_path):
    claude_workspace(tmp_path)
    rule(tmp_path, "big.md", "---\npaths: ['infra/**']\n---\n" + ("TERRAFORM " * 400))
    manifest = collect_manifest(str(tmp_path))
    assert "TERRAFORM" not in manifest.eager_context
