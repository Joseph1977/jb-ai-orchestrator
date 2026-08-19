# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
from pathlib import Path

import pytest

from app.services import workspace_manager as wm
from app.services.workspace_manager import SourceKind, WorkspaceError


@pytest.mark.parametrize(
    "source,expected",
    [
        ("https://github.com/foo/bar.git", SourceKind.GIT_URL),
        ("git@github.com:foo/bar.git", SourceKind.GIT_URL),
        ("git://example.com/x", SourceKind.GIT_URL),
        ("https://github.com/foo/bar", SourceKind.GIT_URL),  # known host repo
        ("https://example.com/pkg.zip", SourceKind.SHARED_FOLDER_URL),
        ("https://example.com/pkg.tar.gz", SourceKind.SHARED_FOLDER_URL),
        ("file:///tmp/shared", SourceKind.SHARED_FOLDER_URL),
        ("\\\\server\\share", SourceKind.SHARED_FOLDER_URL),
        ("/tmp/local/path", SourceKind.LOCAL_PATH),
        ("./relative", SourceKind.LOCAL_PATH),
    ],
)
def test_classify_source(source, expected):
    assert wm.classify_source(source) == expected


def test_classify_source_empty():
    with pytest.raises(WorkspaceError):
        wm.classify_source("")


def test_resolve_within_allows_child(tmp_path):
    (tmp_path / "sub").mkdir()
    resolved = wm.resolve_within(str(tmp_path), "sub/file.txt")
    assert resolved.startswith(str(tmp_path.resolve()))


@pytest.mark.parametrize("evil", ["../escape.txt", "../../etc/passwd", "sub/../../out"])
def test_resolve_within_blocks_traversal(tmp_path, evil):
    with pytest.raises(WorkspaceError):
        wm.resolve_within(str(tmp_path), evil)


def test_provision_local_copy(tmp_path):
    src = tmp_path / "playbook"
    src.mkdir()
    (src / "AGENTS.md").write_text("# Agents\nhello")

    info = asyncio.run(wm.provision("exec-1", str(src), in_place=False))
    assert info.source_kind == SourceKind.LOCAL_PATH
    assert info.in_place is False
    assert (Path(info.path) / "AGENTS.md").read_text().startswith("# Agents")
    wm.cleanup("exec-1")


def test_provision_inplace_disabled(tmp_path, monkeypatch):
    from app.config import Config

    monkeypatch.setattr(Config, "ALLOW_INPLACE_WORKSPACE", False)
    src = tmp_path / "wp"
    src.mkdir()
    with pytest.raises(WorkspaceError):
        asyncio.run(wm.provision("exec-2", str(src), in_place=True))


def test_provision_inplace_enabled(tmp_path, monkeypatch):
    from app.config import Config

    monkeypatch.setattr(Config, "ALLOW_INPLACE_WORKSPACE", True)
    src = tmp_path / "wp"
    src.mkdir()
    info = asyncio.run(wm.provision("exec-3", str(src), in_place=True))
    assert info.in_place is True
    assert info.path == str(src.resolve())
