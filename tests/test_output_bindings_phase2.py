# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.config import Config
from app.controllers.orchestrator_controller import _local_context
from app.services.context_compaction import offload_tool_result_if_needed
from app.services.binding_runtime import binding_system_prompt
from app.services.local_tool_provider import LocalToolContext, local_tool_provider
from app.services.storage.base import StorageError
from app.services.storage.azure_blob import AzureBlobBackend
from app.services.storage.factory import build_output_backend
from app.services.storage.shared_folder import SharedFolderBackend


def run(coro):
    return asyncio.run(coro)


def test_shared_folder_round_trip_and_locked_paths(tmp_path):
    backend = SharedFolderBackend(str(tmp_path))
    paths = [
        "output/result.md",
        ".claude/global_memory.json",
        "projects/a/memory.json",
        "projects/a/01_plan.md",
        "output/.cursor/state.json",
    ]
    for path in paths:
        assert run(backend.write_text(path, path)) == len(path)
        assert run(backend.exists(path))
        assert run(backend.read_text(path)) == path
    listed = run(backend.list())
    assert "output/result.md" in listed.entries


@pytest.mark.parametrize("path", ["../escape", "/absolute", ".agent/todos.json"])
def test_output_paths_are_confined(tmp_path, path):
    backend = SharedFolderBackend(str(tmp_path))
    with pytest.raises(StorageError):
        run(backend.write_text(path, "x"))


def test_output_relative_path_is_applied(tmp_path):
    (tmp_path / "prefix").mkdir()
    backend = build_output_backend(
        {"type": "shared_folder", "uri": str(tmp_path), "relativePath": "prefix"}
    )
    run(backend.write_text("result.md", "ok"))
    assert (tmp_path / "prefix" / "result.md").read_text() == "ok"


def test_shared_folder_caps(monkeypatch, tmp_path):
    backend = SharedFolderBackend(str(tmp_path))
    monkeypatch.setattr(Config, "OUTPUT_WRITE_MAX_BYTES", 2)
    with pytest.raises(StorageError) as exc:
        run(backend.write_text("too-big.txt", "abc"))
    assert exc.value.code == "STORAGE_LIMIT_EXCEEDED"

    monkeypatch.setattr(Config, "OUTPUT_WRITE_MAX_BYTES", 100)
    run(backend.write_text("one.txt", "1"))
    run(backend.write_text("two.txt", "2"))
    monkeypatch.setattr(Config, "OUTPUT_LIST_MAX_ENTRIES", 1)
    assert len(run(backend.list()).entries) == 1


def test_azure_requires_fresh_credential_without_exposing_uri():
    binding = {
        "type": "azure_blob",
        "uri": "https://secret.blob.core.windows.net/private",
        "relativePath": ".",
    }
    with pytest.raises(StorageError) as exc:
        build_output_backend(binding)
    assert exc.value.code == "OUTPUT_CREDENTIAL_REQUIRED"
    assert "secret.blob" not in exc.value.message


def test_azure_forbidden_error_is_secret_free(monkeypatch):
    class Forbidden(Exception):
        status_code = 403

    class Blob:
        async def get_blob_properties(self):
            raise Forbidden("SAS=secret")

    class Client:
        def get_blob_client(self, name):
            return Blob()

        async def close(self):
            pass

    backend = AzureBlobBackend(
        "https://account.blob.core.windows.net/container",
        access_token="secret-token",
    )
    monkeypatch.setattr(backend, "_client", lambda: Client())
    with pytest.raises(StorageError) as exc:
        run(backend.exists("state.json"))
    assert exc.value.code == "OUTPUT_FORBIDDEN"
    assert "secret" not in exc.value.message


def test_workflow_hides_and_blocks_input_mutators(tmp_path):
    ctx = LocalToolContext(workspace_path=str(tmp_path), mode="workflow")
    names = {
        item["function"]["name"]
        for item in local_tool_provider.list_litellm_tools(
            mode="workflow", output_bound=False
        )
    }
    assert "write_file_local" not in names
    assert "execute_local" not in names
    assert "write_output_local" not in names
    assert "read_file_local" in names
    blocked = run(
        local_tool_provider.execute(
            "write_file_local", {"path": "x", "content": "bad"}, ctx
        )
    )
    assert blocked["errorCode"] == "INPUT_WRITE_FORBIDDEN"
    assert not (tmp_path / "x").exists()


