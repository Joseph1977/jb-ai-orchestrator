# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Orchestrator resume against pending_tools batch."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.models.execution_models import ExecutionStatus, LLMStateStatus
from app.controllers.orchestrator_controller import resume
from app.models.requests import OrchestratorResumeInput
from app.services.session_close_service import RunCancelHandle


@pytest.mark.asyncio
async def test_orchestrator_resume_accepts_batch_pending_tool():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    state_payload = {
        "request": "prompt",
        "model": "gpt-4o",
        "pending_tools": [
            {"tool_call_id": "call_a", "function_name": "AskA", "source": "AGUI"},
            {"tool_call_id": "call_b", "function_name": "AskB", "source": "AGUI"},
        ],
        "pending_tool": {"tool_call_id": "call_a"},
        "messages": [],
        "litellm_tools": [],
        "agui_records": [],
    }
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        config={"inPlace": False},
        closed_at=None,
        close_requested_at=None,
    )
    state = SimpleNamespace(
        id=state_id,
        execution_id=exec_id,
        state_payload=state_payload,
        status=LLMStateStatus.AWAITING_RESPONSE,
        thread_id=None,
        run_id="run-batch",
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

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    fake_run = SimpleNamespace(id=uuid.uuid4())

    @asynccontextmanager
    async def noop_manage_run(**_kwargs):
        yield RunCancelHandle()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.get_tool_hub", return_value=hub), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_state", AsyncMock(return_value=state)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.recover_stale_pending_claims", AsyncMock(return_value=0)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.complete_claimed_state", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=fake_run)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", AsyncMock(return_value="/tmp/ws")), \
         patch("app.controllers.orchestrator_controller.session_close_service.manage_run", noop_manage_run), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", AsyncMock()), \
         patch("app.controllers.orchestrator_controller._finalize", AsyncMock(return_value=ExecutionStatus.COMPLETED)), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)):
        resp = await resume(OrchestratorResumeInput(
            orchestratorGuid=exec_id,
            stateGuid=state_id,
            toolCallId="call_b",
            result={"answer": "b"},
        ))

    hub.process_request.assert_awaited_once()
    kwargs = hub.process_request.await_args.kwargs
    assert kwargs["resume_tool_results"][0]["tool_call_id"] == "call_b"
