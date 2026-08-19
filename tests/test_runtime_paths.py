# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import hashlib
from pathlib import Path

import pytest

from app.services.local_tool_provider import LocalToolContext, local_tool_provider
from app.services.runtime_paths import (
    ensure_runtime,
    is_agent_runtime_rel,
    resolve_tool_path,
    runtime_root_for_thread,
)
from app.services.workspace_manager import WorkspaceError


def test_is_agent_runtime_rel():
    assert is_agent_runtime_rel(".agent/offload/x.txt")
    assert is_agent_runtime_rel("./.agent/todos.json")
    assert not is_agent_runtime_rel("output/memory.json")


def test_resolve_agent_path_uses_runtime(tmp_path):
    workspace = tmp_path / "ws"
    runtime = tmp_path / "rt"
    workspace.mkdir()
    runtime.mkdir()
    resolved = resolve_tool_path(
        str(workspace), ".agent/offload/a.txt", runtime_path=str(runtime)
    )
    assert Path(resolved).is_relative_to(runtime.resolve())
    assert "offload" in resolved


def test_resolve_agent_path_cannot_escape_runtime(tmp_path):
    workspace = tmp_path / "ws"
    runtime = tmp_path / "rt"
    workspace.mkdir()
    runtime.mkdir()
    with pytest.raises(WorkspaceError):
        resolve_tool_path(
            str(workspace), ".agent/../secret", runtime_path=str(runtime)
        )


def test_resolve_agent_path_without_runtime_fails_closed(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(WorkspaceError, match="runtime_path is required"):
        resolve_tool_path(
            str(workspace),
            ".agent/offload/a.txt",
            runtime_path=None,
        )
    assert not (workspace / ".agent").exists()


def test_list_files_hides_workspace_agent_when_runtime_set(tmp_path):
    import asyncio

    workspace = tmp_path / "ws"
    runtime = tmp_path / "rt"
    workspace.mkdir()
    runtime.mkdir()
    (workspace / ".agent").mkdir()
    (workspace / ".agent" / "leak.txt").write_text("nope")
    (workspace / "readme.md").write_text("ok")
    ctx = LocalToolContext(
        workspace_path=str(workspace), runtime_path=str(runtime)
    )
    result = asyncio.run(
        local_tool_provider.execute("list_files_local", {"path": "."}, ctx)
    )
    entries = result.get("entries") or []
    assert "readme.md" in entries
    assert not any(e.startswith(".agent") for e in entries)


def test_write_todos_lands_in_runtime(tmp_path):
    import asyncio

    workspace = tmp_path / "ws"
    runtime = tmp_path / "rt"
    workspace.mkdir()
    runtime.mkdir()
    ctx = LocalToolContext(
        workspace_path=str(workspace), runtime_path=str(runtime)
    )
    result = asyncio.run(
        local_tool_provider.execute(
            "write_todos_local",
            {"todos": [{"id": "1", "content": "x", "status": "pending"}]},
            ctx,
        )
    )
    assert result.get("path") == ".agent/todos.json"
    assert (runtime / "todos.json").is_file()
    assert not (workspace / ".agent" / "todos.json").exists()


def test_ensure_runtime_execution(tmp_path, monkeypatch):
    from app.config import Config

    monkeypatch.setattr(Config, "WORKSPACES_ROOT", str(tmp_path))
    path = ensure_runtime(execution_id="exec-1")
    assert Path(path).is_dir()
    assert path.endswith("exec-1/runtime")


def test_thread_runtime_uses_full_sha256(tmp_path, monkeypatch):
    from app.config import Config

    monkeypatch.setattr(Config, "WORKSPACES_ROOT", str(tmp_path))
    long_a = ("x" * 80) + "A"
    long_b = ("x" * 80) + "B"
    path_a = runtime_root_for_thread(long_a)
    path_b = runtime_root_for_thread(long_b)
    digest = hashlib.sha256(long_a.encode("utf-8")).hexdigest()
    assert digest in str(path_a)
    assert path_a != path_b
    assert runtime_root_for_thread(long_a) == path_a


def test_glob_hides_workspace_agent(tmp_path):
    import asyncio

    workspace = tmp_path / "ws"
    runtime = tmp_path / "rt"
    workspace.mkdir()
    runtime.mkdir()
    (workspace / ".agent").mkdir()
    (workspace / ".agent" / "leak.txt").write_text("nope")
    (workspace / "docs").mkdir()
    (workspace / "docs" / "readme.md").write_text("ok")
    ctx = LocalToolContext(
        workspace_path=str(workspace), runtime_path=str(runtime)
    )
    result = asyncio.run(
        local_tool_provider.execute("glob_local", {"pattern": "**/*"}, ctx)
    )
    matches = result.get("matches") or []
    assert any("readme.md" in m for m in matches)
    assert not any(".agent" in m for m in matches)