def test_working_copy_and_output_can_both_write(tmp_path):
    workspace = tmp_path / "input"
    output = tmp_path / "output"
    workspace.mkdir()
    ctx = LocalToolContext(
        workspace_path=str(workspace),
        mode="working_copy",
        output_backend=SharedFolderBackend(str(output)),
    )
    assert "bytesWritten" in run(
        local_tool_provider.execute(
            "write_file_local", {"path": "input.txt", "content": "input"}, ctx
        )
    )
    assert "bytesWritten" in run(
        local_tool_provider.execute(
            "write_output_local", {"path": "durable.txt", "content": "output"}, ctx
        )
    )
    assert (workspace / "input.txt").read_text() == "input"
    assert (output / "durable.txt").read_text() == "output"


def test_edit_output_replaces_target_and_preserves_other_values(tmp_path):
    output = tmp_path / "output"
    ctx = LocalToolContext(
        workspace_path=str(tmp_path),
        output_backend=SharedFolderBackend(str(output)),
    )
    original = '{"first":"one","second":"two","third":"three"}'
    run(
        local_tool_provider.execute(
            "write_output_local",
            {"path": "state.json", "content": original},
            ctx,
        )
    )

    first = run(
        local_tool_provider.execute(
            "edit_output_local",
            {"path": "state.json", "old_string": '"two"', "new_string": '"updated"'},
            ctx,
        )
    )
    second = run(
        local_tool_provider.execute(
            "edit_output_local",
            {"path": "state.json", "old_string": '"three"', "new_string": '"final"'},
            ctx,
        )
    )

    assert first["replacements"] == 1
    assert second["replacements"] == 1
    assert first["durable"] is True
    assert json.loads((output / "state.json").read_text()) == {
        "first": "one",
        "second": "updated",
        "third": "final",
    }


@pytest.mark.parametrize(
    ("args", "error_text"),
    [
        (
            {"path": "state.txt", "old_string": "missing", "new_string": "new"},
            "old_string not found",
        ),
        (
            {"path": "state.txt", "old_string": "", "new_string": "new"},
            "old_string is required and must be non-empty",
        ),
    ],
)
def test_edit_output_rejects_invalid_needles_without_changing_file(
    tmp_path, args, error_text
):
    output = tmp_path / "output"
    backend = SharedFolderBackend(str(output))
    run(backend.write_text("state.txt", "original value"))
    ctx = LocalToolContext(workspace_path=str(tmp_path), output_backend=backend)

    result = run(local_tool_provider.execute("edit_output_local", args, ctx))

    assert error_text in result["error"]
    assert (output / "state.txt").read_text() == "original value"


def test_edit_output_requires_unique_match_unless_replace_all(tmp_path):
    output = tmp_path / "output"
    backend = SharedFolderBackend(str(output))
    run(backend.write_text("state.txt", "same and same"))
    ctx = LocalToolContext(workspace_path=str(tmp_path), output_backend=backend)
    args = {"path": "state.txt", "old_string": "same", "new_string": "new"}

    ambiguous = run(local_tool_provider.execute("edit_output_local", args, ctx))
    assert "matched 2 times" in ambiguous["error"]
    assert (output / "state.txt").read_text() == "same and same"

    replaced = run(
        local_tool_provider.execute(
            "edit_output_local", {**args, "replace_all": True}, ctx
        )
    )
    assert replaced["replacements"] == 2
    assert (output / "state.txt").read_text() == "new and new"


def test_edit_output_missing_file_and_unbound_output(tmp_path):
    bound = LocalToolContext(
        workspace_path=str(tmp_path),
        output_backend=SharedFolderBackend(str(tmp_path / "output")),
    )
    missing = run(
        local_tool_provider.execute(
            "edit_output_local",
            {"path": "missing.txt", "old_string": "old", "new_string": "new"},
            bound,
        )
    )
    assert missing["errorCode"] == "STORAGE_NOT_FOUND"
    assert not (tmp_path / "output" / "missing.txt").exists()

    unbound = LocalToolContext(workspace_path=str(tmp_path), mode="workflow")
    result = run(
        local_tool_provider.execute(
            "edit_output_local",
            {"path": "state.txt", "old_string": "old", "new_string": "new"},
            unbound,
        )
    )
    names = {
        item["function"]["name"]
        for item in local_tool_provider.list_litellm_tools(
            mode="workflow", output_bound=False
        )
    }
    assert result["error"] == "No durable output store is bound"
    assert "edit_output_local" not in names


