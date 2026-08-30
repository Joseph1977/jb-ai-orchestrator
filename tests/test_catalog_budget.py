# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Loading policy filtering and balanced catalog allocation."""

from app.services.harness.base import HarnessManifest, LoadingPolicy, PrimitiveRef
from app.services.harness.registry import (
    MAX_CATALOG_CHARS,
    MAX_SUBAGENT_CATALOG_CHARS,
    _allocate,
    _render_catalog,
    render_subagent_catalog,
    render_system_prompt,
)


def ref(name, kind="skill", policy=LoadingPolicy.MODEL_DISCOVERABLE, scope=(), desc="d"):
    return PrimitiveRef(
        name=name,
        path=f".claude/{kind}s/{name}.md",
        description=desc,
        kind=kind,
        policy=policy,
        scope=scope,
    )


def manifest_with(**kinds):
    m = HarnessManifest(orchestration_type="claude-code", detected=True, confidence=90)
    for kind, refs in kinds.items():
        getattr(m, kind).extend(refs)
    return m


# --- policy decides catalog visibility ---------------------------------------


def test_eager_primitives_are_not_listed_again():
    m = manifest_with(
        rules=[
            ref("always", kind="rule", policy=LoadingPolicy.EAGER),
            ref("lazy", kind="rule", policy=LoadingPolicy.MODEL_DISCOVERABLE),
        ]
    )
    catalog, _ = _render_catalog(m, budget=MAX_CATALOG_CHARS)
    assert "`lazy`" in catalog
    assert "`always`" not in catalog


def test_explicit_only_is_omitted_from_the_catalog():
    m = manifest_with(
        rules=[
            ref("manual", kind="rule", policy=LoadingPolicy.EXPLICIT_ONLY),
            ref("discoverable", kind="rule"),
        ]
    )
    catalog, _ = _render_catalog(m, budget=MAX_CATALOG_CHARS)
    assert "`discoverable`" in catalog
    assert "`manual`" not in catalog


def test_commands_are_exempt_from_explicit_only_omission():
    """Omitting commands would regress /help: the model must know they exist."""
    m = manifest_with(
        commands=[ref("help", kind="command", policy=LoadingPolicy.EXPLICIT_ONLY)],
        skills=[ref("hidden", kind="skill", policy=LoadingPolicy.EXPLICIT_ONLY)],
    )
    catalog, _ = _render_catalog(m, budget=MAX_CATALOG_CHARS)
    assert "`help`" in catalog
    assert "`hidden`" not in catalog


def test_scoped_entries_announce_their_scope():
    m = manifest_with(
        rules=[ref("ts", kind="rule", policy=LoadingPolicy.SCOPED, scope=("**/*.ts",))]
    )
    catalog, _ = _render_catalog(m, budget=MAX_CATALOG_CHARS)
    assert "applies to: **/*.ts" in catalog


def test_eager_primitives_still_appear_in_the_api_summary():
    m = manifest_with(rules=[ref("always", kind="rule", policy=LoadingPolicy.EAGER)])
    assert m.summary()["rules"] == ["always"]


# --- balanced allocation ------------------------------------------------------


def test_allocation_is_max_min_fair():
    # Two kinds want little, one wants far more than an equal share.
    allocation = _allocate({"a": 10, "b": 10, "c": 10_000}, 300)
    assert allocation["a"] == 10
    assert allocation["b"] == 10
    assert allocation["c"] == 280


def test_allocation_never_exceeds_budget():
    allocation = _allocate({"a": 5000, "b": 5000, "c": 5000, "d": 5000}, 1000)
    assert sum(allocation.values()) <= 1000


def test_allocation_is_order_independent():
    forward = _allocate({"a": 100, "b": 4000, "c": 50}, 900)
    backward = _allocate({"c": 50, "b": 4000, "a": 100}, 900)
    assert forward == backward


def test_crowded_kind_does_not_starve_the_others():
    """The old tail cut dropped Rules first, deterministically.

    Sections render Skills, Commands, Agents, Rules in that order, so a plain
    truncation always sacrificed the last kinds regardless of their size.
    """
    m = manifest_with(
        skills=[ref(f"skill-{i:03d}", desc="x" * 150) for i in range(200)],
        rules=[ref("important-rule", kind="rule")],
        agents=[ref("important-agent", kind="agent")],
    )
    catalog, omitted = _render_catalog(m, budget=2000)
    assert "`important-rule`" in catalog
    assert "`important-agent`" in catalog
    assert omitted.get("skills", 0) > 0


def test_budget_is_respected_and_omissions_are_declared():
    m = manifest_with(skills=[ref(f"s-{i:03d}", desc="y" * 120) for i in range(300)])
    catalog, omitted = _render_catalog(m, budget=1500)
    assert len(catalog) <= 1500, "budget is a total, header and notices included"
    assert omitted["skills"] > 0
    assert "more not shown (catalog budget reached)" in catalog


def test_omission_is_reported_on_the_manifest_and_logged(caplog):
    m = manifest_with(skills=[ref(f"s-{i:03d}", desc="z" * 300) for i in range(400)])
    with caplog.at_level("WARNING"):
        render_system_prompt(m)
    assert any("Catalog budget reached" in n for n in m.notes)


def test_subagent_catalog_uses_its_own_smaller_budget():
    m = manifest_with(skills=[ref(f"s-{i:03d}", desc="w" * 100) for i in range(300)])
    main, _ = _render_catalog(m, budget=MAX_CATALOG_CHARS)
    sub = render_subagent_catalog(m)
    assert len(sub) < len(main)
    assert len(sub) <= MAX_SUBAGENT_CATALOG_CHARS
    # Replaced tail truncation: the listing ends on a whole entry, not mid-line.
    assert "[catalog truncated]" not in sub
    assert sub.rstrip().endswith(")") or "not shown" in sub


def test_empty_catalog_renders_nothing():
    catalog, omitted = _render_catalog(manifest_with(), budget=MAX_CATALOG_CHARS)
    assert catalog == ""
    assert omitted == {}
