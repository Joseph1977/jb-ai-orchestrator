"""Legacy agent resume override wiring tests."""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.controllers.agent_controller import resume_run
from app.models.execution_models import LLMStateStatus
from app.models.requests import ResumeRunInput


@pytest.mark.asyncio
async def test_legacy_resume_passes_request_model_and_zero_budget_to_hub():
    execution_id = uuid.uuid4()
    state_id = uuid.uuid4()
    execution = SimpleNamespace(id=execution_id)
    state = SimpleNamespace(
        execution_id=execution_id,
        status=LLMStateStatus.AWAITING_RESPONSE,
        state_payload={
            "request": "prompt",
            "model": "snapshot-model",
            "max_calls": 2,
            "pending_tool": {"tool_call_id": "call-a"},
            "messages": [],
        },
    )
    hub = AsyncMock()
    hub.process_request = AsyncMock(
        return_value={"success": True, "response": "done"}
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.agent_controller.get_session", fake_get_session), \
         patch("app.controllers.agent_controller.get_tool_hub", return_value=hub), \
         patch("app.controllers.agent_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.agent_controller.execution_state_service.get_state", AsyncMock(return_value=state)), \
         patch("app.controllers.agent_controller._mark_state_status", AsyncMock()), \
         patch("app.controllers.agent_controller._update_execution_record", AsyncMock()):
        await resume_run(
            ResumeRunInput(
                executionGuid=execution_id,
                stateGuid=state_id,
                toolCallId="call-a",
                result={"answer": "a"},
                model="request-model",
                max_tool_calls=0,
            )
        )

    call_kwargs = hub.process_request.await_args.kwargs
    assert call_kwargs["model"] == "request-model"
    assert call_kwargs["max_tool_calls"] == 0
