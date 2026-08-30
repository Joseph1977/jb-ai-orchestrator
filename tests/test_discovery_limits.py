# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Pruning and the per-kind ceiling cover every discovery path.

Both guarantees used to apply only to shared discovery. Adapters enumerated
their own rules, skills, commands and agents with rglob and a plain append, so
a workspace the adapter claimed was traversed without pruning and catalogued
without limit.
"""

from app.services.harness.base import MAX_PRIMITIVES_PER_KIND, iter_pruned_files
from app.services.harness.registry import collect_manifest


def write(path, text="---\ndescription: d\n---\nBody\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- pruning ------------------------------------------------------------------


def test_pruned_iterator_skips_excluded_directories(tmp_path):
    write(tmp_path / "keep" / "a.md")
    write(tmp_path / "node_modules" / "pkg" / "b.md")
    write(tmp_path / ".git" / "c.md")

    found = [p.name for p in iter_pruned_files(tmp_path, "*.md", recursive=True)]

    assert found == ["a.md"]


def test_pruned_iterator_is_not_recursive_when_told_not_to_be(tmp_path):
    write(tmp_path / "top.md")
    write(tmp_path / "nested" / "deep.md")

    found = [p.name for p in iter_pruned_files(tmp_path, "*.md", recursive=False)]

    assert found == ["top.md"]


def test_pruned_iterator_tolerates_a_missing_root(tmp_path):
    assert list(iter_pruned_files(tmp_path / "absent", "*.md", recursive=True)) == []


def test_cursor_rules_in_node_modules_are_not_catalogued(tmp_path):
    """The adapter's own rule enumeration is pruned, not just shared discovery."""
    write(tmp_path / ".cursor" / "rules" / "real.mdc", "---\ndescription: REAL\n---\nBody\n")
    write(
        tmp_path / ".cursor" / "rules" / "node_modules" / "junk.mdc",
        "---\ndescription: JUNK\n---\nBody\n",
    )

    manifest = collect_manifest(str(tmp_path))

    names = {r.name for r in manifest.rules}
    assert "real" in names
    assert "junk" not in names


def test_claude_rules_in_pruned_directories_are_skipped(tmp_path):
    write(tmp_path / ".claude" / "rules" / "real.md", "---\ndescription: REAL\n---\nBody\n")
    write(tmp_path / ".claude" / "rules" / "dist" / "junk.md", "---\ndescription: JUNK\n---\nBody\n")

    manifest = collect_manifest(str(tmp_path))

    paths = {r.path for r in manifest.rules}
    assert not any("dist/junk.md" in p for p in paths)


# --- the per-kind ceiling ------------------------------------------------------


def test_adapter_enumerated_rules_respect_the_ceiling(tmp_path):
    """Cursor rules are enumerated by the adapter, not by shared discovery."""
    for i in range(MAX_PRIMITIVES_PER_KIND + 25):
        write(
            tmp_path / ".cursor" / "rules" / f"rule{i:04d}.mdc",
            "---\ndescription: d\n---\nBody\n",
        )

    manifest = collect_manifest(str(tmp_path))

    assert len(manifest.rules) <= MAX_PRIMITIVES_PER_KIND
    assert any("Discovery ceiling reached" in n for n in manifest.notes)


def test_adapter_enumerated_skills_respect_the_ceiling(tmp_path):
    for i in range(MAX_PRIMITIVES_PER_KIND + 10):
        write(
            tmp_path / ".claude" / "skills" / f"skill{i:04d}" / "SKILL.md",
            "---\ndescription: d\n---\nBody\n",
        )

    manifest = collect_manifest(str(tmp_path))

    assert len(manifest.skills) <= MAX_PRIMITIVES_PER_KIND
    assert any("skill" in n for n in manifest.notes if "ceiling" in n)


def test_the_ceiling_is_reported_once_per_kind(tmp_path):
    for i in range(MAX_PRIMITIVES_PER_KIND + 40):
        write(
            tmp_path / ".cursor" / "rules" / f"rule{i:04d}.mdc",
            "---\ndescription: d\n---\nBody\n",
        )

    manifest = collect_manifest(str(tmp_path))

    ceiling_notes = [n for n in manifest.notes if "Discovery ceiling reached" in n]
    assert len(ceiling_notes) == 1


def test_the_ceiling_is_per_kind_not_global(tmp_path):
    for i in range(MAX_PRIMITIVES_PER_KIND + 5):
        write(
            tmp_path / ".cursor" / "rules" / f"rule{i:04d}.mdc",
            "---\ndescription: d\n---\nBody\n",
        )
    write(tmp_path / ".claude" / "commands" / "deploy.md", "---\ndescription: d\n---\nBody\n")

    manifest = collect_manifest(str(tmp_path))

    assert len(manifest.rules) <= MAX_PRIMITIVES_PER_KIND
    assert {c.name for c in manifest.commands} == {"deploy"}
