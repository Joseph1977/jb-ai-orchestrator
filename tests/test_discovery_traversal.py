# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Ordering, laziness and best-effort behaviour of the discovery walker.

The walker's contract is lexicographic order by path component, produced
lazily, with memory proportional to the active frontier rather than the tree.
It is explicitly *not* the containment boundary -- that is `WorkspaceReader`,
covered in test_workspace_containment.py.
"""

import inspect
import os
import sys

import pytest

from app.services.harness.base import (
    ALL_KINDS,
    MAX_PRIMITIVES_PER_KIND,
    AddOutcome,
    DiscoveryContext,
    HarnessManifest,
    PrimitiveFacts,
    PrimitiveRef,
    iter_pruned_files,
    iter_workspace_files,
)
from app.services.harness.generic import GenericAdapter
from app.services.workspace_io import WorkspacePath, WorkspaceReader
from harness_helpers import pruned_files, reader_for, workspace_files, wp


def write(path, text="body"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def count_scandirs(monkeypatch):
    calls: list[object] = []
    real = os.scandir

    def counting(*args, **kwargs):
        calls.append(args[0] if args else None)
        return real(*args, **kwargs)

    monkeypatch.setattr(os, "scandir", counting)
    return calls


def count_opens(monkeypatch):
    opens: list[int] = []
    real = os.fdopen

    def counting(fd, *args, **kwargs):
        opens.append(fd)
        return real(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", counting)
    return opens


# --- ordering -----------------------------------------------------------------


def test_walk_order_matches_sorted_paths(tmp_path):
    for rel in ("b/2.md", "a/1.md", "a/z/9.md", "c.md", "a/b.md"):
        write(tmp_path / rel)

    found = workspace_files(tmp_path)

    assert found == sorted(found)


def test_pruned_files_order_matches_sorted_paths(tmp_path):
    for rel in ("z.md", "m/n.md", "a.md", "m/a.md"):
        write(tmp_path / rel)

    found = pruned_files(tmp_path, "*.md", recursive=True)

    assert found == ["a.md", "m/a.md", "m/n.md", "z.md"]


def test_subtree_is_exhausted_where_its_name_sorts(tmp_path):
    """A directory's contents come out at the directory's own sort position.

    This is what separates true lexicographic order from breadth-first order,
    which would emit every top-level file before descending anywhere.
    """
    write(tmp_path / "aaa.md")
    write(tmp_path / "bbb" / "inner.md")
    write(tmp_path / "ccc.md")

    assert workspace_files(tmp_path) == ["aaa.md", "bbb/inner.md", "ccc.md"]


# --- laziness -----------------------------------------------------------------


def test_walker_is_lazy(tmp_path, monkeypatch):
    for name in ("a", "b", "c", "d"):
        write(tmp_path / name / "f.md")

    with reader_for(tmp_path) as reader:
        walk = iter_workspace_files(reader, WorkspacePath())
        calls = count_scandirs(monkeypatch)
        next(walk)
        # The root plus the first subdirectory only; the rest of the tree is
        # untouched until the caller asks for more.
        assert len(calls) <= 2


def test_pruned_files_is_lazy(tmp_path, monkeypatch):
    for name in ("a", "b", "c", "d"):
        write(tmp_path / name / "f.md")

    with reader_for(tmp_path) as reader:
        walk = iter_pruned_files(reader, WorkspacePath(), "*.md", recursive=True)
        calls = count_scandirs(monkeypatch)
        next(walk)
        assert len(calls) <= 2


# --- pruning and shape --------------------------------------------------------


def test_non_recursive_does_not_descend(tmp_path):
    write(tmp_path / "top.md")
    write(tmp_path / "nested" / "deep.md")

    assert workspace_files(tmp_path, recursive=False) == ["top.md"]


def test_pruned_directories_are_never_entered(tmp_path, monkeypatch):
    write(tmp_path / "node_modules" / "pkg" / "x.md")
    write(tmp_path / "src" / "y.md")

    found = workspace_files(tmp_path)

    assert found == ["src/y.md"]


def test_missing_root_yields_nothing(tmp_path):
    assert workspace_files(tmp_path, root="absent") == []


# --- best effort --------------------------------------------------------------


def test_unreadable_directory_is_skipped_not_fatal(tmp_path):
    write(tmp_path / "ok" / "a.md")
    locked = tmp_path / "locked"
    locked.mkdir()
    write(locked / "b.md")
    os.chmod(locked, 0o000)
    try:
        found = workspace_files(tmp_path)
    finally:
        os.chmod(locked, 0o755)

    assert "ok/a.md" in found


def test_deeply_nested_tree_does_not_raise_recursion_error(tmp_path):
    """The filesystem accepts far deeper trees than the interpreter's stack.

    The limit is lowered rather than the tree made 1,000 levels deep, because
    a path that long exceeds PATH_MAX before it exceeds the stack. What is
    under test is that the walker's depth costs heap, not frames.
    """
    depth = 400
    current = tmp_path
    for _ in range(depth):
        current = current / "d"
    current.mkdir(parents=True)
    write(current / "buried.md")

    original = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack()) + 100)
    try:
        found = workspace_files(tmp_path)
    finally:
        sys.setrecursionlimit(original)

    assert found == ["/".join(["d"] * depth) + "/buried.md"]


# --- the candidate transaction ------------------------------------------------


def make_ctx(tmp_path) -> tuple[DiscoveryContext, HarnessManifest]:
    manifest = HarnessManifest(orchestration_type="generic", detected=False)
    return DiscoveryContext(tmp_path, manifest, WorkspaceReader(tmp_path)), manifest


def facts(scan):
    return PrimitiveFacts(name=scan.name, description=scan.description)


def test_confirmed_capped_kinds_do_not_walk_at_all(tmp_path, monkeypatch):
    """A capped kind must cost no traversal, not merely no catalog entries."""
    write(tmp_path / ".cursor" / "skills" / "s" / "SKILL.md")
    write(tmp_path / "sub" / "AGENTS.md")

    ctx, _ = make_ctx(tmp_path)
    try:
        ctx.capped.update(ALL_KINDS)
        calls = count_scandirs(monkeypatch)
        GenericAdapter()._discover_primitives(ctx)
        GenericAdapter()._discover_scoped_instructions(ctx)
        assert calls == []
    finally:
        ctx.reader.close()


def test_capped_rules_skip_the_scoped_instruction_walk(tmp_path, monkeypatch):
    write(tmp_path / "sub" / "AGENTS.md")

    ctx, _ = make_ctx(tmp_path)
    try:
        ctx.capped.add("rule")
        calls = count_scandirs(monkeypatch)
        GenericAdapter()._discover_scoped_instructions(ctx)
        assert calls == []
    finally:
        ctx.reader.close()


def test_candidate_over_the_ceiling_is_never_read(tmp_path, monkeypatch):
    """The 201st candidate is refused on count alone, without being opened."""
    path = write(tmp_path / "extra.md", "---\nname: extra\n---\n")
    ctx, manifest = make_ctx(tmp_path)
    try:
        manifest.skills.extend(
            PrimitiveRef(name=f"s{i}", path=f"s{i}.md", kind="skill")
            for i in range(MAX_PRIMITIVES_PER_KIND)
        )
        opens = count_opens(monkeypatch)
        result = ctx.consider("skill", wp("extra.md"), facts)
        assert result.outcome is AddOutcome.CEILING
        assert opens == [], "an over-ceiling candidate must not be opened"
    finally:
        ctx.reader.close()
    assert path.exists()


def test_exactly_at_the_ceiling_reports_no_omission(tmp_path):
    ctx, manifest = make_ctx(tmp_path)
    try:
        for i in range(MAX_PRIMITIVES_PER_KIND):
            write(tmp_path / f"s{i}.md", "---\nname: s\n---\n")
            ctx.consider("skill", wp(f"s{i}.md"), facts)
        assert len(manifest.skills) == MAX_PRIMITIVES_PER_KIND
        assert not any("ceiling" in note.lower() for note in manifest.notes)
    finally:
        ctx.reader.close()


def test_duplicate_is_distinct_from_ceiling(tmp_path):
    """A duplicate must not read like overflow, or it would stop traversal."""
    write(tmp_path / "dup.md", "---\nname: dup\n---\n")
    ctx, _ = make_ctx(tmp_path)
    try:
        assert ctx.consider("skill", wp("dup.md"), facts).outcome is AddOutcome.ADDED
        assert ctx.consider("skill", wp("dup.md"), facts).outcome is AddOutcome.DUPLICATE
    finally:
        ctx.reader.close()


def test_transaction_owns_path_and_kind(tmp_path):
    """Adapters supply provider fields only; the entry's identity is not theirs."""
    write(tmp_path / "a" / "thing.md", "---\nname: thing\n---\n")
    ctx, manifest = make_ctx(tmp_path)
    try:
        ctx.consider(
            "agent",
            wp("a/thing.md"),
            lambda scan: PrimitiveFacts(name="renamed", description="d"),
        )
    finally:
        ctx.reader.close()

    entry = manifest.agents[0]
    assert entry.path == "a/thing.md"
    assert entry.kind == "agent"
    assert entry.name == "renamed"


def test_unknown_kind_is_refused(tmp_path):
    ctx, _ = make_ctx(tmp_path)
    try:
        with pytest.raises(ValueError):
            ctx.consider("gadget", wp("x.md"), facts)
    finally:
        ctx.reader.close()


def test_ceiling_never_injects_eager_content(tmp_path):
    """An entry refused by the ceiling must not reach the eager context."""
    from app.services.harness.cursor import CursorAdapter

    write(
        tmp_path / ".cursor" / "rules" / "always.mdc",
        "---\nalwaysApply: true\n---\nSECRET-EAGER-BODY\n",
    )
    write(tmp_path / "AGENTS.md", "# Agents\n")

    adapter = CursorAdapter()
    manifest = HarnessManifest(orchestration_type="cursor", detected=True)
    manifest.rules.extend(
        PrimitiveRef(name=f"r{i}", path=f"r{i}.mdc", kind="rule")
        for i in range(MAX_PRIMITIVES_PER_KIND)
    )
    ctx = DiscoveryContext(tmp_path, manifest, WorkspaceReader(tmp_path))
    try:
        adapter._collect(ctx, manifest, tmp_path)
    finally:
        ctx.reader.close()

    assert "SECRET-EAGER-BODY" not in manifest.eager_context
