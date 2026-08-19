# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Fresh-run LiteLLM tool definitions use registry canonical names."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from app.services.agui_event_service import agui_event_service
from app.services.agui_interrupt import build_interrupts_from_pending
from app.services.agui_service import agui_service
from app.services.mcp_agent_service import MCPTool
from app.services.local_tool_provider import LocalToolContext
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
        return {"result": "mcp"}


def _tool_names(tools):
    return [t["function"]["name"] for t in (tools or []) if t.get("function")]


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


def test_fresh_run_passes_clean_agui_names_to_litellm():
    async def run():
        frontend = [{
            "name": "Ask-Text",
            "description": "Ask text",
            "parameters": {"type": "object", "properties": {}},
            "extensions": {"awaitsResponse": True},
        }]
        rec = agui_service.build_records(frontend)[0]
        fake = FakeMCPService([_llm_tool_call("Ask-Text", {}, "call_1"), _llm_text("x")])
        hub = ToolExecutionHub(fake, agui_service, agui_event_service)
        res = await hub.process_request(
            request="go",
            include_agui_tools=True,
            frontend_tools=frontend,
            agui_context=AGUIRunContext(thread_id="t-clean", run_id="r1"),
        )

        assert fake.calls
        names = _tool_names(fake.calls[0]["tools"])
        assert "Ask-Text" in names
        assert rec.prefixed_name not in names
        assert "AGUI-Ask-Text" not in names

        if res.get("awaits_response"):
            pending = res.get("pending_tool") or (res.get("pending_tools") or [None])[0]
            assert pending["function_name"] == "Ask-Text"
            intr = build_interrupts_from_pending([pending])[0]
            assert intr.metadata["functionName"] == "Ask-Text"
            stored = _tool_names(res["state"]["litellm_tools"])
            assert "Ask-Text" in stored
            assert rec.prefixed_name not in stored

    asyncio.run(run())


def test_segment_backend_credential_is_absent_from_await_state(tmp_path):
    async def run():
        class Backend:
            def __init__(self):
                self.credential = "secret-output-sas"

        fake = FakeMCPService([
            _llm_tool_call(
                "ask_user_local",
                {"question": "Continue?"},
                "call-secret-check",
            )
        ])
        hub = ToolExecutionHub(fake, agui_service, agui_event_service)
        result = await hub.process_request(
            request="go",
            local_context=LocalToolContext(
                workspace_path=str(tmp_path),
                mode="workflow",
                output_backend=Backend(),
            ),
        )
        assert result["awaits_response"] is True
        assert "secret-output-sas" not in json.dumps(result["state"])
        assert "write_output_local" in _tool_names(result["state"]["litellm_tools"])

    asyncio.run(run())


def test_fresh_run_collision_uses_deterministic_alias():
    async def run():
        mcp = [
            MCPTool(
                name="grep_srv",
                description="mcp grep",
                input_schema={},
                server_url="http://x",
                server_name="srv1",
                original_name="grep",
            )
        ]
        frontend = [{
            "name": "grep",
            "description": "ui grep",
            "parameters": {"type": "object", "properties": {}},
        }]
        agui_rec = agui_service.build_records(frontend)[0]
        alias = "grep__agui__frontend"
        fake = FakeMCPService(
            [_llm_tool_call(alias, {}, "call_g"), _llm_text("done")],
            mcp_tools=mcp,
        )
        hub = ToolExecutionHub(fake, agui_service, agui_event_service)
        await hub.process_request(
            request="go",
            include_agui_tools=True,
            frontend_tools=frontend,
        )

        names = _tool_names(fake.calls[0]["tools"])
        assert alias in names
        assert "grep__mcp__srv1" in names
        assert agui_rec.prefixed_name not in names

    asyncio.run(run())


def test_legacy_resume_still_routes_prefixed_tool_names():
    async def run():
        frontend = [{
            "name": "AskUser",
            "description": "Ask",
            "parameters": {"type": "object", "properties": {}},
            "extensions": {"awaitsResponse": True},
        }, {
            "name": "ShowMsg",
            "description": "Show",
            "parameters": {"type": "object", "properties": {}},
        }]
        ask, show = agui_service.build_records(frontend)
        ctx = AGUIRunContext(thread_id="t-legacy", run_id="r1")

        fake1 = FakeMCPService([_llm_tool_call(ask.original_name, {}, "call_1")])
        hub1 = ToolExecutionHub(fake1, agui_service, agui_event_service)
        res1 = await hub1.process_request(
            request="go",
            include_agui_tools=True,
            agui_context=ctx,
            frontend_tools=frontend,
        )
        assert res1["awaits_response"]
        state = res1["state"]

        # Simulate persisted legacy state: prefixed definitions and history.
        state["litellm_tools"] = [
            {"type": "function", "function": {
                "name": t["function"]["name"] if t["function"]["name"] != show.original_name else show.prefixed_name,
                "description": t["function"]["description"],
                "parameters": t["function"]["parameters"],
            }}
            for t in state["litellm_tools"]
        ]
        state["messages"] = state["messages"] + [{
            "role": "tool",
            "tool_call_id": "call_1",
            "name": ask.original_name,
            "content": '{"answer":"yes"}',
        }]
        state["pending_tools"] = []
        state["pending_tool"] = None

        fake2 = FakeMCPService([
            _llm_tool_call(show.prefixed_name, {"msg": "hi"}, "call_2"),
            _llm_text("x"),
        ])
        hub2 = ToolExecutionHub(fake2, agui_service, agui_event_service)
        with patch.object(agui_service, "handle_tool_call", wraps=agui_service.handle_tool_call) as handle:
            res2 = await hub2.process_request(
                request="go",
                resume_state=state,
                include_agui_tools=True,
                agui_context=ctx,
                frontend_tools=frontend,
            )
            assert res2["success"]
            assert handle.called
            assert fake2.mcp_executed == []

    asyncio.run(run())
