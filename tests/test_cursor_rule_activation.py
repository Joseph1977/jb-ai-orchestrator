# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Cursor rule frontmatter decides eager versus lazy loading."""

from app.services.harness.base import LoadingPolicy
from app.services.harness.registry import collect_manifest, render_system_prompt


def rule(tmp_path, name, frontmatter, body="Rule body text."):
    rules = tmp_path / ".cursor" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    path = rules / f"{name}.mdc"
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")
    return path


def policies(manifest):
    return {r.name: r.policy for r in manifest.rules}


def test_always_apply_is_eager(tmp_path):
    rule(tmp_path, "always", "alwaysApply: true", body="ALWAYS BODY")
    manifest = collect_manifest(str(tmp_path))
    assert policies(manifest)["always"] is LoadingPolicy.EAGER
    assert "ALWAYS BODY" in manifest.eager_context


def test_globs_rule_is_scoped_and_not_eager(tmp_path):
    rule(tmp_path, "ts", 'alwaysApply: false\nglobs: ["**/*.ts", "**/*.tsx"]', body="TS BODY")
    manifest = collect_manifest(str(tmp_path))
    entry = {r.name: r for r in manifest.rules}["ts"]
    assert entry.policy is LoadingPolicy.SCOPED
    assert entry.scope == ("**/*.ts", "**/*.tsx")
    assert "TS BODY" not in manifest.eager_context


def test_description_only_rule_is_model_discoverable(tmp_path):
    rule(tmp_path, "adr", "alwaysApply: false\ndescription: How to write an ADR", body="ADR BODY")
    manifest = collect_manifest(str(tmp_path))
    assert policies(manifest)["adr"] is LoadingPolicy.MODEL_DISCOVERABLE
    assert "ADR BODY" not in manifest.eager_context


def test_bare_rule_is_explicit_only(tmp_path):
    rule(tmp_path, "manual", "# no activation fields", body="MANUAL BODY")
    manifest = collect_manifest(str(tmp_path))
    assert policies(manifest)["manual"] is LoadingPolicy.EXPLICIT_ONLY
    assert "MANUAL BODY" not in manifest.eager_context

    prompt = render_system_prompt(manifest) or ""
    assert "`manual`" not in prompt


def test_only_always_apply_rule_bodies_reach_the_prompt(tmp_path):
    """A catalog description is a summary; only eager rules ship their body.

    Each body carries its marker on a later line so it cannot be mistaken for
    the one-line description the catalog is allowed to show.
    """
    body = "Summary line.\n\nDIRECTIVE-{}: do the thing."
    rule(tmp_path, "always", "alwaysApply: true", body=body.format("ALWAYS"))
    rule(tmp_path, "scoped", 'globs: "**/*.py"', body=body.format("SCOPED"))
    rule(tmp_path, "manual", "# nothing", body=body.format("MANUAL"))
    manifest = collect_manifest(str(tmp_path))

    prompt = render_system_prompt(manifest) or ""
    assert "DIRECTIVE-ALWAYS" in prompt
    assert "DIRECTIVE-SCOPED" not in prompt
    assert "DIRECTIVE-MANUAL" not in prompt

    # The scoped rule is still announced, with the paths it applies to.
    assert "`scoped`" in prompt
    assert "applies to: **/*.py" in prompt
    # The eager rule is not listed again on top of its injected body.
    assert "`always`" not in prompt
    # The manual rule is withheld from the model entirely.
    assert "`manual`" not in prompt
    # ...yet every rule remains visible to API callers.
    assert set(manifest.summary()["rules"]) == {"always", "scoped", "manual"}


def test_plain_md_under_cursor_rules_is_ignored(tmp_path):
    rules = tmp_path / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "notes.md").write_text("---\nalwaysApply: true\n---\nPLAIN MD\n", encoding="utf-8")
    manifest = collect_manifest(str(tmp_path))
    assert "notes" not in policies(manifest)
    assert "PLAIN MD" not in manifest.eager_context


def test_legacy_cursorrules_stays_eager_and_marked(tmp_path):
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursorrules").write_text("LEGACY BODY\n", encoding="utf-8")
    manifest = collect_manifest(str(tmp_path))
    entry = {r.name: r for r in manifest.rules}[".cursorrules"]
    assert entry.policy is LoadingPolicy.EAGER
    assert entry.source == "legacy-cursorrules"
    assert "LEGACY BODY" in manifest.eager_context


def test_malformed_rule_frontmatter_is_explicit_only_not_eager(tmp_path):
    """A rule we cannot read must not default to unconditional injection."""
    rule(tmp_path, "broken", "globs: [unclosed", body="BROKEN BODY")
    manifest = collect_manifest(str(tmp_path))
    assert policies(manifest)["broken"] is LoadingPolicy.EXPLICIT_ONLY
    assert "BROKEN BODY" not in manifest.eager_context
