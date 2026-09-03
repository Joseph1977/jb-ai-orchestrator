# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for run_agui_session interrupt/resume controller path."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from ag_ui.core.types import ResumeEntry, UserMessage

from app.config import Config
from app.controllers.ag_ui_controller import AGUIRunRequest, run_agui_session
from app.models.execution_models import LLMStateStatus
from app.services.agui_event_service import agui_event_service
from app.services.agui_service import agui_service
from app.services.tool_hub import ToolExecutionHub
from app.services.session_close_service import RunCancelHandle


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
        if not self._responses:
            raise AssertionError("LLM called more times than scripted")
        return self._responses.pop(0)

    def find_tool_by_name(self, name):
        return None

    async def execute_mcp_tool(self, name, args):
        return {"result": "ok"}


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


async def _read_sse_events(response):
    chunks = []
    async for part in response.body_iterator:
        chunks.append(part if isinstance(part, bytes) else part.encode())
    text = b"".join(chunks).decode()
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


@pytest.fixture
def agui_db_mocks():
    """In-memory execution/state store for controller resume tests."""
    executions: dict = {}
    states: dict = {}

    async def create_execution(session, **kwargs):
        eid = uuid.uuid4()
        executions[eid] = SimpleNamespace(
            id=eid,
            workspace_path=kwargs.get("workspace_path"),
            config=kwargs.get("config") or {},
            status=kwargs.get("status"),
            source=kwargs.get("source"),
        )
        return executions[eid]

    async def create_state(session, *, execution_id, payload, **kwargs):
        sid = uuid.uuid4()
        state = SimpleNamespace(
            id=sid,
            execution_id=execution_id,
            state_payload=payload,
            status=kwargs.get("status"),
            thread_id=kwargs.get("thread_id"),
            run_id=kwargs.get("run_id"),
            tool_call_id=kwargs.get("tool_call_id"),
            updated_at=kwargs.get("updated_at", datetime.now(timezone.utc)),
        )
        states[sid] = state
        return state

    async def update_execution(session, execution_id, **kwargs):
        ex = executions.get(execution_id)
        if ex:
            for k, v in kwargs.items():
                setattr(ex, k, v)

    async def get_execution(session, execution_id):
        return executions.get(execution_id)

    async def get_awaiting_state_by_tool_call_id(session, tool_call_id, *, thread_id=None):
        return await get_resume_hold_by_tool_call_id(
            session, tool_call_id, thread_id=thread_id, awaiting_only=True
        )

    async def get_resume_hold_by_tool_call_id(
        session, tool_call_id, *, thread_id=None, awaiting_only=False
    ):
        for state in reversed(list(states.values())):
            if awaiting_only and state.status != LLMStateStatus.AWAITING_RESPONSE:
                continue
            if not awaiting_only and state.status not in (
                LLMStateStatus.AWAITING_RESPONSE,
                LLMStateStatus.PENDING,
            ):
                continue
            if thread_id and state.thread_id != thread_id:
                continue
            if state.tool_call_id == tool_call_id:
                return state
            pending = (state.state_payload or {}).get("pending_tools") or []
            if not pending and (state.state_payload or {}).get("pending_tool"):
                pending = [(state.state_payload or {}).get("pending_tool")]
            if any(pt.get("tool_call_id") == tool_call_id for pt in pending):
                return state
        return None

    async def get_latest_awaiting_state_for_thread(session, thread_id):
        matches = [
            s for s in states.values()
            if s.thread_id == thread_id and s.status == LLMStateStatus.AWAITING_RESPONSE
        ]
        return matches[-1] if matches else None

    async def get_latest_pending_hold_for_thread(session, thread_id):
        matches = [
            s for s in states.values()
            if s.thread_id == thread_id and s.status == LLMStateStatus.PENDING
        ]
        return matches[-1] if matches else None

    async def mark_state_status(session, state_id, status):
        if state_id in states:
            states[state_id].status = status

    async def try_claim_state_for_resume(session, state_id):
        state = states.get(state_id)
        if state and state.status == LLMStateStatus.AWAITING_RESPONSE:
            state.status = LLMStateStatus.PENDING
            state.updated_at = datetime.now(timezone.utc)
            return True
        return False

    async def complete_claimed_state(session, state_id):
        state = states.get(state_id)
        if state and state.status == LLMStateStatus.PENDING:
            state.status = LLMStateStatus.COMPLETED
            state.updated_at = datetime.now(timezone.utc)
            return True
        return False

    async def restore_claimed_state(session, state_id):
        state = states.get(state_id)
        if state and state.status == LLMStateStatus.PENDING:
            state.status = LLMStateStatus.AWAITING_RESPONSE
            state.updated_at = datetime.now(timezone.utc)
            return True
        return False

    async def recover_stale_pending_claims(
        session, *, thread_id=None, execution_id=None, timeout_sec=None
    ):
        from app.config import Config

        timeout = timeout_sec if timeout_sec is not None else Config.RESUME_CLAIM_TIMEOUT_SEC
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout)
        count = 0
        for state in states.values():
            if state.status != LLMStateStatus.PENDING:
                continue
            if thread_id and state.thread_id != thread_id:
                continue
            if execution_id and state.execution_id != execution_id:
                continue
            if state.updated_at < cutoff:
                state.status = LLMStateStatus.AWAITING_RESPONSE
                state.updated_at = datetime.now(timezone.utc)
                count += 1
        return count

    async def rollback_partial_resume_settlement(
        session, *, claimed_state_id, new_state_id
    ):
        if new_state_id in states:
            states[new_state_id].status = LLMStateStatus.DISCARDED
            states[new_state_id].updated_at = datetime.now(timezone.utc)
        return await restore_claimed_state(session, claimed_state_id)

    async def discard_awaiting_states_for_thread(session, thread_id):
        count = 0
        for state in states.values():
            if state.thread_id == thread_id and state.status == LLMStateStatus.AWAITING_RESPONSE:
                state.status = LLMStateStatus.DISCARDED
                count += 1
        return count

    async def discard_stale_awaiting_and_cleanup_orphans(thread_id):
        return await discard_awaiting_states_for_thread(None, thread_id)

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    @asynccontextmanager
    async def noop_manage_run(**_kwargs):
        yield RunCancelHandle()

    async def ensure_thread_session(session, thread_id):
        return SimpleNamespace(thread_key=f"key-{thread_id}", thread_id=thread_id)

    async def try_create_active_run(session, **kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    patches = [
        patch("app.controllers.ag_ui_controller.get_session", fake_get_session),
        patch(
            "app.controllers.ag_ui_controller.run_lifecycle_service.ensure_thread_session",
            ensure_thread_session,
        ),
        patch(
            "app.controllers.ag_ui_controller.run_lifecycle_service.try_create_active_run",
            try_create_active_run,
        ),
        patch(
            "app.controllers.ag_ui_controller.run_lifecycle_service.reject_if_close_requested",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.controllers.ag_ui_controller.run_lifecycle_service.get_thread_session",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.controllers.ag_ui_controller.run_lifecycle_service.associate_execution_origin",
            AsyncMock(),
        ),
        patch(
            "app.controllers.ag_ui_controller.session_close_service.manage_run",
            noop_manage_run,
        ),
        patch(
            "app.controllers.ag_ui_controller._finalize_run_segment",
            AsyncMock(),
        ),
        patch(
            "app.controllers.ag_ui_controller._discard_stale_awaiting_and_cleanup_orphans",
            discard_stale_awaiting_and_cleanup_orphans,
        ),
        patch(
            "app.controllers.ag_ui_controller._ensure_workspace_for_agui_segment",
            AsyncMock(side_effect=lambda _eid, execution, _cfg, **_: execution.workspace_path or "/tmp/ws"),
        ),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.create_execution",
            create_execution,
        ),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.create_state",
            create_state,
        ),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.update_execution",
            update_execution,
        ),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.get_execution",
            get_execution,
        ),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.get_awaiting_state_by_tool_call_id",
            get_awaiting_state_by_tool_call_id,
        ),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.get_resume_hold_by_tool_call_id",
            get_resume_hold_by_tool_call_id,
        ),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.get_latest_awaiting_state_for_thread",
            get_latest_awaiting_state_for_thread,
        ),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.get_latest_pending_hold_for_thread",
            get_latest_pending_hold_for_thread,
        ),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.mark_state_status",
            mark_state_status,
        ),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.try_claim_state_for_resume",
            try_claim_state_for_resume,
        ),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.complete_claimed_state",
            complete_claimed_state,
        ),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.restore_claimed_state",
            restore_claimed_state,
        ),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.recover_stale_pending_claims",
            recover_stale_pending_claims,
        ),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.rollback_partial_resume_settlement",
            rollback_partial_resume_settlement,
        ),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.discard_awaiting_states_for_thread",
            discard_awaiting_states_for_thread,
        ),
    ]
    for p in patches:
        p.start()
    yield {"executions": executions, "states": states}
    for p in patches:
        p.stop()


