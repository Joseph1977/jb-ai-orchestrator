# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Orchestrator resume claim lifecycle."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.controllers.orchestrator_controller import resume
from app.models.execution_models import ExecutionStatus, LLMStateStatus
from app.models.requests import OrchestratorResumeInput


@pytest.mark.asyncio
async def test_orchestrator_resume_hub_exception_restores_awaiting():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    state_payload = {
        "request": "prompt",
        "model": "gpt-4o",
        "pending_tools": [{"tool_call_id": "call_a", "function_name": "AskA", "source": "AGUI"}],
        "messages": [],
        "litellm_tools": [],
        "agui_records": [],
    }
    execution = AsyncMock(id=exec_id, workspace_path=None, config={}, closed_at=None, close_requested_at=None)
    state = AsyncMock(
        id=state_id,
        execution_id=exec_id,
        state_payload=state_payload,
        status=LLMStateStatus.AWAITING_RESPONSE,
        thread_id=None,
        run_id="run-claim",
    )

    hub = AsyncMock()
    hub.process_request = AsyncMock(side_effect=RuntimeError("boom"))

    restore = AsyncMock(return_value=True)
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
         patch("app.controllers.orchestrator_controller.execution_state_service.recover_stale_pending_claims", AsyncMock(return_value=0)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.restore_claimed_state", restore), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=fake_run)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", AsyncMock(return_value=None)), \
         patch("app.controllers.orchestrator_controller.session_close_service.manage_run", noop_manage_run), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", AsyncMock()), \
         patch("app.controllers.orchestrator_controller._finalize", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await resume(OrchestratorResumeInput(
                orchestratorGuid=exec_id,
                stateGuid=state_id,
                toolCallId="call_a",
                result={"answer": "a"},
            ))

    assert exc.value.status_code == 500
    restore.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_resume_pending_state_rejected():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    execution = AsyncMock(id=exec_id, workspace_path=None, config={}, closed_at=None, close_requested_at=None)
    state = AsyncMock(
        id=state_id,
        execution_id=exec_id,
        state_payload={"pending_tools": [{"tool_call_id": "call_a"}]},
        status=LLMStateStatus.PENDING,
        thread_id=None,
        run_id=None,
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
