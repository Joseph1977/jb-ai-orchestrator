# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Strict lazy harness: catalogs only, no slash/memory/skill body injection."""

from pathlib import Path

from app.services.harness import collect_manifest
from tests.harness_helpers import first_description, parse_frontmatter_fields
from app.services.harness.registry import (
    PROGRESSIVE_DISCLOSURE_INSTRUCTION,
    enrich_system_prompt,
    render_subagent_catalog,
    render_system_prompt,
)


def _lazy_workspace(tmp_path: Path, dotdirs) -> Path:
    (tmp_path / "AGENTS.md").write_text(
        "FULL ROOT AGENTS CONTENT\n" + ("line\n" * 5),
        encoding="utf-8",
    )
    skill_dir = tmp_path / ".cursor" / "skills" / "brainstorm"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Brainstorm\n\nSECRET SKILL BODY MUST NOT APPEAR IN PROMPT",
        encoding="utf-8",
    )
    cmd_dir = tmp_path / ".cursor" / "commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "brainstorm.md").write_text(
        "# Brainstorm command\n\nCOMMAND BODY MUST NOT APPEAR",
        encoding="utf-8",
    )
    mem = tmp_path / "output" / "memory.json"
    mem.parent.mkdir(parents=True)
    mem.write_text('{"step": "waiting_for_user"}', encoding="utf-8")
    return tmp_path


def test_enrich_system_prompt_is_noop():
    assert enrich_system_prompt("base only") == "base only"
    assert enrich_system_prompt(None) is None


def test_hi_and_slash_command_do_not_inject_bodies(tmp_path, dotdirs):
    ws = _lazy_workspace(tmp_path, dotdirs)
    manifest = collect_manifest(str(ws))
    for user_prompt in ("hi", "/brainstorm", "please /brainstorm ideas"):
        prompt = render_system_prompt(manifest)
        enriched = enrich_system_prompt(
            prompt,
            workspace_path=str(ws),
            manifest=manifest,
            user_prompt=user_prompt,
            active_command="brainstorm",
        )
        assert enriched == prompt
        assert "FULL ROOT AGENTS CONTENT" in (prompt or "")
        assert "SECRET SKILL BODY" not in (prompt or "")
        assert "COMMAND BODY MUST NOT" not in (prompt or "")
        assert "waiting_for_user" not in (prompt or "")
        assert "brainstorm" in (prompt or "").lower()
        assert PROGRESSIVE_DISCLOSURE_INSTRUCTION in (prompt or "")


def test_subagent_catalog_metadata_only(tmp_path, dotdirs):
    ws = _lazy_workspace(tmp_path, dotdirs)
    manifest = collect_manifest(str(ws))
    catalog = render_subagent_catalog(manifest)
    assert "SECRET SKILL BODY" not in catalog
    assert "COMMAND BODY MUST NOT" not in catalog
    assert "brainstorm" in catalog.lower()


def test_catalog_dedupes_by_normalized_path(tmp_path, dotdirs):
    ws = tmp_path
    (ws / "AGENTS.md").write_text("# Agents", encoding="utf-8")
    skill = ws / ".cursor" / "skills" / "dup"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Dup skill\nSame path", encoding="utf-8")
    manifest = collect_manifest(str(ws))
    paths = [s.path.replace("\\", "/") for s in manifest.skills]
    assert paths.count(".cursor/skills/dup/SKILL.md") == 1


def test_first_description_bom_html_frontmatter_heading(tmp_path):
    bom_path = tmp_path / "bom.md"
    bom_path.write_text("\ufeff<!-- ignore -->\n# Visible Title\nbody", encoding="utf-8")
    assert first_description(bom_path) == "Visible Title"

    fm_path = tmp_path / "fm.md"
    fm_path.write_text(
        "---\nname: custom-name\ndescription: From frontmatter\n---\n# Ignored\n",
        encoding="utf-8",
    )
    assert parse_frontmatter_fields(fm_path)["name"] == "custom-name"
    assert first_description(fm_path) == "From frontmatter"