FRONTEND_TOOLS = [
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


@pytest.mark.asyncio
async def test_run_agui_session_batch_interrupt_ids_match_pending(agui_db_mocks):
    names = [r.prefixed_name for r in agui_service.build_records(FRONTEND_TOOLS)]
    fake = FakeMCPService([
        _llm_tool_calls([
            (names[0], {}, "call_a"),
            (names[1], {}, "call_b"),
        ])
    ])
    hub = ToolExecutionHub(fake, agui_service, agui_event_service)

    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub):
        req = AGUIRunRequest(
            threadId="thread-ctrl",
            runId="run-1",
            messages=[UserMessage(id="u1", content="start")],
            frontendTools=FRONTEND_TOOLS,
            context=[],
            state={},
            forwardedProps={},
        )
        response = await run_agui_session(req)
        events = await _read_sse_events(response)

    finished = next(e for e in events if e.get("type") == "RUN_FINISHED")
    interrupts = finished["outcome"]["interrupts"]
    assert len(interrupts) == 2
    assert {i["id"] for i in interrupts} == {"call_a", "call_b"}
    assert all(i["id"] == i["toolCallId"] for i in interrupts)

    stored = list(agui_db_mocks["states"].values())[-1].state_payload
    pending = stored["pending_tools"]
    assert {p["tool_call_id"] for p in pending} == {"call_a", "call_b"}
    assert {p["interrupt_id"] for p in pending} == {"call_a", "call_b"}


