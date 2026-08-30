# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Every discovery read is contained inside the workspace.

Containment used to live in the directory walker, which left the paths that do
not walk -- adapter enumeration and root-instruction reads -- outside the
guarantee. It now lives in `WorkspaceReader`, so these tests cover each entry
point independently rather than trusting one traversal to protect the rest.

The check is the open itself: components are traversed one at a time relative
to a workspace descriptor with `O_NOFOLLOW`, so there is no validate-then-open
window to race.
"""

import os

import pytest

from app.services.harness import safe_io
from app.services.harness.base import (
    RootInstructionUnreadable,
    scan_primitive,
)
from app.services.harness.registry import collect_manifest
from app.services.harness.safe_io import (
    ReaderUnavailableError,
    UnsafePathError,
    WorkspacePath,
    WorkspaceReader,
)
from tests.harness_helpers import reader_for, wp


def write(path, text="body"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def open_fd_count() -> int:
    """Descriptors held by this process, via the per-process fd directory."""
    for probe in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(probe):
            return len(os.listdir(probe))
    pytest.skip("no per-process fd directory on this platform")


# --- lexical rejection, before any syscall ------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["/etc/passwd", "../outside.md", "a/../../b.md", "a//b.md/../..", "\0bad"],
)
def test_unsafe_paths_are_rejected_lexically(bad):
    with pytest.raises(UnsafePathError):
        WorkspacePath.parse(bad)


def test_empty_and_dot_components_are_rejected():
    root = WorkspacePath.parse("a")
    for bad in ("", ".", "..", "b/c", "b\\c"):
        with pytest.raises(UnsafePathError):
            root.child(bad)


def test_parse_keeps_ordinary_relative_paths():
    assert WorkspacePath.parse(".cursor/rules/a.mdc").parts == (
        ".cursor",
        "rules",
        "a.mdc",
    )


# --- symlink containment, per component ---------------------------------------


def test_symlinked_discovery_root_is_not_traversed(tmp_path):
    """A symlinked root is the case a child-only check misses entirely."""
    outside = tmp_path.parent / "outside-root"
    write(outside / "secret.md", "LEAKED")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".claude").mkdir()
    os.symlink(outside, workspace / ".claude" / "rules")

    with reader_for(workspace) as reader:
        assert reader.scandir(wp(".claude/rules")) == []
        assert reader.read_capped(wp(".claude/rules/secret.md"), 100) is None


def test_symlinked_parent_component_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside-parent"
    write(outside / "secret.md", "LEAKED")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    os.symlink(outside, workspace / "link")

    with reader_for(workspace) as reader:
        assert reader.read_capped(wp("link/secret.md"), 100) is None


def test_symlinked_leaf_file_is_rejected(tmp_path):
    outside = write(tmp_path.parent / "outside-leaf.md", "LEAKED")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    os.symlink(outside, workspace / "AGENTS.md")

    with reader_for(workspace) as reader:
        assert reader.read_capped(wp("AGENTS.md"), 100) is None
        with pytest.raises(RootInstructionUnreadable):
            from app.services.harness.base import read_root_instructions

            read_root_instructions(reader, wp("AGENTS.md"))


def test_symlink_inside_the_workspace_is_still_refused(tmp_path):
    """Containment is enforced by refusing links, not by resolving them.

    Resolving and comparing is the raceable pattern this design removes, so an
    inside-the-workspace link is refused too rather than specially permitted.
    """
    write(tmp_path / "real.md", "content")
    os.symlink(tmp_path / "real.md", tmp_path / "alias.md")

    with reader_for(tmp_path) as reader:
        assert reader.read_capped(wp("real.md"), 100) == ("content", False)
        assert reader.read_capped(wp("alias.md"), 100) is None


def test_broken_symlink_does_not_abort_discovery(tmp_path):
    write(tmp_path / "good.md", "ok")
    os.symlink(tmp_path / "nowhere", tmp_path / "dangling.md")

    with reader_for(tmp_path) as reader:
        assert reader.read_capped(wp("good.md"), 100) == ("ok", False)
        assert reader.read_capped(wp("dangling.md"), 100) is None


def test_leaf_swapped_to_a_symlink_after_traversal_is_refused(tmp_path):
    """The open is the check, so there is no window between them to exploit.

    Staged with a deterministic hook rather than a thread race: the point is
    that the guarantee holds whenever the swap lands, not that a particular
    interleaving happens to be caught.
    """
    outside = write(tmp_path.parent / "outside-swap.md", "LEAKED")
    workspace = tmp_path / "ws"
    write(workspace / "sub" / "rule.md", "honest content")

    target = workspace / "sub" / "rule.md"

    def swap(candidate):
        if candidate.posix == "sub/rule.md" and not target.is_symlink():
            target.unlink()
            os.symlink(outside, target)

    safe_io.set_before_leaf_open(swap)
    try:
        with reader_for(workspace) as reader:
            assert reader.read_capped(wp("sub/rule.md"), 100) is None
    finally:
        safe_io.set_before_leaf_open(None)


# --- non-regular files --------------------------------------------------------


def test_fifo_is_rejected_without_blocking(tmp_path):
    """O_NONBLOCK plus an fstat check, so a hostile FIFO cannot stall startup."""
    fifo = tmp_path / "AGENTS.md"
    os.mkfifo(fifo)

    with reader_for(tmp_path) as reader:
        assert reader.read_capped(wp("AGENTS.md"), 100) is None


def test_directory_is_not_openable_as_a_file(tmp_path):
    (tmp_path / "adir").mkdir()
    with reader_for(tmp_path) as reader:
        assert reader.read_capped(wp("adir"), 100) is None


def test_workspace_root_is_not_openable_as_a_file(tmp_path):
    with reader_for(tmp_path) as reader:
        with pytest.raises(UnsafePathError):
            with reader.open_text(WorkspacePath()):
                pass


# --- unreadable is not the same as absent -------------------------------------


def test_unsafe_capability_file_is_skipped_not_catalogued(tmp_path):
    """A rejected file is dropped, not listed with a filename guess.

    A catalog entry the model cannot read is worse than no entry, because the
    model will try to read it.
    """
    outside = write(tmp_path.parent / "outside-skill.md", "---\nname: leaked\n---\n")
    skills = tmp_path / ".claude" / "skills" / "danger"
    skills.mkdir(parents=True)
    os.symlink(outside, skills / "SKILL.md")
    write(tmp_path / ".claude" / "skills" / "safe" / "SKILL.md", "---\nname: safe\n---\n")
    write(tmp_path / "CLAUDE.md", "# Playbook\n")

    manifest = collect_manifest(str(tmp_path))

    names = {s.name for s in manifest.skills}
    assert "safe" in names
    assert "danger" not in names and "leaked" not in names


def test_readable_file_without_frontmatter_keeps_its_fallback(tmp_path):
    write(tmp_path / "plain.md", "# Just A Heading\n")
    with reader_for(tmp_path) as reader:
        scan = scan_primitive(reader, wp("plain.md"))
    assert scan is not None
    assert scan.name == "plain"
    assert scan.description == "Just A Heading"


def test_empty_file_is_catalogued_but_unreadable_one_is_not(tmp_path):
    write(tmp_path / "empty.md", "")
    with reader_for(tmp_path) as reader:
        assert scan_primitive(reader, wp("empty.md")) is not None
        assert scan_primitive(reader, wp("missing.md")) is None


# --- reader lifecycle ---------------------------------------------------------


def test_reader_refuses_to_construct_without_the_primitives(tmp_path, monkeypatch):
    """Fail closed. A path-based fallback would silently drop containment."""
    monkeypatch.setattr(safe_io, "platform_supports_safe_io", lambda: False)
    with pytest.raises(ReaderUnavailableError):
        WorkspaceReader(tmp_path)


def test_reader_on_a_missing_workspace_raises(tmp_path):
    with pytest.raises(OSError):
        WorkspaceReader(tmp_path / "nope")


def test_closed_reader_refuses_further_reads(tmp_path):
    write(tmp_path / "a.md", "x")
    reader = WorkspaceReader(tmp_path)
    reader.close()
    reader.close()  # idempotent
    with pytest.raises(UnsafePathError):
        with reader.open_text(wp("a.md")):
            pass


def test_descriptors_are_released_on_success_and_failure(tmp_path):
    write(tmp_path / "deep" / "a" / "b" / "file.md", "x")
    os.symlink(tmp_path.parent, tmp_path / "escape")

    before = open_fd_count()
    with reader_for(tmp_path) as reader:
        for _ in range(50):
            reader.read_capped(wp("deep/a/b/file.md"), 100)
            reader.read_capped(wp("escape/anything.md"), 100)
            reader.read_capped(wp("missing/at/all.md"), 100)
            reader.scandir(wp("deep/a"))
            reader.scandir(wp("escape"))
    after = open_fd_count()

    assert after <= before + 1, "descriptors leaked across repeated operations"


def test_traversal_holds_one_descriptor_at_a_time(tmp_path):
    """Correctness must not depend on the process descriptor limit."""
    current = tmp_path
    for _ in range(300):
        current = current / "d"
    current.mkdir(parents=True)
    write(current / "deep.md", "x")

    before = open_fd_count()
    with reader_for(tmp_path) as reader:
        from app.services.harness.base import iter_workspace_files

        found = [p.posix for p in iter_workspace_files(reader, WorkspacePath())]
        during = open_fd_count()
    assert len(found) == 1
    assert during <= before + 2, "descriptor use must not grow with depth"
