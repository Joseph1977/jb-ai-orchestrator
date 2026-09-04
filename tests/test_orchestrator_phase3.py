# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Phase 3 orchestrator controller wiring tests."""

from __future__ import annotations

import uuid
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app.controllers.orchestrator_controller import (
    _close_error_code,
    close_orchestrator,
    execute,
    get_orchestrator_status,
    initiate,
    resume,
    _finish_active_run,
)
from app.models.bindings import INPUT_WORKSPACE_MISSING, RUN_BINDING_AMBIGUOUS, SESSION_CLOSED, SESSION_CLOSING
from app.services.run_lifecycle import RUN_STATUS_FAILED, RunClaimError
from app.models.execution_models import ExecutionStatus, LLMStateStatus
from app.models.requests import (
    ExecuteOrchestratorInput,
    ExecuteRequestResponse,
    InitiateOrchestratorInput,
    OrchestratorResumeInput,
)
from app.services.binding_contract import BindingError
from app.services.binding_runtime import RUN_BINDING_END, RUN_BINDING_START
from app.services.session_close_service import CloseResult, RunCancelHandle
from app.services.storage import StorageError


def _fake_run(run_pk=None):
    return SimpleNamespace(id=run_pk or uuid.uuid4())


@pytest.mark.asyncio
async def test_status_replays_persisted_pending_interrupts():
    execution_id = uuid.uuid4()
    state_id = uuid.uuid4()
    execution = SimpleNamespace(
        status=ExecutionStatus.AWAITING_RESPONSE,
        result=None,
        error_message=None,
        close_requested_at=None,
        closed_at=None,
    )
    state = SimpleNamespace(
        id=state_id,
        status=LLMStateStatus.AWAITING_RESPONSE,
        state_payload={
            "pending_tools": [
                {
                    "tool_call_id": "call_choice",
                    "function_name": "Ask-Choice",
                    "arguments": {
                        "question": "Choose",
                        "options": ["A", "B"],
                    },
                    "source": "AGUI",
                }
            ]
        },
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch(
             "app.controllers.orchestrator_controller.execution_state_service.get_execution",
             AsyncMock(return_value=execution),
         ), \
         patch(
             "app.controllers.orchestrator_controller.execution_state_service.get_latest_state_for_execution",
             AsyncMock(return_value=state),
         ):
        response = await get_orchestrator_status(execution_id)

    assert response.stateGuid == state_id
    assert response.pendingToolCallIds == ["call_choice"]
    assert response.interrupts == [
        {
            "id": "call_choice",
            "reason": "tool_awaiting_response",
            "message": "Awaiting response for tool 'Ask-Choice'",
            "toolCallId": "call_choice",
            "metadata": {
                "source": "AGUI",
                "functionName": "Ask-Choice",
                "arguments": {
                    "question": "Choose",
                    "options": ["A", "B"],
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_status_omits_stale_interrupts_when_execution_is_not_awaiting():
    execution_id = uuid.uuid4()
    execution = SimpleNamespace(
        status=ExecutionStatus.COMPLETED,
        result={"response": "done"},
        error_message=None,
        close_requested_at=None,
        closed_at=None,
    )
    state = SimpleNamespace(
        id=uuid.uuid4(),
        status=LLMStateStatus.AWAITING_RESPONSE,
        state_payload={"pending_tools": [{"tool_call_id": "stale"}]},
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch(
             "app.controllers.orchestrator_controller.execution_state_service.get_execution",
             AsyncMock(return_value=execution),
         ), \
         patch(
             "app.controllers.orchestrator_controller.execution_state_service.get_latest_state_for_execution",
             AsyncMock(return_value=state),
         ):
        response = await get_orchestrator_status(execution_id)

    assert response.stateGuid is None
    assert response.interrupts is None
    assert response.pendingToolCallIds is None


@asynccontextmanager
async def _noop_manage_run(**_kwargs):
    yield RunCancelHandle()


@pytest.mark.asyncio
async def test_execute_builds_tagged_binding_block_once():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        orchestration_type="generic",
        config={"mode": "workflow", "systemContext": "Base"},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )
    hub = AsyncMock()
    hub.process_request = AsyncMock(return_value={
        "success": True,
        "response": "done",
        "tool_calls_made": 0,
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tool_calls_info": [],
        "agui_tool_calls": [],
    })

    captured_prompt = {}

    async def _capture(**kwargs):
        captured_prompt["value"] = kwargs.get("system_prompt")
        return {
            "success": True,
            "response": "done",
            "tool_calls_made": 0,
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "tool_calls_info": [],
            "agui_tool_calls": [],
        }

    hub.process_request = AsyncMock(side_effect=_capture)

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.get_tool_hub", return_value=hub), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", AsyncMock(return_value="/tmp/ws")), \
         patch("app.controllers.orchestrator_controller.session_close_service.manage_run", _noop_manage_run), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", AsyncMock()), \
         patch("app.controllers.orchestrator_controller._finalize", AsyncMock(return_value=ExecutionStatus.COMPLETED)), \
         patch("app.controllers.orchestrator_controller.collect_manifest") as collect_mock, \
         patch("app.controllers.orchestrator_controller.render_system_prompt", return_value="Harness catalog"):
        collect_mock.return_value = SimpleNamespace(
            orchestration_type="generic",
            eager_context="",
            detected=True,
            confidence=50,
            agents=[],
            skills=[],
            rules=[],
            commands=[],
            notes=[],
        )
        await execute(ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go"))

    prompt = captured_prompt["value"]
    assert prompt.count(RUN_BINDING_START) == 1
    assert prompt.count(RUN_BINDING_END) == 1
    assert "Harness catalog" in prompt
    collect_mock.assert_called_once()


@pytest.mark.asyncio
async def test_resume_does_not_collect_manifest():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    state_payload = {
        "request": "prompt",
        "model": "gpt-4o",
        "pending_tools": [{"tool_call_id": "call_a", "function_name": "AskA", "source": "AGUI"}],
        "messages": [{"role": "system", "content": "Persisted harness"}],
        "litellm_tools": [],
        "agui_records": [],
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
        thread_id="thread-from-db",
        run_id="run-from-db",
    )
    hub = AsyncMock()
    hub.process_request = AsyncMock(return_value={
        "success": True,
        "response": "done",
        "tool_calls_made": 0,
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tool_calls_info": [],
        "agui_tool_calls": [],
    })

    create_run = AsyncMock(return_value=_fake_run())

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.get_tool_hub", return_value=hub), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_state", AsyncMock(return_value=state)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.recover_stale_pending_claims", AsyncMock(return_value=0)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.complete_claimed_state", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", create_run), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", AsyncMock(return_value="/tmp/ws")), \
         patch("app.controllers.orchestrator_controller.session_close_service.manage_run", _noop_manage_run), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", AsyncMock()), \
         patch("app.controllers.orchestrator_controller._finalize", AsyncMock(return_value=ExecutionStatus.COMPLETED)), \
         patch("app.controllers.orchestrator_controller.collect_manifest") as collect_mock:
        await resume(OrchestratorResumeInput(
            orchestratorGuid=exec_id,
            stateGuid=state_id,
            toolCallId="call_a",
            result={"answer": "a"},
        ))

    collect_mock.assert_not_called()
    create_run.assert_awaited_once()
    assert create_run.await_args.kwargs["thread_id"] == "thread-from-db"
    assert create_run.await_args.kwargs["run_id"] == "run-from-db"
    resume_state = hub.process_request.await_args.kwargs["resume_state"]
    assert resume_state["messages"][0]["content"].count(RUN_BINDING_START) == 1


@pytest.mark.asyncio
async def test_execute_existing_workspace_without_input_token():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/existing",
        orchestration_type="generic",
        config={"mode": "workflow", "input": {"type": "shared_folder", "uri": "/src", "relativePath": "."}},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )
    ensure = AsyncMock(return_value="/tmp/existing")

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    hub = AsyncMock()
    hub.process_request = AsyncMock(return_value={
        "success": True,
        "response": "ok",
        "tool_calls_made": 0,
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tool_calls_info": [],
        "agui_tool_calls": [],
    })

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.get_tool_hub", return_value=hub), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", ensure), \
         patch("app.controllers.orchestrator_controller.session_close_service.manage_run", _noop_manage_run), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", AsyncMock()), \
         patch("app.controllers.orchestrator_controller._finalize", AsyncMock(return_value=ExecutionStatus.COMPLETED)), \
         patch("app.controllers.orchestrator_controller._build_execute_system_prompt", return_value="sys"):
        await execute(ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go"))

    ensure.assert_awaited_once()
    assert ensure.await_args.kwargs.get("input_access_token") is None


@pytest.mark.asyncio
async def test_execute_missing_workspace_reprovisions_and_updates_path():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/missing",
        orchestration_type="generic",
        config={"mode": "workflow"},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )
    update = AsyncMock()

    async def _ensure(*_args, **_kwargs):
        execution.workspace_path = "/tmp/rebuilt"
        return "/tmp/rebuilt"

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    hub = AsyncMock()
    hub.process_request = AsyncMock(return_value={
        "success": True,
        "response": "ok",
        "tool_calls_made": 0,
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tool_calls_info": [],
        "agui_tool_calls": [],
    })

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.get_tool_hub", return_value=hub), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", update), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", AsyncMock(side_effect=_ensure)), \
         patch("app.controllers.orchestrator_controller.session_close_service.manage_run", _noop_manage_run), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", AsyncMock()), \
         patch("app.controllers.orchestrator_controller._finalize", AsyncMock(return_value=ExecutionStatus.COMPLETED)), \
         patch("app.controllers.orchestrator_controller._build_execute_system_prompt", return_value="sys"):
        await execute(ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go"))

    assert execution.workspace_path == "/tmp/rebuilt"


@pytest.mark.asyncio
async def test_execute_inplace_missing_workspace_returns_stable_error():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/gone",
        orchestration_type="generic",
        config={"mode": "workflow", "inPlace": True},
        source="https://github.com/acme/playbook.git",
        closed_at=None,
        close_requested_at=None,
    )
    finalize_segment = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", finalize_segment), \
         patch(
             "app.controllers.orchestrator_controller._ensure_workspace_for_segment",
             AsyncMock(side_effect=BindingError(INPUT_WORKSPACE_MISSING, "caller-owned input workspace is missing")),
         ):
        resp = await execute(ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go"))

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 400
    body = resp.body.decode()
    assert INPUT_WORKSPACE_MISSING in body
    finalize_segment.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_rejects_concurrent_active_run():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        orchestration_type="generic",
        config={"mode": "workflow"},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=None)):
        resp = await execute(ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go"))

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_execute_rejects_closed_session():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        orchestration_type="generic",
        config={"mode": "workflow"},
        source=None,
        closed_at=datetime.now(timezone.utc),
        close_requested_at=datetime.now(timezone.utc),
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=True)):
        resp = await execute(ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go"))

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 409
    assert SESSION_CLOSED in resp.body.decode()


@pytest.mark.asyncio
async def test_close_error_code_distinguishes_closed_thread():
    session = object()
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        closed_at=None,
        close_requested_at=None,
    )
    thread = SimpleNamespace(
        closed_at=datetime.now(timezone.utc),
        close_requested_at=datetime.now(timezone.utc),
    )

    with patch(
        "app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested",
        AsyncMock(return_value=True),
    ), patch(
        "app.controllers.orchestrator_controller.run_lifecycle_service.get_thread_session",
        AsyncMock(return_value=thread),
    ):
        code = await _close_error_code(
            session,
            execution,
            thread_id="closed-thread",
        )

    assert code == SESSION_CLOSED


