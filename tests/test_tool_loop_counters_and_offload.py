# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Per-segment tool caps, cumulative metrics, and offload dereference tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.config import Config
from app.services.agui_event_service import agui_event_service
from app.services.agui_service import agui_service
from app.services.context_compaction import (
    is_agent_offload_path,
    offload_tool_result_if_needed,
    should_skip_tool_result_offload,
)
from app.services.local_tool_provider import LocalToolContext, local_tool_provider
from app.services.mcp_agent_service import MCPTool
from app.services.tool_call_counters import restore_counters_on_resume
from app.services.tool_hub import AGUIRunContext, ToolExecutionHub


class FakeMCPService:
    def __init__(self, responses, mcp_tools=None):
        self._responses = list(responses)
        self._mcp_tools = list(mcp_tools or [])
        self.calls = []
        self.mcp_executed = []
        self.litellm_request_timeout_in_sec = 30

    async def fetch_mcp_tools(self):
        return self._mcp_tools

    def convert_mcp_tools_to_litellm(self, tools):
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema or {},
                },
            }
            for t in tools
        ]

    async def call_litellm(self, messages, model="gpt", tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        if not self._responses:
            raise AssertionError("unexpected LLM call")
        return self._responses.pop(0)

    def find_tool_by_name(self, name):
        for tool in self._mcp_tools:
            if tool.name == name:
                return tool
        return None

    async def execute_mcp_tool(self, name, args):
        self.mcp_executed.append(name)
        return {"result": f"mcp:{name}"}


def _llm_tool_calls(calls):
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": cid,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                    for name, args, cid in calls
                ],
            }
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _llm_text(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {}}


def _hub(fake):
    return ToolExecutionHub(fake, agui_service, agui_event_service)


def test_is_agent_offload_path():
    assert is_agent_offload_path(".agent/offload/call_1.txt")
    assert is_agent_offload_path("./.agent/offload/x.txt")
    assert not is_agent_offload_path("src/foo.py")