@pytest.mark.parametrize("path", ["../escape", "/absolute", ".agent/todos.json"])
def test_edit_output_paths_are_confined(tmp_path, path):
    ctx = LocalToolContext(
        workspace_path=str(tmp_path),
        output_backend=SharedFolderBackend(str(tmp_path / "output")),
    )
    result = run(
        local_tool_provider.execute(
            "edit_output_local",
            {"path": path, "old_string": "old", "new_string": "new"},
            ctx,
        )
    )
    assert result["errorCode"] == "STORAGE_PATH_INVALID"


def test_edit_output_enforces_read_and_write_limits_without_changing_file(
    monkeypatch, tmp_path
):
    output = tmp_path / "output"
    backend = SharedFolderBackend(str(output))
    ctx = LocalToolContext(workspace_path=str(tmp_path), output_backend=backend)
    monkeypatch.setattr(Config, "OUTPUT_READ_MAX_BYTES", 100)
    monkeypatch.setattr(Config, "OUTPUT_WRITE_MAX_BYTES", 100)
    run(backend.write_text("state.txt", "old"))

    monkeypatch.setattr(Config, "OUTPUT_WRITE_MAX_BYTES", 2)
    write_result = run(
        local_tool_provider.execute(
            "edit_output_local",
            {"path": "state.txt", "old_string": "old", "new_string": "larger"},
            ctx,
        )
    )
    assert write_result["errorCode"] == "STORAGE_LIMIT_EXCEEDED"
    assert (output / "state.txt").read_text() == "old"

    monkeypatch.setattr(Config, "OUTPUT_WRITE_MAX_BYTES", 100)
    monkeypatch.setattr(Config, "OUTPUT_READ_MAX_BYTES", 2)
    read_result = run(
        local_tool_provider.execute(
            "edit_output_local",
            {"path": "state.txt", "old_string": "old", "new_string": "new"},
            ctx,
        )
    )
    assert read_result["errorCode"] == "STORAGE_LIMIT_EXCEEDED"
    assert (output / "state.txt").read_text() == "old"


def test_partitioned_list_and_dual_read(tmp_path):
    (tmp_path / "same.txt").write_text("input")
    output = tmp_path / "durable"
    backend = SharedFolderBackend(str(output))
    run(backend.write_text("same.txt", "output"))
    run(backend.write_text("only-output.txt", "durable"))
    ctx = LocalToolContext(workspace_path=str(tmp_path), output_backend=backend)

    listed = run(local_tool_provider.execute("list_files_local", {"path": "."}, ctx))
    assert listed["output"]["bound"] is True
    assert "same.txt" in listed["input"]["entries"]
    assert "same.txt" in listed["output"]["entries"]

    overlap = run(local_tool_provider.execute("read_file_local", {"path": "same.txt"}, ctx))
    assert overlap["content"] == "input"
    assert overlap["outputExists"] is True
    assert "read_output_local" in overlap["note"]

    hint = run(
        local_tool_provider.execute("read_file_local", {"path": "only-output.txt"}, ctx)
    )
    assert hint["outputExists"] is True
    assert "read_output_local" in hint["note"]

    output_only_list = run(
        local_tool_provider.execute("list_files_local", {"path": "missing-prefix"}, ctx)
    )
    assert output_only_list["input"]["entries"] == []
    assert output_only_list["output"]["bound"] is True


def test_unbound_list_shape(tmp_path):
    ctx = LocalToolContext(workspace_path=str(tmp_path), mode="workflow")
    listed = run(local_tool_provider.execute("list_files_local", {}, ctx))
    assert listed["output"] == {"bound": False, "entries": []}