@pytest.mark.asyncio
async def test_close_error_code_preserves_closing_thread():
    session = object()
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        closed_at=None,
        close_requested_at=None,
    )
    thread = SimpleNamespace(
        closed_at=None,
        close_requested_at=datetime.now(timezone.utc),
    )

    with patch(
        "app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested",
        AsyncMock(return_value=True),
    ), patch(
        "app.controllers.orchestrator_controller.run_lifecycle_service.get_thread_session",
        AsyncMock(return_value=thread),
    ):
        code = await _close_error_code(
            session,
            execution,
            thread_id="closing-thread",
        )

    assert code == SESSION_CLOSING


@pytest.mark.asyncio
async def test_close_unknown_guid_returns_404():
    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await close_orchestrator(uuid.uuid4())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_close_maps_closed_and_closing_status_codes():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(id=exec_id)

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    closed = CloseResult(
        status="closed",
        already_closed=False,
        discarded_holds=0,
        runtime_deleted=True,
        workspace_deleted=True,
        workspace_deleted_count=1,
    )
    closing = CloseResult(
        status="closing",
        already_closed=False,
        discarded_holds=1,
        runtime_deleted=False,
        workspace_deleted=False,
        workspace_deleted_count=0,
    )

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.session_close_service.close_execution", AsyncMock(return_value=closed)):
        resp = await close_orchestrator(exec_id)
    assert resp.status_code == 200
    payload = resp.body.decode()
    assert "alreadyClosed" in payload
    assert "workspaceDeletedCount" in payload

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.session_close_service.close_execution", AsyncMock(return_value=closing)):
        resp = await close_orchestrator(exec_id)
    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_close_is_idempotent():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(id=exec_id)
    result = CloseResult(
        status="closed",
        already_closed=True,
        discarded_holds=0,
        runtime_deleted=False,
        workspace_deleted=False,
        workspace_deleted_count=0,
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.session_close_service.close_execution", AsyncMock(return_value=result)):
        resp = await close_orchestrator(exec_id)

    assert resp.status_code == 200
    assert "alreadyClosed" in resp.body.decode()


@pytest.mark.asyncio
async def test_initiate_persists_sanitized_config_without_credentials():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(id=exec_id)
    manifest = SimpleNamespace(
        orchestration_type="generic",
        eager_context="eager",
        detected=True,
        confidence=80,
        agents=[],
        skills=[],
        rules=[],
        commands=[],
        notes=[],
    )
    ws = SimpleNamespace(path="/sandbox", source_kind=SimpleNamespace(value="local_path"), notes=[])

    persisted: dict = {}

    async def _update_execution(session, execution_id, **kwargs):
        persisted.update(kwargs)

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.create_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.associate_execution_origin", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.workspace_manager.provision", AsyncMock(return_value=ws)), \
         patch("app.controllers.orchestrator_controller.collect_manifest", return_value=manifest), \
         patch("app.controllers.orchestrator_controller.ensure_runtime", return_value="/tmp/runtime"), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", _update_execution), \
         patch("app.controllers.orchestrator_controller.select_relative_workspace", return_value="/sandbox/ws"), \
         patch("app.controllers.orchestrator_controller.should_provision_in_place", return_value=False), \
         patch("app.controllers.orchestrator_controller.provision_source", return_value="local:/src"), \
         patch("app.controllers.orchestrator_controller.resolve_initiate_input") as resolve_mock, \
         patch("app.controllers.orchestrator_controller.assert_output_feature_enabled"):
        binding = MagicMock()
        binding.type.value = "shared_folder"
        binding.relative_path = "."
        resolve_mock.return_value = (binding, MagicMock(value="workflow"), False)
        from app.models.bindings import TransientCredentials

        await initiate(InitiateOrchestratorInput(
            folder="/src",
            credentials=TransientCredentials(
                inputAccessToken="secret-input",
                outputAccessToken="secret-output",
            ),
        ))

    config = persisted.get("config") or {}
    dumped = str(config)
    assert "secret-input" not in dumped
    assert "secret-output" not in dumped
    assert "credentials" not in dumped


