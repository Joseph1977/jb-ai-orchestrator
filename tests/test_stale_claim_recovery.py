# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Stale PENDING claim recovery and completion-failure cleanup."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from ag_ui.core.types import ResumeEntry, UserMessage
from fastapi import HTTPException

pytest_plugins = ["test_agui_controller_resume"]

from app.controllers.ag_ui_controller import AGUIRunRequest, run_agui_session
from app.controllers.orchestrator_controller import resume
from app.models.execution_models import ExecutionStatus, LLMStateStatus
from app.models.requests import OrchestratorResumeInput
from app.services.execution_state_service import ExecutionStateService


def test_recover_stale_pending_claims_service():
    async def run():
        svc = ExecutionStateService()
        updates = []

        class FakeResult:
            def __init__(self, count):
                self.rowcount = count

        class FakeSession:
            async def execute(self, stmt):
                updates.append(stmt)
                return FakeResult(2)

        count = await svc.recover_stale_pending_claims(
            FakeSession(), thread_id="thread-1", timeout_sec=300
        )
        assert count == 2
        assert updates

    import asyncio

    asyncio.run(run())


@pytest.mark.asyncio
async def test_fresh_claim_not_recovered_until_claim_ages(agui_db_mocks):
    """Stale recovery uses claim time: old AWAITING hold claimed now stays active."""
    from app.config import Config
    from app.controllers import ag_ui_controller

    states = agui_db_mocks["states"]
    sid = uuid.uuid4()
    hold_created = datetime.now(timezone.utc) - timedelta(days=7)
    states[sid] = SimpleNamespace(
        id=sid,
        execution_id=uuid.uuid4(),
        state_payload={"pending_tools": [{"tool_call_id": "call_x"}]},
        status=LLMStateStatus.AWAITING_RESPONSE,
        thread_id="thread-claim-age",
        run_id="run-0",
        tool_call_id="call_x",
        updated_at=hold_created,
    )

    svc = ag_ui_controller.execution_state_service
    assert await svc.try_claim_state_for_resume(None, sid) is True
    assert states[sid].status == LLMStateStatus.PENDING
    assert states[sid].updated_at > hold_created

    assert (
        await svc.recover_stale_pending_claims(
            None, thread_id="thread-claim-age", timeout_sec=300
        )
        == 0
    )
    assert states[sid].status == LLMStateStatus.PENDING

    states[sid].updated_at = datetime.now(timezone.utc) - timedelta(
        seconds=Config.RESUME_CLAIM_TIMEOUT_SEC + 1
    )
    assert (
        await svc.recover_stale_pending_claims(
            None, thread_id="thread-claim-age", timeout_sec=300
        )
        == 1
    )
    assert states[sid].status == LLMStateStatus.AWAITING_RESPONSE


@pytest.mark.asyncio
async def test_stale_pending_recovered_on_resume(agui_db_mocks):
    from test_agui_controller_resume import (
        FRONTEND_TOOLS,
        FakeMCPService,
        ToolExecutionHub,
        _llm_text,
        _llm_tool_calls,
        _read_sse_events,
        agui_service,
        agui_event_service,
    )

    names = [r.prefixed_name for r in agui_service.build_records(FRONTEND_TOOLS[:1])]
    fake1 = FakeMCPService([_llm_tool_calls([(names[0], {}, "call_x")])])
    hub1 = ToolExecutionHub(fake1, agui_service, agui_event_service)

    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub1):
        req1 = AGUIRunRequest(
            threadId="thread-stale",
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
    state.updated_at = datetime.now(timezone.utc) - timedelta(seconds=400)

    fake2 = FakeMCPService([_llm_text("recovered")])
    hub2 = ToolExecutionHub(fake2, agui_service, agui_event_service)
    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub2):
        req2 = AGUIRunRequest(
            threadId="thread-stale",
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

    assert len(fake2.calls) == 1
    assert state.status == LLMStateStatus.COMPLETED
    assert any(e.get("type") == "RUN_FINISHED" for e in events2)


@pytest.mark.asyncio
async def test_stale_pending_then_fresh_run_discards_awaiting(agui_db_mocks):
    from test_agui_controller_resume import (
        FRONTEND_TOOLS,
        FakeMCPService,
        ToolExecutionHub,
        _llm_text,
        _llm_tool_calls,
        _read_sse_events,
        agui_service,
        agui_event_service,
    )

    names = [r.prefixed_name for r in agui_service.build_records(FRONTEND_TOOLS[:1])]
    fake1 = FakeMCPService([_llm_tool_calls([(names[0], {}, "call_x")])])
    hub1 = ToolExecutionHub(fake1, agui_service, agui_event_service)

    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub1):
        req1 = AGUIRunRequest(
            threadId="thread-fresh-stale",
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
    state.updated_at = datetime.now(timezone.utc) - timedelta(seconds=400)

    fake2 = FakeMCPService([_llm_text("fresh after stale")])
    hub2 = ToolExecutionHub(fake2, agui_service, agui_event_service)
    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub2):
        req2 = AGUIRunRequest(
            threadId="thread-fresh-stale",
            runId="run-2",
            messages=[UserMessage(id="u2", content="new message")],
            frontendTools=FRONTEND_TOOLS[:1],
            context=[],
            state={},
            forwardedProps={},
        )
        response2 = await run_agui_session(req2)
        await _read_sse_events(response2)

    assert state.status == LLMStateStatus.DISCARDED
    assert len(fake2.calls) == 1