def test_reserved_runtime_paths_never_touch_output_backend(tmp_path):
    class FailingBackend:
        async def exists(self, path):
            raise AssertionError("runtime read reached output backend")

        async def list(self, path=".", *, next_token=None):
            raise AssertionError("runtime list reached output backend")

    runtime = tmp_path / "runtime"
    (runtime / "todos.json").parent.mkdir(parents=True)
    (runtime / "todos.json").write_text('{"todos":[]}')
    ctx = LocalToolContext(
        workspace_path=str(tmp_path / "input"),
        runtime_path=str(runtime),
        output_backend=FailingBackend(),
    )
    (tmp_path / "input").mkdir()

    read = run(
        local_tool_provider.execute(
            "read_file_local", {"path": ".agent/todos.json"}, ctx
        )
    )
    assert json.loads(read["content"]) == {"todos": []}
    listed = run(
        local_tool_provider.execute(
            "list_files_local", {"path": ".agent", "recursive": True}, ctx
        )
    )
    assert listed["output"] == {"bound": True, "entries": []}


def test_offload_with_output_bound_stays_in_runtime(tmp_path, monkeypatch):
    class FailingBackend:
        def __getattribute__(self, name):
            if name.startswith("_"):
                return object.__getattribute__(self, name)
            raise AssertionError("offload touched output backend")

    monkeypatch.setattr(Config, "TOOL_RESULT_OFFLOAD_CHARS", 5)
    runtime = tmp_path / "runtime"
    workspace = tmp_path / "input"
    workspace.mkdir()
    ctx = LocalToolContext(
        workspace_path=str(workspace),
        runtime_path=str(runtime),
        output_backend=FailingBackend(),
    )
    pointer = offload_tool_result_if_needed(
        "content larger than threshold",
        tool_call_id="call-1",
        tool_name="test",
        local_context=ctx,
    )
    assert json.loads(pointer)["offloaded"] is True
    assert (runtime / "offload" / "call-1.txt").is_file()


def test_orchestrator_context_rebuilds_backend_with_fresh_token(tmp_path):
    config = {
        "mode": "workflow",
        "runtimePath": str(tmp_path / "runtime"),
        "output": {
            "type": "azure_blob",
            "uri": "https://account.blob.core.windows.net/container",
            "relativePath": "state",
        },
    }
    execution = SimpleNamespace(
        id="execution-1",
        source=None,
        config=config,
        workspace_path=str(tmp_path),
    )
    first = _local_context(execution, output_access_token="first-sas")
    second = _local_context(execution, output_access_token="second-sas")
    assert first.output_backend is not second.output_backend
    assert first.output_backend._credential == "first-sas"
    assert second.output_backend._credential == "second-sas"
    assert "first-sas" not in json.dumps(config)


def test_write_output_log_omits_content(caplog, tmp_path):
    ctx = LocalToolContext(
        workspace_path=str(tmp_path),
        output_backend=SharedFolderBackend(str(tmp_path / "output")),
    )
    with caplog.at_level("INFO"):
        run(
            local_tool_provider.execute(
                "write_output_local",
                {"path": "state.txt", "content": "private-output-value"},
                ctx,
            )
        )
    assert "private-output-value" not in caplog.text
    assert "contentChars" in caplog.text


def test_edit_output_log_omits_old_and_new_strings(caplog, tmp_path):
    output = tmp_path / "output"
    backend = SharedFolderBackend(str(output))
    run(backend.write_text("state.txt", "private-old-value"))
    ctx = LocalToolContext(workspace_path=str(tmp_path), output_backend=backend)

    with caplog.at_level("INFO"):
        run(
            local_tool_provider.execute(
                "edit_output_local",
                {
                    "path": "state.txt",
                    "old_string": "private-old-value",
                    "new_string": "private-new-value",
                },
                ctx,
            )
        )

    assert "private-old-value" not in caplog.text
    assert "private-new-value" not in caplog.text
    assert "oldStringChars" in caplog.text
    assert "newStringChars" in caplog.text


def test_binding_prompt_contains_no_physical_location_or_token(monkeypatch):
    monkeypatch.setattr(Config, "OUTPUT_BINDINGS_ENABLED", True)
    prompt = binding_system_prompt(
        {
            "mode": "workflow",
            "output": {
                "type": "azure_blob",
                "uri": "https://secret.blob.core.windows.net/private",
                "relativePath": "projects",
            },
        }
    )
    assert "azure_blob" in prompt
    assert "projects" in prompt
    assert "write_output_local" in prompt
    assert "edit_output_local" in prompt
    assert "secret.blob" not in prompt
