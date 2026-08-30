# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for harness discovery tests.

Discovery reads are workspace-relative and go through a `WorkspaceReader`, so
tests need one anchored on their tmp_path rather than bare filesystem paths.
These wrappers take an absolute path, anchor on a workspace, and hand back the
same values the production helpers return -- including `None` for a file that
cannot be read safely, which is a distinct outcome from an empty file.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from app.services.harness import base
from app.services.harness.safe_io import WorkspacePath, WorkspaceReader


def wp(text: str) -> WorkspacePath:
    return WorkspacePath.parse(text)


@contextmanager
def reader_for(workspace: Path):
    reader = WorkspaceReader(workspace)
    try:
        yield reader
    finally:
        reader.close()


def _split(path: Path, workspace: Optional[Path]) -> tuple[Path, WorkspacePath]:
    workspace = workspace or path.parent
    return workspace, wp(Path(path).relative_to(workspace).as_posix())


# -- metadata ------------------------------------------------------------------


def scan_path(path: Path, workspace: Optional[Path] = None):
    ws, rel = _split(path, workspace)
    with reader_for(ws) as reader:
        return base.scan_primitive(reader, rel)


def first_description(path: Path, workspace: Optional[Path] = None) -> str:
    scan = scan_path(path, workspace)
    return "" if scan is None else scan.description


def primitive_name_from_path(path: Path, workspace: Optional[Path] = None) -> str:
    scan = scan_path(path, workspace)
    return "" if scan is None else scan.name


def parse_frontmatter_fields(path: Path, workspace: Optional[Path] = None) -> dict:
    scan = scan_path(path, workspace)
    if scan is None:
        return {}
    return {
        key: value
        for key, value in scan.metadata.items()
        if key in ("name", "description") and isinstance(value, str)
    }


# -- reads ---------------------------------------------------------------------


def read_capped(path: Path, cap: int, workspace: Optional[Path] = None):
    """Raw reader result: ``None`` when the file cannot be read safely."""
    ws, rel = _split(path, workspace)
    with reader_for(ws) as reader:
        return reader.read_capped(rel, cap)


def read_text_capped(path: Path, cap: int = base.MAX_EAGER_FILE_CHARS, workspace=None) -> str:
    ws, rel = _split(path, workspace)
    with reader_for(ws) as reader:
        return base.read_text_capped(reader, rel, cap)


def read_eager_rule(path: Path, cap: int = base.MAX_EAGER_FILE_CHARS, workspace=None):
    ws, rel = _split(path, workspace)
    with reader_for(ws) as reader:
        return base.read_eager_rule(reader, rel, cap)


def read_root_instructions(
    path: Path, cap: int = base.MAX_ROOT_INSTRUCTION_CHARS, workspace=None
) -> str:
    ws, rel = _split(path, workspace)
    with reader_for(ws) as reader:
        return base.read_root_instructions(reader, rel, cap=cap)


# -- traversal -----------------------------------------------------------------


def workspace_files(workspace: Path, root: str = "", recursive: bool = True) -> list[str]:
    with reader_for(workspace) as reader:
        return [
            p.posix
            for p in base.iter_workspace_files(reader, wp(root), recursive=recursive)
        ]


def pruned_files(workspace: Path, glob: str, *, recursive: bool, root: str = "") -> list[str]:
    with reader_for(workspace) as reader:
        return [
            p.posix
            for p in base.iter_pruned_files(reader, wp(root), glob, recursive=recursive)
        ]


def skill_files(workspace: Path) -> list[str]:
    with reader_for(workspace) as reader:
        return [p.posix for p in base.iter_skill_files(reader)]


def scoped_instructions(workspace: Path) -> list[str]:
    with reader_for(workspace) as reader:
        return [p.posix for p in base.iter_scoped_instructions(reader)]
