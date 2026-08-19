# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Resumed-run tool registry reconstruction and AG-UI routing tests."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.agui_event_service import agui_event_service
from app.services.agui_service import AGUIToolRecord, agui_service
from app.services.tool_hub import AGUIRunContext, ToolExecutionHub
from app.services.tool_registry import (
    agui_exposed_names,
    build_active_agui_map,
    build_tool_registry,
    is_agui_litellm_name,
    mcp_tools_from_stored_litellm,
)


def test_agui_exposed_names_includes_clean_and_prefixed():
    rec = agui_service.build_records([{
        "name": "ShowMsg",
        "description": "show",
        "parameters": {"type": "object", "properties": {}},
    }])[0]
    names = agui_exposed_names([rec])
    assert rec.original_name in names
    assert rec.prefixed_name in names
    assert is_agui_litellm_name(rec.original_name, [rec])
    assert is_agui_litellm_name(rec.prefixed_name, [rec])
    assert not is_agui_litellm_name("grep_stored", [rec])


def test_mcp_from_stored_excludes_clean_agui_names():
    rec = agui_service.build_records([{
        "name": "AskUser",
        "description": "ask",
        "parameters": {"type": "object", "properties": {}},
    }])[0]
    litellm_tools = [
        {"type": "function", "function": {"name": rec.original_name, "description": "d", "parameters": {}}},
        {"type": "function", "function": {"name": "mcp_tool_x", "description": "d", "parameters": {}}},
        {"type": "function", "function": {"name": f"read_file_local", "description": "d", "parameters": {}}},
    ]
    mcp = mcp_tools_from_stored_litellm(
        litellm_tools,
        agui_records=[rec],
        local_namespace="local",
        is_local_tool=lambda n: n.endswith("_local"),
    )
    assert len(mcp) == 1
    assert mcp[0].name == "mcp_tool_x"


def test_active_agui_map_canonical_and_aliases():
    rec = agui_service.build_records([{
        "name": "ToolX",
        "description": "x",
        "parameters": {"type": "object", "properties": {}},
    }])[0]
    reg = build_tool_registry(mcp_tools=[], agui_records=[rec], local_litellm_tools=[])
    amap = build_active_agui_map(reg)
    assert amap[rec.original_name] is rec
    assert amap[rec.prefixed_name] is rec


class FakeMCPService:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.mcp_executed = []
        self.litellm_request_timeout_in_sec = 30

    async def fetch_mcp_tools(self):
        return []

    def convert_mcp_tools_to_litellm(self, tools):
        return []

    async def call_litellm(self, messages, model="gpt", tools=None, **kwargs):
        self.calls.append({"messages": json.loads(json.dumps(messages)), "tools": tools})
        return self._responses.pop(0)

    def find_tool_by_name(self, name):
        return None

    async def execute_mcp_tool(self, name, args):
        self.mcp_executed.append(name)
        return {"result": "mcp"}


def _llm_tool_call(name, args, call_id):
    return {
        "choices": [{"message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }],
        }}],
        "usage": {},
    }


def _llm_text(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {}}


def test_resumed_agui_tool_routes_clean_name_not_mcp():
    async def run():
        frontend = [
            {
                "name": "AskUser",
                "description": "Ask",
                "parameters": {"type": "object", "properties": {}},
                "extensions": {"awaitsResponse": True},
            },
            {
                "name": "ShowMsg",
                "description": "Show",
                "parameters": {"type": "object", "properties": {}},
            },
        ]
        records = agui_service.build_records(frontend)
        ask, show = records[0], records[1]
        ctx = AGUIRunContext(thread_id="t-route", run_id="r1")

        fake1 = FakeMCPService([_llm_tool_call(ask.prefixed_name, {}, "call_ask")])
        res1 = await ToolExecutionHub(fake1, agui_service, agui_event_service).process_request(
            request="go",
            include_agui_tools=True,
            agui_context=ctx,
            frontend_tools=frontend,
        )
        state = res1["state"]

        fake2 = FakeMCPService([
            _llm_tool_call(show.original_name, {"msg": "hi"}, "call_show"),
            _llm_text("done"),
        ])
        hub2 = ToolExecutionHub(fake2, agui_service, agui_event_service)
        with patch.object(agui_service, "handle_tool_call", wraps=agui_service.handle_tool_call) as handle:
            res2 = await hub2.process_request(
                request="go",
                resume_state=state,
                resume_tool_result={"answer": "yes"},
                include_agui_tools=True,
                agui_context=ctx,
                frontend_tools=frontend,
            )
            assert res2["success"]
            assert handle.called
            assert fake2.mcp_executed == []

        state["litellm_tools"] = [
            {"type": "function", "function": {
                "name": show.prefixed_name,
                "description": show.description,
                "parameters": show.parameters,
            }}
            if t["function"]["name"] == show.original_name
            else t
            for t in state["litellm_tools"]
        ]
        state["messages"] = state["messages"] + [{
            "role": "tool",
            "tool_call_id": "call_ask",
            "name": ask.prefixed_name,
            "content": '{"answer":"yes"}',
        }]
        state["pending_tools"] = []
        state["pending_tool"] = None

        fake3 = FakeMCPService([_llm_tool_call(show.prefixed_name, {}, "call_legacy"), _llm_text("x")])
        hub3 = ToolExecutionHub(fake3, agui_service, agui_event_service)
        with patch.object(agui_service, "handle_tool_call", wraps=agui_service.handle_tool_call) as handle2:
            await hub3.process_request(
                request="go",
                resume_state=state,
                include_agui_tools=True,
                agui_context=ctx,
                frontend_tools=frontend,
            )
            assert handle2.called
            assert fake3.mcp_executed == []

    asyncio.run(run())