def test_read_offload_path_never_creates_pointer(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(Config, "TOOL_RESULT_OFFLOAD_CHARS", 100)
    runtime = tmp_path / "runtime"
    ctx = LocalToolContext(
        workspace_path=str(tmp_path),
        runtime_path=str(runtime),
    )
    offload_dir = runtime / "offload"
    offload_dir.mkdir(parents=True)
    rel = ".agent/offload/skill.txt"
    content = "skill-body-" + ("x" * 500)
    (offload_dir / "skill.txt").write_text(content, encoding="utf-8")

    result = asyncio.run(
        local_tool_provider.execute("read_file_local", {"path": rel}, ctx)
    )
    assert result.get("alreadyOffloaded") is True
    assert result.get("content") == content

    serialized = json.dumps(result)
    out = offload_tool_result_if_needed(
        serialized,
        tool_call_id="call_read",
        tool_name="read_file_local",
        local_context=ctx,
        skip_offload=should_skip_tool_result_offload(
            tool_name="read_file_local",
            tool_args={"path": rel},
            tool_result=result,
        ),
    )
    assert out == serialized
    payload = json.loads(out)
    assert "offloaded" not in payload


def test_oversized_normal_result_still_offloads(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(Config, "TOOL_RESULT_OFFLOAD_CHARS", 100)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    ctx = LocalToolContext(
        workspace_path=str(tmp_path),
        runtime_path=str(runtime),
    )
    big = "y" * 500
    out = offload_tool_result_if_needed(
        big,
        tool_call_id="call_big",
        tool_name="grep_local",
        local_context=ctx,
    )
    payload = json.loads(out)
    assert payload["offloaded"] is True


def test_restore_counters_legacy_state():
    segment, total = restore_counters_on_resume({"tool_call_count": 42})
    assert segment == 0
    assert total == 42

    segment, total = restore_counters_on_resume(
        {"tool_call_count": 99, "total_tool_call_count": 7}
    )
    assert segment == 0
    assert total == 7


def test_multi_resume_under_segment_cap(monkeypatch):
    monkeypatch.setattr(Config, "MAX_TOOL_CALLS", 2)

    async def run():
        mcp = [
            MCPTool(
                name="sync_tool",
                description="sync",
                input_schema={},
                server_url="http://x",
                server_name="srv",
                original_name="sync_tool",
            )
        ]
        frontend = [{
            "name": "Ask",
            "description": "Ask",
            "parameters": {"type": "object", "properties": {}},
            "extensions": {"awaitsResponse": True},
        }]
        ask_name = agui_service.build_records(frontend)[0].prefixed_name
        ctx = AGUIRunContext(thread_id="t-cap", run_id="r-cap")

        cycles = 4
        state = None
        cumulative = 0
        for i in range(cycles):
            fake = FakeMCPService(
                [
                    _llm_tool_calls([
                        ("sync_tool", {}, f"sync_{i}"),
                        (ask_name, {}, f"ask_{i}"),
                    ])
                ],
                mcp_tools=mcp,
            )
            res = await _hub(fake).process_request(
                request="go",
                resume_state=state,
                resume_tool_results=[{"tool_call_id": f"ask_{i-1}", "result": {"ok": True}}]
                if state
                else None,
                include_agui_tools=True,
                frontend_tools=frontend,
                agui_context=ctx,
            )
            assert res["awaits_response"] is True
            assert res["segment_tool_calls_made"] <= 2
            cumulative = res["tool_calls_made"]
            state = res["state"]
            assert state["segment_tool_call_count"] == res["segment_tool_calls_made"]
            assert state["total_tool_call_count"] == cumulative

        assert cumulative == cycles * 2
        assert cumulative > Config.MAX_TOOL_CALLS

        fake_final = FakeMCPService([_llm_text("done")])
        final = await _hub(fake_final).process_request(
            request="go",
            resume_state=state,
            resume_tool_results=[{"tool_call_id": f"ask_{cycles-1}", "result": {"ok": True}}],
            include_agui_tools=True,
            frontend_tools=frontend,
            agui_context=ctx,
        )
        assert final["success"] is True
        assert final["segment_tool_calls_made"] == 0

    asyncio.run(run())


def test_single_segment_exceeding_cap_fails(monkeypatch):
    monkeypatch.setattr(Config, "MAX_TOOL_CALLS", 2)

    async def run():
        mcp = [
            MCPTool(
                name="sync_tool",
                description="sync",
                input_schema={},
                server_url="http://x",
                server_name="srv",
                original_name="sync_tool",
            )
        ]
        fake = FakeMCPService(
            [
                _llm_tool_calls([
                    ("sync_tool", {}, "a"),
                    ("sync_tool", {}, "b"),
                    ("sync_tool", {}, "c"),
                ]),
            ],
            mcp_tools=mcp,
        )
        res = await _hub(fake).process_request(
            request="go",
            include_agui_tools=False,
        )
        assert res["success"] is False
        assert "Maximum tool calls" in res["error"]
        assert res["segment_tool_calls_made"] == 2
        assert res["tool_calls_made"] == 2
        assert len(fake.mcp_executed) == 2

    asyncio.run(run())


def test_old_state_resume_allows_fresh_segment(monkeypatch):
    monkeypatch.setattr(Config, "MAX_TOOL_CALLS", 2)

    async def run():
        mcp = [
            MCPTool(
                name="sync_tool",
                description="sync",
                input_schema={},
                server_url="http://x",
                server_name="srv",
                original_name="sync_tool",
            )
        ]
        legacy_state = {
            "messages": [{"role": "user", "content": "go"}],
            "request": "go",
            "model": "gpt",
            "max_calls": 2,
            "requested_tools": None,
            "lite_llm_request_timeout_in_sec": 30,
            "tool_call_count": 50,
            "llm_interaction_count": 10,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "litellm_tools": [],
            "agui_records": [],
            "agui_tool_calls": [],
            "tool_calls_info": [],
            "pending_tools": [],
            "pending_tool": {},
        }
        fake = FakeMCPService(
            [
                _llm_tool_calls([("sync_tool", {}, "legacy_1")]),
                _llm_text("ok"),
            ],
            mcp_tools=mcp,
        )
        res = await _hub(fake).process_request(
            request="go",
            resume_state=legacy_state,
            include_agui_tools=False,
        )
        assert res["success"] is True
        assert res["tool_calls_made"] == 51
        assert res["segment_tool_calls_made"] == 1

    asyncio.run(run())


def test_skill_sized_offload_dereference(tmp_path, monkeypatch):
    """~11KB offload artifact is consumable without a second pointer."""
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(Config, "TOOL_RESULT_OFFLOAD_CHARS", 8000)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    ctx = LocalToolContext(
        workspace_path=str(tmp_path),
        runtime_path=str(runtime),
    )
    big = "z" * 11_000
    pointer = offload_tool_result_if_needed(
        big,
        tool_call_id="skill_call",
        tool_name="grep_local",
        local_context=ctx,
    )
    payload = json.loads(pointer)
    assert payload["offloaded"] is True
    rel_path = payload["path"]

    result = asyncio.run(
        local_tool_provider.execute("read_file_local", {"path": rel_path}, ctx)
    )
    assert result.get("alreadyOffloaded") is True
    assert len(result.get("content", "")) == 11_000

    serialized = json.dumps(result)
    assert json.loads(
        offload_tool_result_if_needed(
            serialized,
            tool_call_id="read_skill",
            tool_name="read_file_local",
            local_context=ctx,
            skip_offload=should_skip_tool_result_offload(
                tool_name="read_file_local",
                tool_args={"path": rel_path},
                tool_result=result,
            ),
        )
    ).get("content") == result["content"]


@pytest.mark.asyncio
async def test_cancel_event_before_first_llm_call_skips_litellm():
    cancel_event = asyncio.Event()
    cancel_event.set()
    fake = FakeMCPService([_llm_text("should-not-run")])

    with pytest.raises(asyncio.CancelledError):
        await _hub(fake).process_request(
            request="go",
            include_agui_tools=False,
            cancel_event=cancel_event,
        )

    assert len(fake.calls) == 0


@pytest.mark.asyncio
async def test_cancel_event_stops_before_next_llm_iteration():
    cancel_event = asyncio.Event()
    mcp = [
        MCPTool(
            name="sync_tool",
            description="sync",
            input_schema={},
            server_url="http://x",
            server_name="srv",
            original_name="sync_tool",
        )
    ]
    fake = FakeMCPService(
        [
            _llm_tool_calls([("sync_tool", {}, "a")]),
            _llm_text("should-not-run"),
        ],
        mcp_tools=mcp,
    )
    orig_execute = fake.execute_mcp_tool

    async def execute_mcp_tool(name, args):
        result = await orig_execute(name, args)
        cancel_event.set()
        return result

    fake.execute_mcp_tool = execute_mcp_tool

    with pytest.raises(asyncio.CancelledError):
        await _hub(fake).process_request(
            request="go",
            include_agui_tools=False,
            cancel_event=cancel_event,
        )

    assert len(fake.calls) == 1
    assert fake.mcp_executed == ["sync_tool"]