@pytest.mark.asyncio
async def test_run_agui_session_copilotkit_resume_partial_then_final(agui_db_mocks):
    names = [r.prefixed_name for r in agui_service.build_records(FRONTEND_TOOLS)]
    fake1 = FakeMCPService([
        _llm_tool_calls([
            (names[0], {}, "call_a"),
            (names[1], {}, "call_b"),
        ])
    ])
    hub1 = ToolExecutionHub(fake1, agui_service, agui_event_service)

    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub1):
        req1 = AGUIRunRequest(
            threadId="thread-resume",
            runId="run-1",
            messages=[UserMessage(id="u1", content="go")],
            frontendTools=FRONTEND_TOOLS,
            context=[],
            state={},
            forwardedProps={},
        )
        response1 = await run_agui_session(req1)
        await _read_sse_events(response1)

    fake2 = FakeMCPService([])
    hub2 = ToolExecutionHub(fake2, agui_service, agui_event_service)
    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub2):
        req2 = AGUIRunRequest(
            threadId="thread-resume",
            runId="run-2",
            messages=[],
            frontendTools=FRONTEND_TOOLS,
            context=[],
            state={},
            forwardedProps={},
            resume=[
                ResumeEntry(
                    interruptId="call_a",
                    status="resolved",
                    payload={"result": {"answer": "a"}},
                )
            ],
        )
        response2 = await run_agui_session(req2)
        events2 = await _read_sse_events(response2)

    assert fake2.calls == []
    finished2 = next(e for e in events2 if e.get("type") == "RUN_FINISHED")
    assert finished2["outcome"]["type"] == "interrupt"
    assert len(finished2["outcome"]["interrupts"]) == 1
    assert finished2["outcome"]["interrupts"][0]["id"] == "call_b"

    fake3 = FakeMCPService([_llm_text("all done")])
    hub3 = ToolExecutionHub(fake3, agui_service, agui_event_service)
    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub3):
        req3 = AGUIRunRequest(
            threadId="thread-resume",
            runId="run-3",
            messages=[],
            frontendTools=FRONTEND_TOOLS,
            context=[],
            state={},
            forwardedProps={},
            resume=[
                ResumeEntry(
                    interruptId="call_b",
                    status="resolved",
                    payload={"result": {"answer": "b"}},
                )
            ],
        )
        response3 = await run_agui_session(req3)
        events3 = await _read_sse_events(response3)

    assert len(fake3.calls) == 1
    finished3 = next(e for e in events3 if e.get("type") == "RUN_FINISHED")
    assert finished3.get("outcome") is None or finished3.get("result", {}).get("toolCalls") is not None


