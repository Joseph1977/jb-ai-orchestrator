# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Resume idempotency and duplicate tool-result guards."""

from __future__ import annotations

import asyncio
import json

from app.services.agui_event_service import agui_event_service
from app.services.agui_service import agui_service
from app.services.tool_hub import ToolExecutionHub


class FakeMCPService:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.litellm_request_timeout_in_sec = 30

    async def fetch_mcp_tools(self):
        return []

    def convert_mcp_tools_to_litellm(self, tools):
        return []

    async def call_litellm(self, messages, model="gpt", tools=None, **kwargs):
        self.calls.append(json.loads(json.dumps(messages)))
        if self._responses:
            return self._responses.pop(0)
        raise AssertionError("unexpected LLM call")

    def find_tool_by_name(self, name):
        return None

    async def execute_mcp_tool(self, name, args):
        return {}


def test_duplicate_tool_result_not_appended_on_resume():
    async def run():
        frontend = [{
            "name": "AskUser",
            "description": "Ask",
            "parameters": {"type": "object", "properties": {}},
            "extensions": {"awaitsResponse": True},
        }]
        prefixed = agui_service.build_records(frontend)[0].prefixed_name
        fake1 = FakeMCPService([{
            "choices": [{"message": {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "call_x", "type": "function",
                                "function": {"name": prefixed, "arguments": "{}"}}],
            }}],
            "usage": {},
        }])
        hub = ToolExecutionHub(fake1, agui_service, agui_event_service)
        res1 = await hub.process_request(
            request="x", include_agui_tools=True, frontend_tools=frontend,
        )
        state = res1["state"]
        state["messages"].append({
            "role": "tool", "tool_call_id": "call_x", "name": prefixed, "content": '{"answer":"y"}',
        })

        fake2 = FakeMCPService([{
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {},
        }])
        hub2 = ToolExecutionHub(fake2, agui_service, agui_event_service)
        await hub2.process_request(
            request="x",
            resume_state=state,
            resume_tool_results=[{"tool_call_id": "call_x", "result": {"answer": "y"}}],
            include_agui_tools=True,
            frontend_tools=frontend,
        )
        tool_msgs = [m for m in fake2.calls[0] if m.get("role") == "tool" and m.get("tool_call_id") == "call_x"]
        assert len(tool_msgs) == 1

    asyncio.run(run())


def test_try_claim_state_for_resume():
    async def run():
        from app.services.execution_state_service import ExecutionStateService
        from app.models.execution_models import LLMStateStatus

        svc = ExecutionStateService()
        claimed = []

        class FakeResult:
            rowcount = 1

        class FakeSession:
            async def execute(self, stmt):
                claimed.append(stmt.compile().params)
                return FakeResult()

        ok = await svc.try_claim_state_for_resume(FakeSession(), "00000000-0000-0000-0000-000000000001")
        assert ok is True
        assert claimed[0]["status"] == LLMStateStatus.PENDING

    asyncio.run(run())
