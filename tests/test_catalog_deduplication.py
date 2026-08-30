# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Each primitive appears once, whether an adapter or shared discovery found it."""

from collections import Counter

from app.services.harness.registry import collect_manifest


def make_skill(root, name, body="---\nname: {n}\ndescription: Does {n}.\n---\n"):
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(body.format(n=name), encoding="utf-8")


def test_claude_workspace_lists_each_primitive_once(tmp_path, dotdirs):
    claude = tmp_path / ".claude"
    make_skill(claude / "skills", "help")
    make_skill(claude / "skills", "brainstorm")
    (claude / "commands").mkdir(parents=True)
    (claude / "commands" / "help.md").write_text("/help - list commands\n", encoding="utf-8")
    (claude / "agents").mkdir(parents=True)
    (claude / "agents" / "reviewer.md").write_text("# Reviewer\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Playbook\n", encoding="utf-8")

    manifest = collect_manifest(str(tmp_path))
    assert manifest.orchestration_type == "claude-code"

    for bucket in (manifest.skills, manifest.commands, manifest.agents):
        duplicates = [p for p, n in Counter(r.path for r in bucket).items() if n > 1]
        assert duplicates == []

    assert sorted(s.name for s in manifest.skills) == ["brainstorm", "help"]
    assert [c.name for c in manifest.commands] == ["help"]
    assert [a.name for a in manifest.agents] == ["reviewer"]


def test_cursor_workspace_lists_each_primitive_once(tmp_path, dotdirs):
    cursor = tmp_path / ".cursor"
    make_skill(cursor / "skills", "release-check")
    (cursor / "agents").mkdir(parents=True)
    (cursor / "agents" / "reviewer.md").write_text("# Reviewer\n", encoding="utf-8")
    (cursor / "rules").mkdir(parents=True)
    (cursor / "rules" / "style.mdc").write_text("# Style\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Playbook\n", encoding="utf-8")

    manifest = collect_manifest(str(tmp_path))
    assert manifest.orchestration_type == "cursor"

    for bucket in (manifest.skills, manifest.commands, manifest.agents, manifest.rules):
        duplicates = [p for p, n in Counter(r.path for r in bucket).items() if n > 1]
        assert duplicates == []

    assert [a.name for a in manifest.agents] == ["reviewer"]
    assert [s.name for s in manifest.skills] == ["release-check"]


def test_adapter_description_is_kept_over_rediscovery(tmp_path, dotdirs):
    claude = tmp_path / ".claude"
    make_skill(claude / "skills", "help")
    (tmp_path / "CLAUDE.md").write_text("# Playbook\n", encoding="utf-8")

    manifest = collect_manifest(str(tmp_path))
    assert [s.description for s in manifest.skills] == ["Does help."]