@pytest.mark.asyncio
async def test_run_agui_session_frontend_resume_no_tool_call_result_echo(agui_db_mocks):
    names = [r.prefixed_name for r in agui_service.build_records(FRONTEND_TOOLS[:1])]
    fake1 = FakeMCPService([_llm_tool_calls([(names[0], {}, "call_x")])])
    hub1 = ToolExecutionHub(fake1, agui_service, agui_event_service)

    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub1):
        req1 = AGUIRunRequest(
            threadId="thread-no-echo",
            runId="run-1",
            messages=[UserMessage(id="u1", content="go")],
            frontendTools=FRONTEND_TOOLS[:1],
            context=[],
            state={},
            forwardedProps={},
        )
        response1 = await run_agui_session(req1)
        await _read_sse_events(response1)

    fake2 = FakeMCPService([_llm_text("done")])
    hub2 = ToolExecutionHub(fake2, agui_service, agui_event_service)
    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub2):
        req2 = AGUIRunRequest(
            threadId="thread-no-echo",
            runId="run-2",
            messages=[],
            frontendTools=FRONTEND_TOOLS[:1],
            context=[],
            state={},
            forwardedProps={},
            resume=[
                ResumeEntry(
                    interruptId="call_x",
                    status="resolved",
                    payload={"result": {"answer": "yes"}},
                )
            ],
        )
        response2 = await run_agui_session(req2)
        events2 = await _read_sse_events(response2)

    assert not any(e.get("type") == "TOOL_CALL_RESULT" for e in events2)
    assert len(fake2.calls) == 1
    tool_msgs = [
        m for m in fake2.calls[0]["messages"]
        if m.get("role") == "tool" and m.get("tool_call_id") == "call_x"
    ]
    assert len(tool_msgs) == 1
    assert json.loads(tool_msgs[0]["content"]) == {"answer": "yes"}


@pytest.mark.asyncio
async def test_run_agui_session_invalid_resume_emits_error_not_fresh_run(agui_db_mocks):
    names = [r.prefixed_name for r in agui_service.build_records(FRONTEND_TOOLS[:1])]
    fake1 = FakeMCPService([_llm_tool_calls([(names[0], {}, "call_x")])])
    hub1 = ToolExecutionHub(fake1, agui_service, agui_event_service)

    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub1):
        req1 = AGUIRunRequest(
            threadId="thread-invalid",
            runId="run-1",
            messages=[UserMessage(id="u1", content="go")],
            frontendTools=FRONTEND_TOOLS[:1],
            context=[],
            state={},
            forwardedProps={},
        )
        response1 = await run_agui_session(req1)
        await _read_sse_events(response1)

    fake2 = FakeMCPService([_llm_text("should not run")])
    hub2 = ToolExecutionHub(fake2, agui_service, agui_event_service)
    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub2):
        req2 = AGUIRunRequest(
            threadId="thread-invalid",
            runId="run-2",
            messages=[UserMessage(id="u2", content="new message")],
            frontendTools=FRONTEND_TOOLS[:1],
            context=[],
            state={},
            forwardedProps={},
            resume=[
                ResumeEntry(
                    interruptId="unknown-interrupt",
                    status="resolved",
                    payload={"result": {"answer": "nope"}},
                )
            ],
        )
        response = await run_agui_session(req2)
        events = await _read_sse_events(response)

    assert fake2.calls == []
    assert any(e.get("type") == "RUN_ERROR" for e in events)
    assert not any(e.get("type") == "RUN_STARTED" for e in events)


