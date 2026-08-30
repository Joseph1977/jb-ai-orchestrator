# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""The workspace walker: ordering, laziness, containment and best effort.

Discovery must be bounded in *work*, not merely in output. Buffering every
match before yielding produced only 200 entries but could read an entire
filesystem to get there, so these tests assert against the traversal itself.
"""

import os
from pathlib import Path

from app.services.harness.base import (
    MAX_PRIMITIVES_PER_KIND,
    iter_pruned_files,
    iter_workspace_files,
)
from app.services.harness.registry import collect_manifest


def write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def tricky_tree(root):
    """Names chosen so separator-vs-dot and prefix ordering both appear.

    Sorting Path objects compares path components, so 'a/c.md' precedes
    'a.md'. Sorting the equivalent strings would not. A depth-first walk over
    name-sorted entries reproduces the component order; this tree is what
    distinguishes the two.
    """
    for directory in ("a", "ab", "a-b", "a.d", "z", "sub/deep"):
        for name in ("a.md", "ab.md", "a-b.md", "b.md", "z.md"):
            write(root / directory / name)
    for name in ("a.md", "ab.md", "a-b.md", "b.md", "z.md"):
        write(root / name)
    return root


# --- ordering is preserved exactly -------------------------------------------


def test_walk_order_matches_sorted_paths(tmp_path):
    """The contract is lexicographic by path component, i.e. sorted() order."""
    tricky_tree(tmp_path)

    walked = list(iter_workspace_files(tmp_path, tmp_path))

    assert walked == sorted(walked)
    assert len(walked) == 35


def test_pruned_files_order_matches_sorted_paths(tmp_path):
    tricky_tree(tmp_path)
    write(tmp_path / "node_modules" / "junk.md")

    found = list(iter_pruned_files(tmp_path, "*.md", recursive=True, workspace=tmp_path))

    assert found == sorted(found)
    assert not any("node_modules" in str(p) for p in found)


def test_subtree_is_exhausted_where_its_name_sorts(tmp_path):
    """'a/c.md' must precede 'a.md', which a files-then-directories walk breaks."""
    write(tmp_path / "a" / "c.md")
    write(tmp_path / "a.md")
    write(tmp_path / "b.md")

    found = [
        str(p.relative_to(tmp_path))
        for p in iter_pruned_files(tmp_path, "*.md", recursive=True, workspace=tmp_path)
    ]

    assert found == ["a/c.md", "a.md", "b.md"]


# --- the walk is lazy ---------------------------------------------------------


def test_walker_is_lazy(tmp_path):
    """Taking one item must not enumerate the tree."""
    for i in range(300):
        write(tmp_path / f"dir{i:04d}" / "f.md")

    walker = iter_workspace_files(tmp_path, tmp_path)
    first = next(walker)

    assert first.name == "f.md"
    assert first.parent.name == "dir0000"


def test_pruned_files_is_lazy(tmp_path):
    for i in range(300):
        write(tmp_path / f"dir{i:04d}" / "f.md")

    found = iter_pruned_files(tmp_path, "*.md", recursive=True, workspace=tmp_path)

    assert next(found).parent.name == "dir0000"


def test_non_recursive_does_not_descend(tmp_path):
    write(tmp_path / "top.md")
    write(tmp_path / "nested" / "deep.md")

    found = [
        p.name
        for p in iter_pruned_files(tmp_path, "*.md", recursive=False, workspace=tmp_path)
    ]

    assert found == ["top.md"]


# --- a capped kind stops costing reads ----------------------------------------


def test_capped_kind_stops_reading_candidates(tmp_path, monkeypatch):
    """Past the ceiling, further candidates must not be opened at all."""
    import app.services.harness.base as base

    for i in range(MAX_PRIMITIVES_PER_KIND + 500):
        write(
            tmp_path / ".cursor" / "rules" / f"rule{i:05d}.mdc",
            "---\ndescription: d\n---\nBody\n",
        )

    scans: list[str] = []
    real_scan = base.scan_primitive

    def counting_scan(path):
        scans.append(str(path))
        return real_scan(path)

    monkeypatch.setattr(base, "scan_primitive", counting_scan)
    monkeypatch.setattr("app.services.harness.cursor.scan_primitive", counting_scan)

    manifest = collect_manifest(str(tmp_path))

    assert len(manifest.rules) <= MAX_PRIMITIVES_PER_KIND
    # One read per catalogued entry, plus a small constant for other kinds.
    # 700 candidates existed; anything near that means the cap did not stop work.
    assert len(scans) <= MAX_PRIMITIVES_PER_KIND + 20, (
        f"expected reads to stop at the ceiling, got {len(scans)}"
    )


def test_exactly_at_the_ceiling_reports_no_omission(tmp_path):
    """A workspace with no excess candidate must not claim entries were dropped."""
    for i in range(MAX_PRIMITIVES_PER_KIND):
        write(
            tmp_path / ".cursor" / "rules" / f"rule{i:05d}.mdc",
            "---\ndescription: d\n---\nBody\n",
        )

    manifest = collect_manifest(str(tmp_path))

    assert len(manifest.rules) == MAX_PRIMITIVES_PER_KIND
    assert not any("ceiling" in n.lower() for n in manifest.notes)


# --- symlinks cannot walk discovery out of the workspace ----------------------


def test_symlinked_directory_is_not_descended(tmp_path):
    outside = tmp_path.parent / "outside_tree"
    write(outside / "secret.md")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    os.symlink(outside, workspace / "linked")

    found = [p.name for p in iter_workspace_files(workspace, workspace)]

    assert "secret.md" not in found


def test_symlinked_file_escaping_the_workspace_is_skipped(tmp_path):
    outside = tmp_path.parent / "outside_file"
    write(outside / "secret.md")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    os.symlink(outside / "secret.md", workspace / "linked.md")

    found = [p.name for p in iter_workspace_files(workspace, workspace)]

    assert found == []


def test_symlinked_file_inside_the_workspace_is_followed(tmp_path):
    workspace = tmp_path / "ws"
    write(workspace / "real" / "rule.md")
    os.symlink(workspace / "real" / "rule.md", workspace / "alias.md")

    found = {p.name for p in iter_workspace_files(workspace, workspace)}

    assert found == {"rule.md", "alias.md"}


def test_broken_symlink_does_not_abort_the_walk(tmp_path):
    write(tmp_path / "real.md")
    os.symlink(tmp_path / "missing.md", tmp_path / "dangling.md")

    found = [p.name for p in iter_workspace_files(tmp_path, tmp_path)]

    assert found == ["real.md"]


# --- one unreadable subtree does not abort discovery --------------------------


def test_unreadable_directory_is_skipped_not_fatal(tmp_path):
    write(tmp_path / "readable" / "a.md")
    locked = tmp_path / "locked"
    locked.mkdir()
    write(locked / "b.md")
    os.chmod(locked, 0o000)
    try:
        found = [p.name for p in iter_workspace_files(tmp_path, tmp_path)]
        assert "a.md" in found
        assert "b.md" not in found
    finally:
        os.chmod(locked, 0o755)


def test_missing_root_yields_nothing(tmp_path):
    assert list(iter_workspace_files(tmp_path / "absent", tmp_path)) == []
    assert (
        list(iter_pruned_files(tmp_path / "absent", "*.md", recursive=True, workspace=tmp_path))
        == []
    )


# --- deep trees do not exhaust the interpreter stack --------------------------


def test_deeply_nested_tree_does_not_raise_recursion_error(tmp_path):
    """The filesystem accepts trees deeper than the recursion limit."""
    deep = tmp_path
    for _ in range(400):
        deep = deep / "d"
    deep.mkdir(parents=True)
    write(deep / "buried.md")

    found = [p.name for p in iter_workspace_files(tmp_path, tmp_path)]

    assert found == ["buried.md"]
