# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Multi-tool batch and partial resume tests."""

from __future__ import annotations

import asyncio
import json

from app.services.agui_event_service import agui_event_service
from app.services.agui_service import agui_service
from app.services.tool_hub import AGUIRunContext, ToolExecutionHub


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
        self.calls.append({"messages": json.loads(json.dumps(messages)), "tools": tools})
        return self._responses.pop(0)

    def find_tool_by_name(self, name):
        return None

    async def execute_mcp_tool(self, name, args):
        return {"result": "mcp-ok"}


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


def test_multi_tool_batch_partial_resume():
    async def run():
        frontend = [
            {
                "name": "AskA",
                "description": "Ask A",
                "parameters": {"type": "object", "properties": {}},
                "extensions": {"awaitsResponse": True},
            },
            {
                "name": "AskB",
                "description": "Ask B",
                "parameters": {"type": "object", "properties": {}},
                "extensions": {"awaitsResponse": True},
            },
        ]
        recs = agui_service.build_records(frontend)
        names = [r.prefixed_name for r in recs]
        ctx = AGUIRunContext(thread_id="t-batch", run_id="r-batch")

        fake1 = FakeMCPService([
            _llm_tool_calls([
                (names[0], {}, "call_a"),
                (names[1], {}, "call_b"),
            ])
        ])
        res1 = await _hub(fake1).process_request(
            request="go",
            include_agui_tools=True,
            agui_context=ctx,
            frontend_tools=frontend,
        )
        assert res1["awaits_response"] is True
        pending = res1.get("pending_tools") or [res1["pending_tool"]]
        assert len(pending) == 2

        state = res1["state"]
        fake2 = FakeMCPService([])
        res_partial = await _hub(fake2).process_request(
            request="go",
            resume_state=state,
            resume_tool_results=[{"tool_call_id": "call_a", "result": {"answer": "a"}}],
            include_agui_tools=True,
            agui_context=ctx,
            frontend_tools=frontend,
        )
        assert res_partial["awaits_response"] is True
        assert len(res_partial.get("pending_tools") or []) == 1
        assert fake2.calls == []

        fake3 = FakeMCPService([_llm_text("done both")])
        res_final = await _hub(fake3).process_request(
            request="go",
            resume_state=res_partial["state"],
            resume_tool_results=[{"tool_call_id": "call_b", "result": {"answer": "b"}}],
            include_agui_tools=True,
            agui_context=ctx,
            frontend_tools=frontend,
        )
        assert res_final["success"] is True
        assert res_final["response"] == "done both"

    asyncio.run(run())


def test_canonical_interrupt_payload():
    from app.services.agui_interrupt import build_run_finished_interrupt_event

    evt = build_run_finished_interrupt_event(
        thread_id="t1",
        run_id="r1",
        pending_tools=[{"tool_call_id": "c1", "function_name": "AskUser", "source": "AGUI"}],
        execution_guid="exec-1",
        state_guid="state-1",
    )
    dumped = evt.model_dump(by_alias=True, exclude_none=True)
    assert dumped["outcome"]["type"] == "interrupt"
    assert len(dumped["outcome"]["interrupts"]) == 1
    assert dumped["outcome"]["interrupts"][0]["id"] == "c1"
    assert dumped["outcome"]["interrupts"][0]["toolCallId"] == "c1"
    assert dumped["result"]["awaitsResponse"] is True


def test_pending_tools_interrupt_id_matches_tool_call_id():
    from app.services.tool_hub import ToolExecutionHub
    from unittest.mock import MagicMock

    hub = ToolExecutionHub(MagicMock(), MagicMock(), MagicMock())
    result = hub._build_await_result(
        pending_tools=[
            {"tool_call_id": "call_a", "function_name": "AskA", "source": "AGUI"},
            {"tool_call_id": "call_b", "function_name": "AskB", "source": "AGUI"},
        ],
        messages=[],
        original_prompt="x",
        model="gpt",
        max_calls=10,
        requested_tools=None,
        lite_llm_timeout=30,
        segment_tool_call_count=1,
        total_tool_call_count=1,
        llm_interaction_count=1,
        total_prompt_tokens=0,
        total_completion_tokens=0,
        total_tokens=0,
        litellm_tools=[],
        selected_agui_tools=[],
        executed_tool_calls_info=[],
        forwarded_agui_tool_calls=[],
    )
    for pt in result["pending_tools"]:
        assert pt["interrupt_id"] == pt["tool_call_id"]
    assert {i["id"] for i in result["interrupts"]} == {"call_a", "call_b"}
    assert all(
        value is not None
        for item in result["interrupts"]
        for value in item.values()
    )


def test_normalize_resume_maps_interrupt_id_to_tool_call_id():
    from app.services.agui_interrupt import normalize_resume_responses
    from ag_ui.core.types import ResumeEntry

    state = {
        "pending_tools": [
            {"tool_call_id": "call_a", "interrupt_id": "call_a", "source": "AGUI"},
        ]
    }
    out = normalize_resume_responses(
        resume_entries=[
            ResumeEntry(
                interruptId="call_a",
                status="resolved",
                payload={"result": {"ok": True}},
            )
        ],
        state_payload=state,
    )
    assert len(out) == 1
    assert out[0]["tool_call_id"] == "call_a"