@pytest.mark.asyncio
async def test_run_agui_session_dual_resume_single_request(agui_db_mocks):
    names = [r.prefixed_name for r in agui_service.build_records(FRONTEND_TOOLS)]
    fake1 = FakeMCPService([
        _llm_tool_calls([
            (names[0], {}, "call_a"),
            (names[1], {}, "call_b"),
        ])
    ])
    hub1 = ToolExecutionHub(fake1, agui_service, agui_event_service)

    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub1):
        req1 = AGUIRunRequest(
            threadId="thread-dual",
            runId="run-1",
            messages=[UserMessage(id="u1", content="go")],
            frontendTools=FRONTEND_TOOLS,
            context=[],
            state={},
            forwardedProps={},
        )
        response1 = await run_agui_session(req1)
        await _read_sse_events(response1)

    old_state = list(agui_db_mocks["states"].values())[0]

    fake2 = FakeMCPService([_llm_text("both answered")])
    hub2 = ToolExecutionHub(fake2, agui_service, agui_event_service)
    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub2):
        req2 = AGUIRunRequest(
            threadId="thread-dual",
            runId="run-2",
            messages=[],
            frontendTools=FRONTEND_TOOLS,
            context=[],
            state={},
            forwardedProps={},
            resume=[
                ResumeEntry(
                    interruptId="call_a",
                    status="resolved",
                    payload={"result": {"answer": "a"}},
                ),
                ResumeEntry(
                    interruptId="call_b",
                    status="resolved",
                    payload={"result": {"answer": "b"}},
                ),
            ],
        )
        response2 = await run_agui_session(req2)
        events2 = await _read_sse_events(response2)

    assert len(fake2.calls) == 1
    tool_msgs = [
        m for m in fake2.calls[0]["messages"]
        if m.get("role") == "tool"
    ]
    assert {m["tool_call_id"] for m in tool_msgs} == {"call_a", "call_b"}
    finished = next(e for e in events2 if e.get("type") == "RUN_FINISHED")
    assert finished.get("outcome") is None or finished.get("result")
    assert old_state.status == LLMStateStatus.COMPLETED


@pytest.mark.asyncio
async def test_agui_resume_rejects_output_retarget(monkeypatch, agui_db_mocks):
    monkeypatch.setattr(Config, "OUTPUT_BINDINGS_ENABLED", True)
    name = agui_service.build_records(FRONTEND_TOOLS[:1])[0].prefixed_name
    fake1 = FakeMCPService([_llm_tool_calls([(name, {}, "call-retarget")])])
    hub1 = ToolExecutionHub(fake1, agui_service, agui_event_service)

    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub1):
        response1 = await run_agui_session(
            AGUIRunRequest(
                threadId="thread-retarget",
                runId="run-1",
                messages=[UserMessage(id="u1", content="go")],
                frontendTools=FRONTEND_TOOLS[:1],
            )
        )
        await _read_sse_events(response1)

    execution = next(iter(agui_db_mocks["executions"].values()))
    execution.config["output"] = {
        "type": "shared_folder",
        "uri": "/durable/original",
        "relativePath": ".",
    }
    state = next(iter(agui_db_mocks["states"].values()))

    fake2 = FakeMCPService([_llm_text("must not run")])
    hub2 = ToolExecutionHub(fake2, agui_service, agui_event_service)
    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub2):
        response2 = await run_agui_session(
            AGUIRunRequest(
                threadId="thread-retarget",
                runId="run-2",
                frontendTools=FRONTEND_TOOLS[:1],
                output={
                    "type": "shared_folder",
                    "uri": "/durable/replacement",
                },
                resume=[
                    ResumeEntry(
                        interruptId="call-retarget",
                        status="resolved",
                        payload={"result": {"answer": "yes"}},
                    )
                ],
            )
        )
        events = await _read_sse_events(response2)

    assert fake2.calls == []
    assert state.status == LLMStateStatus.AWAITING_RESPONSE
    assert any(
        event.get("type") == "RUN_ERROR"
        and "OUTPUT_BINDING_REPLACEMENT_NOT_ALLOWED" in event.get("message", "")
        for event in events
    )