@pytest.mark.asyncio
async def test_execute_uses_synthetic_config_for_legacy_rows():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        orchestration_type="generic",
        config={"mode": "workflow", "model": "gpt-4o"},
        source="https://github.com/acme/legacy.git",
        closed_at=None,
        close_requested_at=None,
    )
    synthetic = {
        "mode": "workflow",
        "model": "gpt-4o",
        "input": {"type": "git", "uri": "https://github.com/acme/legacy.git", "relativePath": "."},
    }
    ensure = AsyncMock(return_value="/tmp/ws")

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    hub = AsyncMock()
    hub.process_request = AsyncMock(return_value={
        "success": True,
        "response": "ok",
        "tool_calls_made": 0,
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tool_calls_info": [],
        "agui_tool_calls": [],
    })

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.get_tool_hub", return_value=hub), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller.synthetic_input_from_config", return_value=synthetic), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", ensure), \
         patch("app.controllers.orchestrator_controller._build_execute_system_prompt", return_value="sys"), \
         patch("app.controllers.orchestrator_controller.session_close_service.manage_run", _noop_manage_run), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", AsyncMock()), \
         patch("app.controllers.orchestrator_controller._finalize", AsyncMock(return_value=ExecutionStatus.COMPLETED)):
        await execute(ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go"))

    assert ensure.await_args.args[2] == synthetic


@pytest.mark.asyncio
async def test_resume_prep_binding_error_restores_claim_and_finishes_run():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    state_payload = {
        "request": "prompt",
        "model": "gpt-4o",
        "pending_tools": [{"tool_call_id": "call_a"}],
        "messages": [{"role": "system", "content": "Persisted"}],
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
        thread_id="t-db",
        run_id="r-db",
    )
    restore_claim = AsyncMock()
    complete = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_state", AsyncMock(return_value=state)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.recover_stale_pending_claims", AsyncMock(return_value=0)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch(
             "app.controllers.orchestrator_controller._ensure_workspace_for_segment",
             AsyncMock(side_effect=BindingError(INPUT_WORKSPACE_MISSING, "missing")),
         ), \
         patch("app.controllers.orchestrator_controller._restore_claimed_state_if_needed", restore_claim), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", complete):
        resp = await resume(OrchestratorResumeInput(
            orchestratorGuid=exec_id,
            stateGuid=state_id,
            toolCallId="call_a",
            result={"answer": "a"},
        ))

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 400
    restore_claim.assert_awaited_once()
    assert restore_claim.await_args.kwargs["restore"] is True
    complete.assert_awaited_once()
    assert complete.await_args.kwargs["run_status"] == RUN_STATUS_FAILED


@pytest.mark.asyncio
async def test_execute_refresh_binding_error_finishes_run():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        orchestration_type="generic",
        config={"mode": "workflow"},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )
    finalize_segment = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", AsyncMock(return_value="/tmp/ws")), \
         patch(
             "app.controllers.orchestrator_controller._build_execute_system_prompt",
             side_effect=BindingError(RUN_BINDING_AMBIGUOUS, "ambiguous markers"),
         ), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", finalize_segment):
        resp = await execute(ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go"))

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 400
    assert RUN_BINDING_AMBIGUOUS in resp.body.decode()
    finalize_segment.assert_awaited_once()
    assert finalize_segment.await_args.kwargs["run_status"] == RUN_STATUS_FAILED


@pytest.mark.asyncio
async def test_execute_success_completes_run_after_parent_finalization():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        orchestration_type="generic",
        config={"mode": "workflow"},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )
    call_order: list[str] = []

    async def track_finalize(*_args, **_kwargs):
        call_order.append("finalize")
        return ExecutionStatus.COMPLETED

    async def track_complete(*_args, **_kwargs):
        call_order.append("complete")

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    hub = AsyncMock()
    hub.process_request = AsyncMock(return_value={
        "success": True,
        "response": "done",
        "tool_calls_made": 0,
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tool_calls_info": [],
        "agui_tool_calls": [],
    })

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.get_tool_hub", return_value=hub), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", AsyncMock(return_value="/tmp/ws")), \
         patch("app.controllers.orchestrator_controller._build_execute_system_prompt", return_value="sys"), \
         patch("app.controllers.orchestrator_controller.session_close_service.manage_run", _noop_manage_run), \
         patch("app.controllers.orchestrator_controller._finalize", side_effect=track_finalize), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", side_effect=track_complete):
        await execute(ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go"))

    assert call_order == ["finalize", "complete"]


@pytest.mark.asyncio
async def test_resume_refresh_binding_error_restores_claim_and_finishes_run():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    state_payload = {
        "request": "prompt",
        "model": "gpt-4o",
        "pending_tools": [{"tool_call_id": "call_a"}],
        "messages": [{"role": "system", "content": "<!-- run-binding:start -->"}],
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
        thread_id="t-db",
        run_id="r-db",
    )
    restore_claim = AsyncMock()
    complete = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_state", AsyncMock(return_value=state)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.recover_stale_pending_claims", AsyncMock(return_value=0)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", AsyncMock(return_value="/tmp/ws")), \
         patch("app.controllers.orchestrator_controller._restore_claimed_state_if_needed", restore_claim), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", complete):
        resp = await resume(OrchestratorResumeInput(
            orchestratorGuid=exec_id,
            stateGuid=state_id,
            toolCallId="call_a",
            result={"answer": "a"},
        ))

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 400
    assert RUN_BINDING_AMBIGUOUS in resp.body.decode()
    restore_claim.assert_awaited_once()
    complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_awaiting_persists_before_run_completion():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        orchestration_type="generic",
        config={"mode": "workflow"},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )
    call_order: list[str] = []

    async def track_persist(*_args, **_kwargs):
        call_order.append("persist")
        return uuid.uuid4()

    async def track_complete(*_args, **_kwargs):
        call_order.append("complete")

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    hub = AsyncMock()
    hub.process_request = AsyncMock(return_value={
        "success": True,
        "awaits_response": True,
        "state": {"request": "x", "messages": []},
        "pending_tools": [{
            "tool_call_id": "call_choice_1",
            "function_name": "Ask-Choice",
            "arguments": {
                "question": "Choose a path",
                "options": ["Short", "Detailed"],
            },
            "source": "AGUI",
        }],
        "tool_calls_made": 0,
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tool_calls_info": [],
        "agui_tool_calls": [],
    })

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.get_tool_hub", return_value=hub), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", AsyncMock(return_value="/tmp/ws")), \
         patch("app.controllers.orchestrator_controller._build_execute_system_prompt", return_value="sys"), \
         patch("app.controllers.orchestrator_controller.session_close_service.manage_run", _noop_manage_run), \
         patch("app.controllers.orchestrator_controller._persist_awaiting", side_effect=track_persist), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", side_effect=track_complete):
        response = await execute(
            ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go")
        )

    assert call_order == ["persist", "complete"]
    body = json.loads(response.body)
    assert body["awaitsResponse"] is True
    assert body["pendingToolCallIds"] == ["call_choice_1"]
    interrupt = body["interrupts"][0]
    assert interrupt["id"] == "call_choice_1"
    assert interrupt["toolCallId"] == "call_choice_1"
    assert interrupt["reason"] == "tool_awaiting_response"
    assert all(value is not None for value in interrupt.values())
    assert interrupt["metadata"] == {
        "source": "AGUI",
        "functionName": "Ask-Choice",
        "arguments": {
            "question": "Choose a path",
            "options": ["Short", "Detailed"],
        },
    }
    documented = ExecuteRequestResponse.model_validate(body)
    assert documented.interrupts == body["interrupts"]