@pytest.mark.asyncio
async def test_active_pending_blocks_fresh_run(agui_db_mocks):
    from test_agui_controller_resume import (
        FRONTEND_TOOLS,
        FakeMCPService,
        ToolExecutionHub,
        _llm_tool_calls,
        _read_sse_events,
        agui_service,
        agui_event_service,
    )

    names = [r.prefixed_name for r in agui_service.build_records(FRONTEND_TOOLS[:1])]
    fake1 = FakeMCPService([_llm_tool_calls([(names[0], {}, "call_x")])])
    hub1 = ToolExecutionHub(fake1, agui_service, agui_event_service)

    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub1):
        req1 = AGUIRunRequest(
            threadId="thread-block-fresh",
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

    fake2 = FakeMCPService([{"choices": [{"message": {"role": "assistant", "content": "nope"}}]}])
    hub2 = ToolExecutionHub(fake2, agui_service, agui_event_service)
    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub2):
        req2 = AGUIRunRequest(
            threadId="thread-block-fresh",
            runId="run-2",
            messages=[UserMessage(id="u2", content="new message")],
            frontendTools=FRONTEND_TOOLS[:1],
            context=[],
            state={},
            forwardedProps={},
        )
        response2 = await run_agui_session(req2)
        events2 = await _read_sse_events(response2)

    assert fake2.calls == []
    assert any(
        e.get("type") == "RUN_ERROR" and "fresh run" in e.get("message", "").lower()
        for e in events2
    )


@pytest.mark.asyncio
async def test_partial_completion_failure_rollback(agui_db_mocks):
    from test_agui_controller_resume import (
        FRONTEND_TOOLS,
        FakeMCPService,
        ToolExecutionHub,
        _llm_tool_calls,
        _read_sse_events,
        agui_service,
        agui_event_service,
    )

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
            threadId="thread-fail-complete",
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

    async def fail_complete(session, state_id):
        return False

    with patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub2), \
         patch(
             "app.controllers.ag_ui_controller.execution_state_service.complete_claimed_state",
             fail_complete,
         ):
        req2 = AGUIRunRequest(
            threadId="thread-fail-complete",
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

    assert any(
        e.get("type") == "RUN_ERROR" and "settle" in e.get("message", "").lower()
        for e in events2
    )
    assert agui_db_mocks["states"][old_state_id].status == LLMStateStatus.AWAITING_RESPONSE
    orphan_states = [
        s for s in agui_db_mocks["states"].values()
        if s.id != old_state_id and s.status == LLMStateStatus.DISCARDED
    ]
    assert len(orphan_states) == 1


@pytest.mark.asyncio
async def test_orchestrator_stale_pending_recovered_before_resume():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    state_payload = {
        "request": "prompt",
        "model": "gpt-4o",
        "pending_tools": [{"tool_call_id": "call_a", "function_name": "AskA", "source": "AGUI"}],
        "messages": [],
    }
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        config={"mode": "workflow"},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )
    state = SimpleNamespace(
        id=state_id,
        execution_id=exec_id,
        state_payload=state_payload,
        status=LLMStateStatus.AWAITING_RESPONSE,
    )

    hub = AsyncMock()
    hub.process_request = AsyncMock(return_value={
        "success": True,
        "response": "done",
        "awaits_response": False,
        "tool_calls_made": 1,
        "total_tokens": 1,
        "prompt_tokens": 1,
        "completion_tokens": 0,
        "tool_calls_info": [],
        "agui_tool_calls": [],
    })

    recover = AsyncMock(return_value=1)
    fake_run = SimpleNamespace(id=uuid.uuid4())

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    @asynccontextmanager
    async def noop_manage_run(**_kwargs):
        yield AsyncMock()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.get_tool_hub", return_value=hub), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_state", AsyncMock(return_value=state)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.recover_stale_pending_claims", recover), \
         patch("app.controllers.orchestrator_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.complete_claimed_state", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=fake_run)), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", AsyncMock(return_value="/tmp/ws")), \
         patch("app.controllers.orchestrator_controller.session_close_service.manage_run", noop_manage_run), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", AsyncMock()), \
         patch("app.controllers.orchestrator_controller._restore_claimed_state_if_needed", AsyncMock()), \
         patch("app.controllers.orchestrator_controller._finalize", AsyncMock(return_value=ExecutionStatus.COMPLETED)):
        resp = await resume(OrchestratorResumeInput(
            orchestratorGuid=exec_id,
            stateGuid=state_id,
            toolCallId="call_a",
            result={"answer": "a"},
        ))

    recover.assert_awaited_once()
    hub.process_request.assert_awaited_once()
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_orchestrator_active_pending_rejected_after_recovery():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path=None,
        config={},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )
    state = SimpleNamespace(
        id=state_id,
        execution_id=exec_id,
        state_payload={"pending_tools": [{"tool_call_id": "call_a"}]},
        status=LLMStateStatus.PENDING,
        updated_at=datetime.now(timezone.utc),
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.get_tool_hub"), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_state", AsyncMock(return_value=state)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.recover_stale_pending_claims", AsyncMock(return_value=0)), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await resume(OrchestratorResumeInput(
                orchestratorGuid=exec_id,
                stateGuid=state_id,
                toolCallId="call_a",
                result={"answer": "a"},
            ))

    assert exc.value.status_code == 409