@pytest.mark.asyncio
async def test_run_agui_session_cancelled_resume_content(agui_db_mocks):
    names = [r.prefixed_name for r in agui_service.build_records(FRONTEND_TOOLS[:1])]
    fake1 = FakeMCPService([_llm_tool_calls([(names[0], {}, "call_x")])])
    hub1 = ToolExecutionHub(fake1, agui_service, agui_event_service)

    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub1):
        req1 = AGUIRunRequest(
            threadId="thread-cancel",
            runId="run-1",
            messages=[UserMessage(id="u1", content="go")],
            frontendTools=FRONTEND_TOOLS[:1],
            context=[],
            state={},
            forwardedProps={},
        )
        response1 = await run_agui_session(req1)
        await _read_sse_events(response1)

    fake2 = FakeMCPService([_llm_text("ack cancel")])
    hub2 = ToolExecutionHub(fake2, agui_service, agui_event_service)
    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub2):
        req2 = AGUIRunRequest(
            threadId="thread-cancel",
            runId="run-2",
            messages=[],
            frontendTools=FRONTEND_TOOLS[:1],
            context=[],
            state={},
            forwardedProps={},
            resume=[
                ResumeEntry(
                    interruptId="call_x",
                    status="cancelled",
                    payload="user dismissed",
                )
            ],
        )
        response2 = await run_agui_session(req2)
        events2 = await _read_sse_events(response2)

    result_events = [e for e in events2 if e.get("type") == "TOOL_CALL_RESULT"]
    assert len(result_events) == 0
    tool_msg = next(
        m for m in fake2.calls[0]["messages"]
        if m.get("role") == "tool" and m.get("tool_call_id") == "call_x"
    )
    assert json.loads(tool_msg["content"]) == {"error": "user dismissed", "cancelled": True}


@pytest.mark.asyncio
async def test_run_agui_session_partial_resume_completes_old_state(agui_db_mocks):
    names = [r.prefixed_name for r in agui_service.build_records(FRONTEND_TOOLS)]
    fake1 = FakeMCPService([
        _llm_tool_calls([
            (names[0], {}, "call_a"),
            (names[1], {}, "call_b"),
        ])
    ])
    hub1 = ToolExecutionHub(fake1, agui_service, agui_event_service)

    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub1):
        req1 = AGUIRunRequest(
            threadId="thread-partial",
            runId="run-1",
            messages=[UserMessage(id="u1", content="go")],
            frontendTools=FRONTEND_TOOLS,
            context=[],
            state={},
            forwardedProps={},
        )
        response1 = await run_agui_session(req1)
        await _read_sse_events(response1)

    old_state = list(agui_db_mocks["states"].values())[0]
    old_state_id = old_state.id

    fake2 = FakeMCPService([])
    hub2 = ToolExecutionHub(fake2, agui_service, agui_event_service)
    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub2):
        req2 = AGUIRunRequest(
            threadId="thread-partial",
            runId="run-2",
            messages=[],
            frontendTools=FRONTEND_TOOLS,
            context=[],
            state={},
            forwardedProps={},
            resume=[
                ResumeEntry(
                    interruptId="call_a",
                    status="resolved",
                    payload={"result": {"answer": "a"}},
                )
            ],
        )
        response2 = await run_agui_session(req2)
        await _read_sse_events(response2)

    assert agui_db_mocks["states"][old_state_id].status == LLMStateStatus.COMPLETED
    new_states = [
        s for s in agui_db_mocks["states"].values()
        if s.id != old_state_id and s.status == LLMStateStatus.AWAITING_RESPONSE
    ]
    assert len(new_states) == 1
    pending = new_states[0].state_payload["pending_tools"]
    assert len(pending) == 1
    assert pending[0]["tool_call_id"] == "call_b"