@pytest.mark.asyncio
async def test_execute_prep_storage_error_finishes_run():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        orchestration_type="generic",
        config={"mode": "workflow", "output": {"type": "azure_blob", "uri": "https://x.blob.core.windows.net/o"}},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )
    complete = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", AsyncMock(return_value="/tmp/ws")), \
         patch("app.controllers.orchestrator_controller._build_execute_system_prompt", return_value="sys"), \
         patch(
             "app.controllers.orchestrator_controller._local_context",
             side_effect=StorageError("OUTPUT_CREDENTIAL_REQUIRED", "output credential required"),
         ), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", complete):
        resp = await execute(ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go"))

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 400
    complete.assert_awaited_once()
    assert complete.await_args.kwargs["run_status"] == RUN_STATUS_FAILED


@pytest.mark.asyncio
async def test_execute_persist_await_failure_finishes_run():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        orchestration_type="generic",
        config={"mode": "workflow"},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )
    call_order: list[str] = []
    complete = AsyncMock(side_effect=lambda *_args, **_kwargs: call_order.append("complete"))
    restore_pending = AsyncMock(
        side_effect=lambda *_args, **_kwargs: call_order.append("restore_pending")
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    hub = AsyncMock()
    hub.process_request = AsyncMock(return_value={
        "success": True,
        "awaits_response": True,
        "state": {"request": "x", "messages": []},
        "pending_tools": [{"tool_call_id": "call_1"}],
        "tool_calls_made": 0,
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tool_calls_info": [],
        "agui_tool_calls": [],
    })

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.get_tool_hub", return_value=hub), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", AsyncMock(return_value="/tmp/ws")), \
         patch("app.controllers.orchestrator_controller._build_execute_system_prompt", return_value="sys"), \
         patch("app.controllers.orchestrator_controller.session_close_service.manage_run", _noop_manage_run), \
         patch("app.controllers.orchestrator_controller._persist_awaiting", AsyncMock(side_effect=RuntimeError("persist failed"))), \
         patch("app.controllers.orchestrator_controller._restore_execution_pending", restore_pending), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", complete):
        with pytest.raises(HTTPException):
            await execute(ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go"))
        call_order.append("response")

    complete.assert_awaited_once()
    assert complete.await_args.kwargs["run_status"] == RUN_STATUS_FAILED
    restore_pending.assert_awaited_once_with(
        exec_id,
        {
            "success": False,
            "error": "Failed to persist awaiting state: persist failed",
        },
    )
    assert call_order == ["restore_pending", "complete", "response"]


@pytest.mark.asyncio
async def test_finish_active_run_skips_execution_status_update():
    run_pk = uuid.uuid4()
    finish = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.finish_run", finish):
        await _finish_active_run(run_pk, status=RUN_STATUS_FAILED)

    finish.assert_awaited_once()
    assert finish.await_args.kwargs["update_execution_status"] is False


@pytest.mark.asyncio
async def test_resume_close_cancellation_does_not_restore_claim():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    state_payload = {
        "request": "prompt",
        "model": "gpt-4o",
        "pending_tools": [{"tool_call_id": "call_a"}],
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
        thread_id="t-db",
        run_id="r-db",
    )
    restore_claim = AsyncMock()
    complete = AsyncMock()

    hub = AsyncMock()
    hub.process_request = AsyncMock(side_effect=asyncio.CancelledError())

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.get_tool_hub", return_value=hub), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_state", AsyncMock(return_value=state)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.recover_stale_pending_claims", AsyncMock(return_value=0)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", AsyncMock(return_value="/tmp/ws")), \
         patch("app.controllers.orchestrator_controller.session_close_service.manage_run", _noop_manage_run), \
         patch("app.controllers.orchestrator_controller._restore_claimed_state_if_needed", restore_claim), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", complete):
        resp = await resume(OrchestratorResumeInput(
            orchestratorGuid=exec_id,
            stateGuid=state_id,
            toolCallId="call_a",
            result={"answer": "a"},
        ))

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 409
    restore_claim.assert_awaited_once()
    assert restore_claim.await_args.kwargs["restore"] is False
    complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_run_claim_error_returns_session_closed():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        orchestration_type="generic",
        config={"mode": "workflow"},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch(
             "app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run",
             AsyncMock(side_effect=RunClaimError(code=SESSION_CLOSED, message="Session is closed")),
         ):
        resp = await execute(ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go"))

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 409
    assert SESSION_CLOSED in resp.body.decode()


@pytest.mark.asyncio
async def test_execute_run_claim_error_returns_session_closing():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        orchestration_type="generic",
        config={"mode": "workflow"},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch(
             "app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run",
             AsyncMock(side_effect=RunClaimError(code=SESSION_CLOSING, message="Session close is in progress")),
         ):
        resp = await execute(ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go"))

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 409
    assert SESSION_CLOSING in resp.body.decode()


@pytest.mark.asyncio
async def test_resume_run_claim_error_restores_claim():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    state_payload = {
        "request": "prompt",
        "model": "gpt-4o",
        "pending_tools": [{"tool_call_id": "call_a"}],
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
        thread_id="t-db",
        run_id="r-db",
    )
    restore_state = AsyncMock(return_value=True)

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_state", AsyncMock(return_value=state)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.recover_stale_pending_claims", AsyncMock(return_value=0)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.restore_claimed_state", restore_state), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch(
             "app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run",
             AsyncMock(side_effect=RunClaimError(code=SESSION_CLOSED, message="Session is closed")),
         ):
        resp = await resume(OrchestratorResumeInput(
            orchestratorGuid=exec_id,
            stateGuid=state_id,
            toolCallId="call_a",
            result={"answer": "a"},
        ))

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 409
    assert SESSION_CLOSED in resp.body.decode()
    restore_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_initial_close_gate_uses_state_thread_id():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    state_payload = {
        "request": "prompt",
        "model": "gpt-4o",
        "pending_tools": [{"tool_call_id": "call_a"}],
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
        thread_id="persisted-thread",
        run_id="r-db",
    )

    async def close_code(session, execution, *, thread_id=None):
        if thread_id == "persisted-thread":
            return SESSION_CLOSING
        return None

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_state", AsyncMock(return_value=state)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.recover_stale_pending_claims", AsyncMock(return_value=0)), \
         patch("app.controllers.orchestrator_controller._close_error_code", AsyncMock(side_effect=close_code)):
        resp = await resume(OrchestratorResumeInput(
            orchestratorGuid=exec_id,
            stateGuid=state_id,
            toolCallId="call_a",
            result={"answer": "a"},
        ))

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 409
    assert SESSION_CLOSING in resp.body.decode()


