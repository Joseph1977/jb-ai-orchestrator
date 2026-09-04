# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Deterministic acceptance tests for generic durable-output routing."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from app.config import Config
from app.controllers.orchestrator_controller import _local_context
from app.services.binding_runtime import binding_system_prompt, segment_output_backend
from app.services.context_compaction import offload_tool_result_if_needed
from app.services.local_tool_provider import local_tool_provider


def run(coro):
    return asyncio.run(coro)


def output_config(output_root, *, relative_path=".", input_binding=True):
    return {
        "mode": "workflow",
        "runtimePath": str(output_root.parent / "runtime"),
        "input": (
            {
                "type": "shared_folder",
                "uri": str(output_root.parent / "input"),
                "relativePath": ".",
                "materialization": "in_place_read_only",
            }
            if input_binding
            else None
        ),
        "output": {
            "type": "shared_folder",
            "uri": str(output_root),
            "relativePath": relative_path,
        },
    }


def execution(workspace):
    return SimpleNamespace(id=None, workspace_path=str(workspace))


def tool_names(ctx):
    return {
        item["function"]["name"]
        for item in local_tool_provider.list_litellm_tools(
            mode=ctx.mode,
            output_bound=ctx.output_backend is not None,
        )
    }


def test_requested_output_root_and_prefix_are_applied_once(tmp_path):
    input_root = tmp_path / "input"
    output_root = tmp_path / "requested-output"
    input_root.mkdir()
    config = output_config(output_root, relative_path="tenant-a")
    ctx = _local_context(execution(input_root), config)

    result = run(
        local_tool_provider.execute(
            "write_output_local",
            {"path": "reports/final.md", "content": "durable"},
            ctx,
        )
    )

    assert result["durable"] is True
    assert (output_root / "tenant-a" / "reports" / "final.md").read_text() == "durable"
    assert not (output_root / "tenant-a" / "tenant-a" / "reports" / "final.md").exists()
    assert not (output_root / "reports" / "final.md").exists()
    assert not (input_root / "reports" / "final.md").exists()


def test_same_logical_path_keeps_input_and_output_separate(tmp_path):
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    (input_root / "state.json").write_text('{"source":"input"}')
    ctx = _local_context(execution(input_root), output_config(output_root))

    run(
        local_tool_provider.execute(
            "write_output_local",
            {"path": "state.json", "content": '{"source":"output"}'},
            ctx,
        )
    )

    assert json.loads((input_root / "state.json").read_text()) == {"source": "input"}
    assert json.loads((output_root / "state.json").read_text()) == {"source": "output"}
    assert "write_file_local" not in tool_names(ctx)
    assert "write_output_local" in tool_names(ctx)
    assert "edit_output_local" in tool_names(ctx)


def test_distinct_output_bindings_do_not_cross_write(tmp_path):
    first_input = tmp_path / "input-a"
    second_input = tmp_path / "input-b"
    first_input.mkdir()
    second_input.mkdir()
    first_output = tmp_path / "output-a"
    second_output = tmp_path / "output-b"
    first = _local_context(execution(first_input), output_config(first_output))
    second = _local_context(execution(second_input), output_config(second_output))

    run(
        local_tool_provider.execute(
            "write_output_local", {"path": "state.json", "content": "first"}, first
        )
    )
    run(
        local_tool_provider.execute(
            "write_output_local", {"path": "state.json", "content": "second"}, second
        )
    )

    assert (first_output / "state.json").read_text() == "first"
    assert (second_output / "state.json").read_text() == "second"


def test_unbound_workflow_cannot_create_input_or_output_files(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    config = {
        "mode": "workflow",
        "runtimePath": str(tmp_path / "runtime"),
        "input": {
            "type": "shared_folder",
            "uri": str(input_root),
            "relativePath": ".",
            "materialization": "in_place_read_only",
        },
        "output": None,
    }
    ctx = _local_context(execution(input_root), config)

    output_result = run(
        local_tool_provider.execute(
            "write_output_local", {"path": "state.json", "content": "bad"}, ctx
        )
    )
    input_result = run(
        local_tool_provider.execute(
            "write_file_local", {"path": "state.json", "content": "bad"}, ctx
        )
    )

    assert output_result["error"] == "No durable output store is bound"
    assert input_result["errorCode"] == "INPUT_WRITE_FORBIDDEN"
    assert "write_output_local" not in tool_names(ctx)
    assert "edit_output_local" not in tool_names(ctx)
    assert "write_file_local" not in tool_names(ctx)
    assert not (input_root / "state.json").exists()


def test_output_backend_binding_does_not_depend_on_input_config(tmp_path):
    output_root = tmp_path / "output-only"
    config = output_config(output_root, input_binding=False)
    backend = segment_output_backend(config)

    assert backend is not None
    assert config["input"] is None
    assert run(backend.write_text("artifact.txt", "output-only")) == len("output-only")

    assert (output_root / "artifact.txt").read_text() == "output-only"


def test_binding_prompt_is_explicit_and_secret_free(tmp_path):
    physical_uri = str(tmp_path / "private-output")
    token = "secret-output-token"
    config = output_config(tmp_path / "private-output", relative_path="tenant-a")
    config["credentials"] = {"outputAccessToken": token}

    prompt = binding_system_prompt(config)
    normalized_prompt = " ".join(prompt.split())

    assert (
        "The bound output store is the sole destination for every durable write"
        in normalized_prompt
    )
    assert "write_output_local to create or fully replace them" in normalized_prompt
    assert "prefer edit_output_local for an exact targeted replacement" in normalized_prompt
    assert "The output backend already applies that prefix" in normalized_prompt
    assert "do not prepend the prefix or a physical URI" in normalized_prompt
    assert "Logical prefix: tenant-a" in prompt
    assert physical_uri not in prompt
    assert token not in prompt

    unbound = binding_system_prompt({"mode": "workflow", "output": None})
    normalized_unbound = " ".join(unbound.split())
    assert (
        "Do not invent an output location or claim that generated artifacts were persisted"
        in normalized_unbound
    )


def test_offload_remains_runtime_local_when_output_is_bound(tmp_path, monkeypatch):
    class FailingBackend:
        def __getattribute__(self, name):
            if name.startswith("_"):
                return object.__getattribute__(self, name)
            raise AssertionError("runtime offload touched durable output")

    input_root = tmp_path / "input"
    input_root.mkdir()
    output_root = tmp_path / "output"
    config = output_config(output_root)
    ctx = _local_context(execution(input_root), config)
    ctx.output_backend = FailingBackend()
    monkeypatch.setattr(Config, "TOOL_RESULT_OFFLOAD_CHARS", 5)

    pointer = offload_tool_result_if_needed(
        "content larger than threshold",
        tool_call_id="call-1",
        tool_name="test",
        local_context=ctx,
    )

    assert json.loads(pointer)["offloaded"] is True
    assert (tmp_path / "runtime" / "offload" / "call-1.txt").is_file()
    assert not output_root.exists()
