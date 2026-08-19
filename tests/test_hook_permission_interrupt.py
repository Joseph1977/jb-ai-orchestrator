# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Generic hook permission interrupts — no synthetic frontend tool call events."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest

from ag_ui.core.events import RunFinishedEvent, ToolCallEndEvent, ToolCallStartEvent

from app.services.agui_interrupt import (
    HOOK_PERMISSION_REASON,
    HOOK_PERMISSION_RESPONSE_SCHEMA,
    HOOK_PERMISSION_SOURCE,
    build_interrupts_from_pending,
    build_run_finished_interrupt_event,
)
from app.services.agui_event_service import agui_event_service
from app.services.agui_service import agui_service
from app.services.local_tool_provider import LocalToolContext
from app.services.tool_hub import AGUIRunContext, ToolExecutionHub


def _llm_tool_call(name, args, call_id):
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }],
            }
        }],
        "usage": {},
    }


def _llm_text(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {}}


class FakeMCPService:
    litellm_request_timeout_in_sec = 30

    def __init__(self, responses=None):
        self._responses = list(responses or [])

    async def fetch_mcp_tools(self):
        return []

    async def call_litellm(self, messages, model="gpt", tools=None, **kwargs):
        if not self._responses:
            raise AssertionError("unexpected LLM call")
        return self._responses.pop(0)


def _hook_workspace(tmp_path, monkeypatch, dotdirs):
    monkeypatch.setattr("app.config.Config.HOOKS_ENABLED", True)
    monkeypatch.setattr("app.config.Config.LOCAL_SHELL_ENABLED", True)
    monkeypatch.setattr("app.config.Config.PATH_POLICY_ENABLED", False)
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    script = cursor / "ask.sh"
    script.write_text(
        "#!/bin/sh\necho '{\"permission\":\"ask\",\"userMessage\":\"confirm shell?\"}'\n"
    )
    os.chmod(script, 0o755)
    (cursor / "hooks.json").write_text(
        json.dumps({
            "version": 1,
            "hooks": {"beforeShellExecution": [{"command": str(script)}]},
        })
    )
    return LocalToolContext(workspace_path=str(tmp_path), in_place=True)


@pytest.mark.parametrize("decision,blocked", [("approve", False), ("deny", True)])
def test_hook_permission_resume_emits_deferred_tool_result_only(tmp_path, monkeypatch, dotdirs, decision, blocked):
    async def run():
        ctx = _hook_workspace(tmp_path, monkeypatch, dotdirs)
        deferred_id = "call_deferred"
        hub = ToolExecutionHub(
            FakeMCPService([
                _llm_tool_call("execute_local", {"command": "echo hi"}, deferred_id),
                _llm_text("done"),
                _llm_text("finished"),
            ]),
            agui_service,
            agui_event_service,
        )
        publish = AsyncMock()
        hub.agui_event_service.publish_tool_call = publish

        first = await hub.process_request(
            request="run shell",
            initial_messages=[{"role": "user", "content": "run shell"}],
            local_context=ctx,
            model="gpt-4o",
        )

        assert first["awaits_response"]
        publish.assert_not_called()
        pending = first["pending_tool"]
        assert pending["source"] == HOOK_PERMISSION_SOURCE
        assert pending["reason"] == HOOK_PERMISSION_REASON
        assert pending["original_tool_call_id"] == deferred_id
        permission_id = pending["tool_call_id"]
        assert pending["interrupt_id"] == permission_id

        intr = build_interrupts_from_pending([pending])[0]
        assert intr.id == permission_id
        assert intr.reason == HOOK_PERMISSION_REASON
        assert intr.tool_call_id == deferred_id
        assert intr.metadata["deferredToolCallId"] == deferred_id
        assert intr.response_schema == HOOK_PERMISSION_RESPONSE_SCHEMA
        assert "decision" in (intr.response_schema or {}).get("required", [])
        assert "approved" not in (intr.response_schema or {}).get("properties", {})

        resume_payload = (
            {"decision": "approve"}
            if decision == "approve"
            else {"decision": "deny", "reason": "no"}
        )
        resume_payload["interrupt_id"] = permission_id
        resume_payload["tool_call_id"] = permission_id

        state = first["state"]
        events, updated = await hub.prepare_hook_permission_resume_events(
            state_payload=dict(state),
            resume_tool_results=[resume_payload],
            local_context=ctx,
            model="gpt-4o",
            lite_llm_timeout=30,
            agui_context=None,
        )
        assert len(events) == 1
        assert events[0].tool_call_id == deferred_id
        assert events[0].type == "TOOL_CALL_RESULT"

        tool_msgs = [
            m for m in updated["messages"]
            if m.get("role") == "tool" and m.get("tool_call_id") == deferred_id
        ]
        assert len(tool_msgs) == 1
        body = json.loads(tool_msgs[0]["content"])
        if blocked:
            assert body.get("hookBlocked") is True
            assert body.get("error") == "no"
        assert updated["pending_tools"] == []

        second = await hub.process_request(
            request=state["request"],
            resume_state=updated,
            resume_tool_results=[resume_payload],
            local_context=ctx,
            model="gpt-4o",
        )
        assert second.get("success")

    asyncio.run(run())


def test_run_finished_interrupt_has_no_tool_call_events(tmp_path, monkeypatch, dotdirs):
    async def run():
        ctx = _hook_workspace(tmp_path, monkeypatch, dotdirs)
        hub = ToolExecutionHub(
            FakeMCPService([_llm_tool_call("execute_local", {"command": "echo hi"}, "c1")]),
            agui_service,
            agui_event_service,
        )
        publish = AsyncMock()
        hub.agui_event_service.publish_tool_call = publish

        result = await hub.process_request(
            request="go",
            initial_messages=[{"role": "user", "content": "go"}],
            local_context=ctx,
            model="gpt-4o",
            agui_context=AGUIRunContext(thread_id="t1", run_id="r1"),
        )

        publish.assert_not_called()
        pending = result["pending_tools"]
        event = build_run_finished_interrupt_event(
            thread_id="t1",
            run_id="r1",
            pending_tools=pending,
        )
        assert isinstance(event, RunFinishedEvent)
        assert event.outcome.type == "interrupt"
        assert event.outcome.interrupts[0].reason == HOOK_PERMISSION_REASON
        forbidden = (ToolCallStartEvent, ToolCallEndEvent)
        assert not any(isinstance(event, cls) for cls in forbidden)

    asyncio.run(run())


def test_stable_permission_interrupt_id_across_build(tmp_path, monkeypatch, dotdirs):
    async def run():
        ctx = _hook_workspace(tmp_path, monkeypatch, dotdirs)
        hub = ToolExecutionHub(
            FakeMCPService([_llm_tool_call("execute_local", {"command": "echo hi"}, "c1")]),
            agui_service,
            agui_event_service,
        )
        result = await hub.process_request(
            request="go",
            initial_messages=[{"role": "user", "content": "go"}],
            local_context=ctx,
            model="gpt-4o",
        )
        pending = result["pending_tool"]
        pid = pending["tool_call_id"]
        assert pending["interrupt_id"] == pid
        rebuilt = build_interrupts_from_pending([pending])[0]
        assert rebuilt.id == pid
        assert rebuilt.id != pending["original_tool_call_id"]

    asyncio.run(run())