@pytest.mark.asyncio
async def test_execute_prep_binding_error_restores_execution_pending():
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        orchestration_type="generic",
        config={"mode": "workflow"},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )
    finalize = AsyncMock(return_value=ExecutionStatus.FAILED)
    restore_pending = AsyncMock()
    complete = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", AsyncMock(return_value="/tmp/ws")), \
         patch(
             "app.controllers.orchestrator_controller._build_execute_system_prompt",
             side_effect=BindingError(RUN_BINDING_AMBIGUOUS, "ambiguous markers"),
         ), \
         patch("app.controllers.orchestrator_controller._finalize", finalize), \
         patch("app.controllers.orchestrator_controller._restore_execution_pending", restore_pending), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", complete):
        resp = await execute(ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go"))

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 400
    finalize.assert_not_awaited()
    restore_pending.assert_awaited_once_with(
        exec_id,
        {
            "success": False,
            "error": "ambiguous markers",
            "error_code": RUN_BINDING_AMBIGUOUS,
        },
    )
    complete.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code", ["UNAVAILABLE", "RUN_LIFECYCLE_FAILED", "MAX_TOOL_CALLS"]
)
async def test_execute_failure_returns_pending_after_claim_finalization(error_code):
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        orchestration_type="generic",
        config={"mode": "workflow"},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )
    failure = {
        "success": False,
        "error": "segment failed",
        "error_code": error_code,
    }
    call_order: list[str] = []
    restore_pending = AsyncMock(
        side_effect=lambda *_args, **_kwargs: call_order.append("restore")
    )
    complete_run = AsyncMock(
        side_effect=lambda *_args, **_kwargs: call_order.append("claim_finalized")
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller._managed_hub_process", AsyncMock(return_value=failure)), \
         patch("app.controllers.orchestrator_controller._restore_execution_pending", restore_pending), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", complete_run):
        response = await execute(
            ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="go")
        )
        call_order.append("response")

    body = json.loads(response.body)
    assert body["executionStatus"] == ExecutionStatus.PENDING
    assert body["errorCode"] == error_code
    restore_pending.assert_awaited_once_with(exec_id, failure)
    assert call_order == ["restore", "claim_finalized", "response"]


