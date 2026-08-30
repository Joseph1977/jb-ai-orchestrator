# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Catalog metadata parsed from YAML frontmatter."""

from tests.harness_helpers import first_description, parse_frontmatter_fields
from app.services.harness.registry import collect_manifest, render_system_prompt


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_plain_single_line_value(tmp_path):
    path = write(tmp_path, "a.md", "---\nname: helper\ndescription: One line\n---\n# Ignored\n")
    assert parse_frontmatter_fields(path) == {"name": "helper", "description": "One line"}
    assert first_description(path) == "One line"


def test_quoted_single_line_value(tmp_path):
    path = write(tmp_path, "b.md", '---\nname: "quoted"\ndescription: \'single\'\n---\n')
    assert parse_frontmatter_fields(path) == {"name": "quoted", "description": "single"}


def test_folded_block_scalar(tmp_path):
    path = write(
        tmp_path,
        "c.md",
        "---\nname: help\ndescription: >\n  Display all PM workflow commands.\n"
        "  Can also provide usage examples.\nversion: 1.0\n---\n",
    )
    assert parse_frontmatter_fields(path)["description"] == (
        "Display all PM workflow commands. Can also provide usage examples."
    )


def test_literal_block_scalar(tmp_path):
    path = write(tmp_path, "d.md", "---\nname: lit\ndescription: |\n  First line\n  Second line\n---\n")
    assert parse_frontmatter_fields(path)["description"] == "First line Second line"


def test_block_scalar_chomping_indicators(tmp_path):
    strip = write(tmp_path, "e.md", "---\ndescription: >-\n  Stripped text\n---\n")
    keep = write(tmp_path, "f.md", "---\ndescription: |+\n  Kept text\n\n---\n")
    assert parse_frontmatter_fields(strip)["description"] == "Stripped text"
    assert parse_frontmatter_fields(keep)["description"] == "Kept text"


def test_name_only_frontmatter_falls_back_to_heading(tmp_path):
    path = write(tmp_path, "g.md", "---\nname: only\n---\n# Real Title\nbody\n")
    assert "description" not in parse_frontmatter_fields(path)
    assert first_description(path) == "Real Title"


def test_malformed_yaml_degrades_without_raising(tmp_path):
    path = write(tmp_path, "h.md", "---\nname: [unclosed\ndescription: broken\n---\n# Fallback\n")
    assert parse_frontmatter_fields(path) == {}
    assert first_description(path) == "Fallback"


def test_non_scalar_values_are_ignored(tmp_path):
    path = write(tmp_path, "i.md", "---\nname:\n  nested: value\ndescription:\n  - one\n  - two\n---\n# Title\n")
    assert parse_frontmatter_fields(path) == {}
    assert first_description(path) == "Title"


def test_numeric_value_is_coerced(tmp_path):
    path = write(tmp_path, "j.md", "---\nname: 42\ndescription: 3.5\n---\n")
    assert parse_frontmatter_fields(path) == {"name": "42", "description": "3.5"}


def test_description_truncated_to_200_chars(tmp_path):
    path = write(tmp_path, "k.md", f"---\ndescription: {'x' * 300}\n---\n")
    assert len(parse_frontmatter_fields(path)["description"]) == 200


def test_pm_skill_shape_with_bom_and_html_comment(tmp_path):
    path = write(
        tmp_path,
        "l.md",
        "\ufeff<!--\nSkill: help\nOutputs:\n  - MD file: HELP.md\n-->\n\n"
        "---\nname: help\ndescription: >\n  Display all PM workflow commands and their descriptions.\n"
        "version: 1.0\n---\n\n## Behavior\n",
    )
    assert first_description(path) == (
        "Display all PM workflow commands and their descriptions."
    )


def test_catalog_renders_real_description(tmp_path):
    skill = tmp_path / ".claude" / "skills" / "help"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: help\ndescription: >\n  Show every command.\n---\n", encoding="utf-8"
    )
    (tmp_path / "CLAUDE.md").write_text("# Playbook\n", encoding="utf-8")

    manifest = collect_manifest(str(tmp_path))
    descriptions = {s.name: s.description for s in manifest.skills}
    assert descriptions["help"] == "Show every command."

    prompt = render_system_prompt(manifest)
    assert "Show every command." in prompt
    assert "— >" not in prompt
