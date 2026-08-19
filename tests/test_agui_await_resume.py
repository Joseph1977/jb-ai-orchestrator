# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""End-to-end tests for the persist-and-resume tool loop (no DB / LLM / network).

These exercise the exact code path the orchestrator and AG-UI controllers rely
on: a HITL tool pauses the run by persisting a JSON-serializable ``state`` and
returning; a later request resumes from that state (on any instance) by
injecting the user's answer. The only mocked boundary is
``MCPAgentService.call_litellm`` (the single HTTP hop to LiteLLM).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from app.config import Config
from app.services.agui_event_service import agui_event_service
from app.services.agui_service import agui_service
from app.services.local_tool_provider import LocalToolContext, local_tool_provider
from app.services.tool_hub import AGUIRunContext, ToolExecutionHub


class FakeMCPService:
    """Stands in for MCPAgentService: no MCP servers, scripted LLM replies."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # captured payloads sent to the "LLM"
        self.litellm_request_timeout_in_sec = 30

    async def fetch_mcp_tools(self):
        return []

    def convert_mcp_tools_to_litellm(self, tools):
        return []

    async def call_litellm(self, messages, model="gpt", tools=None,
                           lite_llm_request_timeout_in_sec=None, **kwargs):
        self.calls.append({"messages": json.loads(json.dumps(messages)), "tools": tools})
        assert self._responses, "LLM called more times than scripted"
        return self._responses.pop(0)

    def find_tool_by_name(self, name):  # pragma: no cover - not hit
        return None

    async def execute_mcp_tool(self, name, args):  # pragma: no cover - not hit
        return {}


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
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _llm_text(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {}}


def _hub(fake):
    return ToolExecutionHub(fake, agui_service, agui_event_service)


def test_agui_hitl_await_then_resume_on_fresh_instance():
    async def run():
        frontend_tools = [{
            "name": "AskUser",
            "description": "Ask the user a question",
            "parameters": {"type": "object", "properties": {"question": {"type": "string"}}},
            "extensions": {"awaitsResponse": True},
        }]
        prefixed = agui_service.build_records(frontend_tools)[0].prefixed_name
        ctx = AGUIRunContext(thread_id="t1", run_id="r1")

        # Instance #1 runs until the HITL tool pauses it.
        fake1 = FakeMCPService([_llm_tool_call(prefixed, {"question": "Proceed?"}, "call_hitl")])
        res1 = await _hub(fake1).process_request(
            request="do the thing", include_agui_tools=True,
            agui_context=ctx, frontend_tools=frontend_tools,
        )
        assert res1["awaits_response"] is True
        assert res1["response"] is None
        assert res1["pending_tool"]["tool_call_id"] == "call_hitl"
        assert res1["pending_tool"]["source"] == "AGUI"
        assert any(c["awaits_response"] for c in res1["agui_tool_calls"])

        # State must be fully JSON-serializable so *any* pod can resume it.
        state = res1["state"]
        json.dumps(state)
        assert state["pending_tool"]["tool_call_id"] == "call_hitl"

        # Instance #2 (fresh hub, no shared memory) resumes from the DB-shaped state.
        fake2 = FakeMCPService([_llm_text("All done, thanks!")])
        res2 = await _hub(fake2).process_request(
            request="do the thing", resume_state=state,
            resume_tool_result={"answer": "yes"},
            include_agui_tools=True, agui_context=ctx, frontend_tools=frontend_tools,
        )
        assert res2["success"] is True
        assert res2["response"] == "All done, thanks!"

        # The user's answer was injected as a tool message before resuming.
        resumed_msgs = fake2.calls[0]["messages"]
        tool_msgs = [m for m in resumed_msgs
                     if m.get("role") == "tool" and m.get("tool_call_id") == "call_hitl"]
        assert tool_msgs and "yes" in tool_msgs[0]["content"]

    asyncio.run(run())


def test_local_ask_user_await_then_resume():
    prev = Config.LOCAL_TOOLS_ENABLED
    Config.LOCAL_TOOLS_ENABLED = True
    try:
        async def run():
            ws = tempfile.mkdtemp(prefix="ws_ask_")
            lc = LocalToolContext(workspace_path=ws)
            ask_name = f"ask_user_{local_tool_provider.namespace}"

            fake1 = FakeMCPService([_llm_tool_call(ask_name, {"question": "proceed?"}, "call_a")])
            res1 = await _hub(fake1).process_request(request="x", local_context=lc)
            assert res1["awaits_response"] is True
            assert res1["pending_tool"]["source"] == "LOCAL"
            assert res1["pending_tool"]["function_name"] == ask_name
            json.dumps(res1["state"])

            fake2 = FakeMCPService([_llm_text("done")])
            res2 = await _hub(fake2).process_request(
                request="x", resume_state=res1["state"],
                resume_tool_result={"answer": "go"}, local_context=lc,
            )
            assert res2["success"] is True
            assert res2["response"] == "done"

        asyncio.run(run())
    finally:
        Config.LOCAL_TOOLS_ENABLED = prev


def test_local_read_file_lazy_load_within_workspace():
    prev = Config.LOCAL_TOOLS_ENABLED
    Config.LOCAL_TOOLS_ENABLED = True
    try:
        async def run():
            ws = tempfile.mkdtemp(prefix="ws_read_")
            skill_dir = Path(ws) / "skills"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "demo.md").write_text("HELLO_SKILL_BODY", encoding="utf-8")
            lc = LocalToolContext(workspace_path=ws)

            read_name = f"read_file_{local_tool_provider.namespace}"
            fake = FakeMCPService([
                _llm_tool_call(read_name, {"path": "skills/demo.md"}, "call_r"),
                _llm_text("I have read the skill."),
            ])
            res = await _hub(fake).process_request(request="load the skill", local_context=lc)
            assert res["success"] is True
            assert res["response"] == "I have read the skill."
            assert any(i["tool_source"] == "LOCAL" for i in res["tool_calls_info"])

            # The lazily-loaded file content was fed back to the LLM.
            second_call_msgs = fake.calls[1]["messages"]
            tool_msgs = [m for m in second_call_msgs
                         if m.get("role") == "tool" and m.get("tool_call_id") == "call_r"]
            assert tool_msgs and "HELLO_SKILL_BODY" in tool_msgs[0]["content"]

        asyncio.run(run())
    finally:
        Config.LOCAL_TOOLS_ENABLED = prev


def test_local_read_file_blocks_path_traversal():
    prev = Config.LOCAL_TOOLS_ENABLED
    Config.LOCAL_TOOLS_ENABLED = True
    try:
        async def run():
            ws = tempfile.mkdtemp(prefix="ws_trav_")
            lc = LocalToolContext(workspace_path=ws)
            read_name = f"read_file_{local_tool_provider.namespace}"
            fake = FakeMCPService([
                _llm_tool_call(read_name, {"path": "../../etc/passwd"}, "call_x"),
                _llm_text("handled"),
            ])
            res = await _hub(fake).process_request(request="escape", local_context=lc)
            assert res["success"] is True
            second_call_msgs = fake.calls[1]["messages"]
            tool_msgs = [m for m in second_call_msgs
                         if m.get("role") == "tool" and m.get("tool_call_id") == "call_x"]
            assert tool_msgs
            assert "error" in tool_msgs[0]["content"].lower()

        asyncio.run(run())
    finally:
        Config.LOCAL_TOOLS_ENABLED = prev