@pytest.mark.asyncio
async def test_resume_max_tool_calls_failure_restores_existing_await():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        orchestration_type="generic",
        config={"mode": "workflow"},
        source=None,
        closed_at=None,
        close_requested_at=None,
    )
    state = SimpleNamespace(
        id=state_id,
        execution_id=exec_id,
        state_payload={
            "request": "prompt",
            "model": "gpt-4o",
            "max_calls": 2,
            "pending_tools": [{"tool_call_id": "call_a"}],
            "messages": [],
        },
        status=LLMStateStatus.AWAITING_RESPONSE,
        thread_id="t-db",
        run_id="r-db",
    )
    failure = {
        "success": False,
        "error": "Maximum tool calls (2) reached",
        "error_code": "MAX_TOOL_CALLS",
    }
    restore_claim = AsyncMock()
    restore_awaiting = AsyncMock()
    complete_run = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_state", AsyncMock(return_value=state)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.recover_stale_pending_claims", AsyncMock(return_value=0)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.orchestrator_controller._managed_hub_process", AsyncMock(return_value=failure)), \
         patch("app.controllers.orchestrator_controller._restore_claimed_state_if_needed", restore_claim), \
         patch("app.controllers.orchestrator_controller._restore_execution_awaiting", restore_awaiting), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", complete_run):
        response = await resume(
            OrchestratorResumeInput(
                orchestratorGuid=exec_id,
                stateGuid=state_id,
                toolCallId="call_a",
                result={"answer": "a"},
            )
        )

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["success"] is False
    assert body["executionStatus"] == ExecutionStatus.AWAITING_RESPONSE
    assert body["stateGuid"] == str(state_id)
    assert body["errorCode"] == "MAX_TOOL_CALLS"
    restore_claim.assert_awaited_once_with(state_id, restore=True)
    restore_awaiting.assert_awaited_once_with(exec_id, failure)
    complete_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_prep_binding_error_restores_execution_awaiting():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    state_payload = {
        "request": "prompt",
        "model": "gpt-4o",
        "pending_tools": [{"tool_call_id": "call_a"}],
        "messages": [{"role": "system", "content": "Persisted"}],
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
        thread_id="t-db",
        run_id="r-db",
    )
    restore_claim = AsyncMock()
    restore_awaiting = AsyncMock()
    finalize = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_state", AsyncMock(return_value=state)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.recover_stale_pending_claims", AsyncMock(return_value=0)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch(
             "app.controllers.orchestrator_controller._ensure_workspace_for_segment",
             AsyncMock(side_effect=BindingError(INPUT_WORKSPACE_MISSING, "missing")),
         ), \
         patch("app.controllers.orchestrator_controller._restore_claimed_state_if_needed", restore_claim), \
         patch("app.controllers.orchestrator_controller._restore_execution_awaiting", restore_awaiting), \
         patch("app.controllers.orchestrator_controller._finalize", finalize), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", AsyncMock()):
        resp = await resume(OrchestratorResumeInput(
            orchestratorGuid=exec_id,
            stateGuid=state_id,
            toolCallId="call_a",
            result={"answer": "a"},
        ))

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 400
    restore_claim.assert_awaited_once()
    restore_awaiting.assert_awaited_once_with(
        exec_id,
        {
            "success": False,
            "error": "missing",
            "error_code": INPUT_WORKSPACE_MISSING,
        },
    )
    finalize.assert_not_called()