@pytest.mark.asyncio
async def test_run_agui_session_hub_exception_restores_awaiting(agui_db_mocks):
    names = [r.prefixed_name for r in agui_service.build_records(FRONTEND_TOOLS[:1])]
    fake1 = FakeMCPService([_llm_tool_calls([(names[0], {}, "call_x")])])
    hub1 = ToolExecutionHub(fake1, agui_service, agui_event_service)

    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub1):
        req1 = AGUIRunRequest(
            threadId="thread-fail",
            runId="run-1",
            messages=[UserMessage(id="u1", content="go")],
            frontendTools=FRONTEND_TOOLS[:1],
            context=[],
            state={},
            forwardedProps={},
        )
        response1 = await run_agui_session(req1)
        await _read_sse_events(response1)

    state = list(agui_db_mocks["states"].values())[0]
    state_id = state.id

    failing_hub = AsyncMock()
    failing_hub.process_request = AsyncMock(side_effect=RuntimeError("hub exploded"))

    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=failing_hub):
        req2 = AGUIRunRequest(
            threadId="thread-fail",
            runId="run-2",
            messages=[],
            frontendTools=FRONTEND_TOOLS[:1],
            context=[],
            state={},
            forwardedProps={},
            resume=[
                ResumeEntry(
                    interruptId="call_x",
                    status="resolved",
                    payload={"result": {"answer": "y"}},
                )
            ],
        )
        response2 = await run_agui_session(req2)
        events2 = await _read_sse_events(response2)

    assert any(e.get("type") == "RUN_ERROR" for e in events2)
    assert agui_db_mocks["states"][state_id].status == LLMStateStatus.AWAITING_RESPONSE


@pytest.mark.asyncio
async def test_run_agui_session_concurrent_claim_rejected(agui_db_mocks):
    names = [r.prefixed_name for r in agui_service.build_records(FRONTEND_TOOLS[:1])]
    fake1 = FakeMCPService([_llm_tool_calls([(names[0], {}, "call_x")])])
    hub1 = ToolExecutionHub(fake1, agui_service, agui_event_service)

    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub1):
        req1 = AGUIRunRequest(
            threadId="thread-dup",
            runId="run-1",
            messages=[UserMessage(id="u1", content="go")],
            frontendTools=FRONTEND_TOOLS[:1],
            context=[],
            state={},
            forwardedProps={},
        )
        response1 = await run_agui_session(req1)
        await _read_sse_events(response1)

    state = list(agui_db_mocks["states"].values())[0]
    state.status = LLMStateStatus.PENDING
    state.updated_at = datetime.now(timezone.utc)

    fake2 = FakeMCPService([_llm_text("should not run")])
    hub2 = ToolExecutionHub(fake2, agui_service, agui_event_service)
    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub2):
        req2 = AGUIRunRequest(
            threadId="thread-dup",
            runId="run-2",
            messages=[],
            frontendTools=FRONTEND_TOOLS[:1],
            context=[],
            state={},
            forwardedProps={},
            resume=[
                ResumeEntry(
                    interruptId="call_x",
                    status="resolved",
                    payload={"result": {"answer": "y"}},
                )
            ],
        )
        response2 = await run_agui_session(req2)
        events2 = await _read_sse_events(response2)

    assert fake2.calls == []
    assert any(
        e.get("type") == "RUN_ERROR" and "in progress" in e.get("message", "").lower()
        for e in events2
    )
