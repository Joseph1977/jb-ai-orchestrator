# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""``disable-model-invocation`` is honoured wherever a primitive is enumerated.

Adapter entries win during deduplication, so an adapter that enumerates its
own skills has to apply the opt-out itself: shared discovery never gets the
chance to correct the policy afterwards.

Commands are the deliberate exception. A slash command is reached by name, so
withholding it from the catalog would hide it from the only mechanism that
invokes it.
"""

from app.services.harness.base import LoadingPolicy
from app.services.harness.registry import collect_manifest, render_system_prompt


def claude_skill(tmp_path, name, frontmatter, body="Skill body."):
    folder = tmp_path / ".claude" / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def claude_flat(tmp_path, kind, name, frontmatter, body="Body."):
    folder = tmp_path / ".claude" / kind
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def by_name(refs):
    return {r.name: r for r in refs}


# --- skills ------------------------------------------------------------------


def test_claude_skill_opt_out_is_explicit_only(tmp_path):
    claude_skill(tmp_path, "internal", "description: Internal\ndisable-model-invocation: true")
    manifest = collect_manifest(str(tmp_path))
    assert by_name(manifest.skills)["internal"].policy is LoadingPolicy.EXPLICIT_ONLY


def test_claude_skill_without_opt_out_is_model_discoverable(tmp_path):
    claude_skill(tmp_path, "public", "description: Public")
    manifest = collect_manifest(str(tmp_path))
    assert by_name(manifest.skills)["public"].policy is LoadingPolicy.MODEL_DISCOVERABLE


def test_opted_out_skill_is_absent_from_the_catalog(tmp_path):
    claude_skill(tmp_path, "hidden", "description: HIDDEN MARKER\ndisable-model-invocation: true")
    claude_skill(tmp_path, "shown", "description: SHOWN MARKER")

    prompt = render_system_prompt(collect_manifest(str(tmp_path)))

    assert "SHOWN MARKER" in prompt
    assert "HIDDEN MARKER" not in prompt


def test_flat_claude_skill_also_honours_the_opt_out(tmp_path):
    claude_flat(tmp_path, "skills", "flat", "description: Flat\ndisable-model-invocation: true")
    manifest = collect_manifest(str(tmp_path))
    assert by_name(manifest.skills)["flat"].policy is LoadingPolicy.EXPLICIT_ONLY


def test_adapter_precedence_cannot_reinstate_a_discoverable_policy(tmp_path):
    """The adapter entry is the one that survives deduplication."""
    claude_skill(tmp_path, "once", "description: Once\ndisable-model-invocation: true")

    manifest = collect_manifest(str(tmp_path))

    entries = [s for s in manifest.skills if s.name == "once"]
    assert len(entries) == 1, "the skill must be catalogued exactly once"
    assert entries[0].policy is LoadingPolicy.EXPLICIT_ONLY


# --- agents ------------------------------------------------------------------


def test_claude_agent_opt_out_is_explicit_only(tmp_path):
    claude_flat(tmp_path, "agents", "scout", "description: Scout\ndisable-model-invocation: true")
    manifest = collect_manifest(str(tmp_path))
    assert by_name(manifest.agents)["scout"].policy is LoadingPolicy.EXPLICIT_ONLY


# --- commands are exempt ------------------------------------------------------


def test_command_stays_model_discoverable_despite_the_opt_out(tmp_path):
    """A slash command is invoked by name; hiding it would strand it."""
    claude_flat(tmp_path, "commands", "deploy", "description: Deploy\ndisable-model-invocation: true")

    manifest = collect_manifest(str(tmp_path))

    assert by_name(manifest.commands)["deploy"].policy is LoadingPolicy.MODEL_DISCOVERABLE


def test_opted_out_command_still_reaches_the_catalog(tmp_path):
    claude_flat(
        tmp_path,
        "commands",
        "release",
        "description: RELEASE MARKER\ndisable-model-invocation: true",
    )

    prompt = render_system_prompt(collect_manifest(str(tmp_path)))

    assert "RELEASE MARKER" in prompt


def test_discovered_command_outside_the_adapter_is_also_exempt(tmp_path):
    """Shared discovery applies the same exemption as the adapter."""
    folder = tmp_path / ".cursor" / "commands"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "ship.md").write_text(
        "---\ndescription: Ship it\ndisable-model-invocation: true\n---\nBody\n",
        encoding="utf-8",
    )

    manifest = collect_manifest(str(tmp_path))

    assert by_name(manifest.commands)["ship"].policy is LoadingPolicy.MODEL_DISCOVERABLE
