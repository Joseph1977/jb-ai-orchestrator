# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio

from app.services.local_tool_provider import LocalToolContext, local_tool_provider


def _ctx(tmp_path) -> LocalToolContext:
    return LocalToolContext(workspace_path=str(tmp_path), in_place=False)


def run(coro):
    return asyncio.run(coro)


def test_namespacing_and_conflict_base_names():
    # Local tools are namespaced so they never literally collide with MCP names.
    assert local_tool_provider.is_local_tool("read_file_local")
    assert not local_tool_provider.is_local_tool("read_file")
    # Base names drive the MCP conflict filter.
    assert {
        "read_file",
        "write_file",
        "git_status",
        "edit_file",
        "glob",
        "grep",
        "execute",
        "write_todos",
        "task",
    } <= local_tool_provider.base_names()
    assert local_tool_provider.is_hub_managed("task_local")
    assert not local_tool_provider.is_hub_managed("write_todos_local")


def test_write_then_read(tmp_path):
    ctx = _ctx(tmp_path)
    w = run(local_tool_provider.execute("write_file_local", {"path": "a/b.txt", "content": "hi"}, ctx))
    assert w.get("bytesWritten") == 2
    r = run(local_tool_provider.execute("read_file_local", {"path": "a/b.txt"}, ctx))
    assert r.get("content") == "hi"


def test_create_file_overwrite_guard(tmp_path):
    ctx = _ctx(tmp_path)
    run(local_tool_provider.execute("create_file_local", {"path": "x.txt", "content": "1"}, ctx))
    dup = run(local_tool_provider.execute("create_file_local", {"path": "x.txt", "content": "2"}, ctx))
    assert "error" in dup
    ok = run(local_tool_provider.execute("create_file_local", {"path": "x.txt", "content": "2", "overwrite": True}, ctx))
    assert ok.get("created") is True


def test_path_traversal_blocked(tmp_path):
    ctx = _ctx(tmp_path)
    res = run(local_tool_provider.execute("read_file_local", {"path": "../../etc/passwd"}, ctx))
    assert "error" in res
    res2 = run(local_tool_provider.execute("write_file_local", {"path": "../escape.txt", "content": "x"}, ctx))
    assert "error" in res2
    assert not (tmp_path.parent / "escape.txt").exists()


def test_list_files(tmp_path):
    (tmp_path / "one.txt").write_text("1")
    (tmp_path / "sub").mkdir()
    ctx = _ctx(tmp_path)
    res = run(local_tool_provider.execute("list_files_local", {"path": "."}, ctx))
    entries = res.get("entries")
    assert "one.txt" in entries
    assert "sub/" in entries


def test_ask_user_is_awaits_and_has_no_handler():
    assert local_tool_provider.awaits_response("ask_user_local") is True
    # No inline handler -> handled specially by the hub (persist-and-return).
    res = asyncio.run(
        local_tool_provider.execute("ask_user_local", {"question": "?"}, LocalToolContext(workspace_path="/tmp"))
    )
    assert "error" in res  # execute() refuses; the hub short-circuits instead


def test_git_flow(dotdirs, tmp_path):
    ctx = _ctx(tmp_path)
    # init a repo in the sandbox
    from app.services import workspace_manager as wm

    code, _out, _err = asyncio.run(wm.run_git(["init"], cwd=str(tmp_path)))
    assert code == 0
    (tmp_path / "f.txt").write_text("content")

    status = run(local_tool_provider.execute("git_status_local", {}, ctx))
    assert "f.txt" in status.get("stdout", "")

    add = run(local_tool_provider.execute("git_add_local", {"paths": ["f.txt"]}, ctx))
    assert add.get("exitCode") == 0

    commit = run(local_tool_provider.execute("git_commit_local", {"message": "init"}, ctx))
    assert commit.get("exitCode") == 0

    log = run(local_tool_provider.execute("git_log_local", {}, ctx))
    assert "init" in log.get("stdout", "")


def test_edit_file_replace_and_ambiguity(tmp_path):
    ctx = _ctx(tmp_path)
    (tmp_path / "a.txt").write_text("foo bar foo")
    amb = run(
        local_tool_provider.execute(
            "edit_file_local",
            {"path": "a.txt", "old_string": "foo", "new_string": "baz"},
            ctx,
        )
    )
    assert "error" in amb
    ok = run(
        local_tool_provider.execute(
            "edit_file_local",
            {"path": "a.txt", "old_string": "foo", "new_string": "baz", "replace_all": True},
            ctx,
        )
    )
    assert ok.get("replacements") == 2
    assert (tmp_path / "a.txt").read_text() == "baz bar baz"


def test_glob_and_grep(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def hello():\n    return 1\n")
    (tmp_path / "src" / "b.txt").write_text("hello world\n")
    ctx = _ctx(tmp_path)

    found = run(local_tool_provider.execute("glob_local", {"pattern": "**/*.py"}, ctx))
    assert found.get("count") == 1
    assert "src/a.py" in found.get("matches", [])

    grepped = run(
        local_tool_provider.execute(
            "grep_local",
            {"pattern": "hello", "glob": "*.py"},
            ctx,
        )
    )
    assert grepped.get("count") == 1
    assert grepped["matches"][0]["path"] == "src/a.py"


def test_execute_shell(tmp_path):
    ctx = _ctx(tmp_path)
    (tmp_path / "note.txt").write_text("hi")
    res = run(local_tool_provider.execute("execute_local", {"command": "cat note.txt"}, ctx))
    assert res.get("exitCode") == 0
    assert "hi" in res.get("stdout", "")
    bad = run(local_tool_provider.execute("execute_local", {"command": "exit 7"}, ctx))
    assert bad.get("exitCode") == 7


def test_write_todos(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    ctx = LocalToolContext(
        workspace_path=str(tmp_path),
        runtime_path=str(runtime),
    )
    res = run(
        local_tool_provider.execute(
            "write_todos_local",
            {
                "todos": [
                    {"id": "1", "content": "Find bug", "status": "completed"},
                    {"id": "2", "content": "Fix bug", "status": "in_progress"},
                ]
            },
            ctx,
        )
    )
    assert res.get("count") == 2
    saved = (runtime / "todos.json").read_text()
    assert "Fix bug" in saved
    bad = run(
        local_tool_provider.execute(
            "write_todos_local",
            {"todos": [{"id": "x", "content": "y", "status": "nope"}]},
            ctx,
        )
    )
    assert "error" in bad


def test_task_is_hub_managed_not_inline():
    res = run(
        local_tool_provider.execute(
            "task_local",
            {"prompt": "do something"},
            LocalToolContext(workspace_path="/tmp"),
        )
    )
    assert "error" in res