@pytest.mark.asyncio
async def test_resume_persist_failure_restores_awaiting_with_diagnostics():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
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
        state_payload={
            "request": "prompt",
            "model": "gpt-4o",
            "pending_tools": [{"tool_call_id": "call_a"}],
            "messages": [],
        },
        status=LLMStateStatus.AWAITING_RESPONSE,
        thread_id="t-db",
        run_id="r-db",
    )
    restore_claim = AsyncMock()
    restore_awaiting = AsyncMock()
    complete_run = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_state", AsyncMock(return_value=state)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.recover_stale_pending_claims", AsyncMock(return_value=0)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=_fake_run())), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch(
             "app.controllers.orchestrator_controller._managed_hub_process",
             AsyncMock(
                 return_value={
                     "success": True,
                     "awaits_response": True,
                     "state": {"request": "prompt", "messages": []},
                     "pending_tools": [{"tool_call_id": "call_b"}],
                 }
             ),
         ), \
         patch(
             "app.controllers.orchestrator_controller._persist_awaiting",
             AsyncMock(side_effect=RuntimeError("persist failed")),
         ), \
         patch("app.controllers.orchestrator_controller._restore_claimed_state_if_needed", restore_claim), \
         patch("app.controllers.orchestrator_controller._restore_execution_awaiting", restore_awaiting), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", complete_run):
        with pytest.raises(HTTPException):
            await resume(
                OrchestratorResumeInput(
                    orchestratorGuid=exec_id,
                    stateGuid=state_id,
                    toolCallId="call_a",
                    result={"answer": "a"},
                )
            )

    restore_claim.assert_awaited_once_with(state_id, restore=True)
    restore_awaiting.assert_awaited_once_with(
        exec_id,
        {
            "success": False,
            "error": "Failed to persist awaiting state: persist failed",
        },
    )
    complete_run.assert_awaited_once()
