# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Endpoint behaviour when root instructions exceed the eager budget.

test_execute_root_budget covers the prompt builder in isolation. These call the
endpoint coroutines so the parts that only exist at that level are pinned too:
the status code and error code a caller receives, that the execution is
finalized as failed, that the run is closed rather than left active, and that
the workspace is cleaned up.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.controllers.orchestrator_controller import execute, initiate
from app.models.bindings import ROOT_INSTRUCTIONS_TOO_LARGE, TransientCredentials
from app.models.execution_models import ExecutionStatus
from app.models.requests import ExecuteOrchestratorInput, InitiateOrchestratorInput
from app.services.harness import HarnessManifest, RootInstructionError
from app.services.run_lifecycle import RUN_STATUS_COMPLETED, RUN_STATUS_FAILED

CONTROLLER = "app.controllers.orchestrator_controller"

TOO_LARGE = RootInstructionError(
    "Root instructions are 9000 characters, over the 8000 character eager budget"
)


def body(response):
    import json

    return json.loads(response.body)


@asynccontextmanager
async def fake_get_session():
    yield object()


# --- initiate -----------------------------------------------------------------


def initiate_patches(updates, cleanup, *, manifest_error=None):
    """Patch initiate's module-level collaborators; return the context managers."""
    execution = SimpleNamespace(id=uuid.uuid4())
    ws = SimpleNamespace(
        path="/sandbox", source_kind=SimpleNamespace(value="local_path"), notes=[]
    )
    manifest = HarnessManifest(orchestration_type="generic", detected=True)

    async def _update(session, execution_id, **kwargs):
        updates.append(kwargs)

    collect = (
        MagicMock(side_effect=manifest_error)
        if manifest_error
        else MagicMock(return_value=manifest)
    )
    return execution, [
        patch(f"{CONTROLLER}.get_session", fake_get_session),
        patch(
            f"{CONTROLLER}.execution_state_service.create_execution",
            AsyncMock(return_value=execution),
        ),
        patch(f"{CONTROLLER}.execution_state_service.update_execution", _update),
        patch(f"{CONTROLLER}.run_lifecycle_service.associate_execution_origin", AsyncMock()),
        patch(f"{CONTROLLER}.workspace_manager.provision", AsyncMock(return_value=ws)),
        patch(f"{CONTROLLER}.workspace_manager.cleanup", cleanup),
        patch(f"{CONTROLLER}.collect_manifest", collect),
        patch(f"{CONTROLLER}.ensure_runtime", return_value="/tmp/runtime"),
        patch(f"{CONTROLLER}.select_relative_workspace", return_value="/sandbox/ws"),
        patch(f"{CONTROLLER}.should_provision_in_place", return_value=False),
        patch(f"{CONTROLLER}.provision_source", return_value="local:/src"),
        patch(f"{CONTROLLER}.provision_branch", return_value=None),
        patch(f"{CONTROLLER}.assert_output_feature_enabled"),
    ]


async def run_initiate(updates, cleanup, *, manifest_error=None):
    execution, patches = initiate_patches(updates, cleanup, manifest_error=manifest_error)
    binding = MagicMock()
    binding.type.value = "shared_folder"
    binding.relative_path = "."
    patches.append(patch(f"{CONTROLLER}.resolve_initiate_input"))

    import contextlib

    with contextlib.ExitStack() as stack:
        entered = [stack.enter_context(p) for p in patches]
        entered[-1].return_value = (binding, MagicMock(value="workflow"), False)
        response = await initiate(
            InitiateOrchestratorInput(
                folder="/src",
                credentials=TransientCredentials(inputAccessToken="tok"),
            )
        )
    return execution, response


@pytest.mark.asyncio
async def test_initiate_reports_the_root_budget_error_code():
    updates: list[dict] = []
    cleanup = MagicMock()

    _execution, response = await run_initiate(updates, cleanup, manifest_error=TOO_LARGE)

    assert response.status_code == 400
    payload = body(response)
    assert payload["success"] is False
    assert payload["errorCode"] == ROOT_INSTRUCTIONS_TOO_LARGE
    assert "eager budget" in payload["error"]


@pytest.mark.asyncio
async def test_initiate_root_budget_failure_finalizes_and_cleans_up():
    updates: list[dict] = []
    cleanup = MagicMock()

    await run_initiate(updates, cleanup, manifest_error=TOO_LARGE)

    assert updates[-1]["status"] is ExecutionStatus.FAILED
    cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_initiate_other_failures_still_report_without_a_code():
    """The new branch must not shadow the generic handler."""
    updates: list[dict] = []
    cleanup = MagicMock()

    _execution, response = await run_initiate(
        updates, cleanup, manifest_error=RuntimeError("disk gone")
    )

    assert response.status_code == 400
    payload = body(response)
    assert payload["success"] is False
    assert payload.get("errorCode") is None
    assert updates[-1]["status"] is ExecutionStatus.FAILED
    cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_initiate_succeeds_when_root_instructions_fit():
    updates: list[dict] = []
    cleanup = MagicMock()

    _execution, response = await run_initiate(updates, cleanup)

    # The success path returns the response model itself, not a JSONResponse.
    assert response.success is True
    assert getattr(response, "errorCode", None) is None
    cleanup.assert_not_called()


# --- execute ------------------------------------------------------------------


async def run_execute(finalized, run_statuses, *, prompt_error=None):
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/sandbox/ws",
        config={"model": "gpt-4o"},
        orchestration_type="generic",
        closed_at=None,
        close_requested_at=None,
    )
    active_run = SimpleNamespace(id=uuid.uuid4())

    async def _finalize(execution_id, result):
        finalized.append(result)
        return ExecutionStatus.FAILED

    async def _complete(run_pk, execution_id, *, run_status):
        run_statuses.append(run_status)

    prompt = (
        MagicMock(side_effect=prompt_error)
        if prompt_error
        else MagicMock(return_value="system")
    )
    hub = AsyncMock()
    hub.process_request = AsyncMock(
        return_value={"success": True, "response": "ok", "toolCalls": []}
    )

    with patch(f"{CONTROLLER}.get_session", fake_get_session), \
         patch(f"{CONTROLLER}.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch(f"{CONTROLLER}.execution_state_service.update_execution", AsyncMock()), \
         patch(f"{CONTROLLER}._close_error_code", AsyncMock(return_value=None)), \
         patch(f"{CONTROLLER}._segment_config", return_value={"model": "gpt-4o"}), \
         patch(f"{CONTROLLER}.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=active_run)), \
         patch(f"{CONTROLLER}._ensure_workspace_for_segment", AsyncMock(return_value="/sandbox/ws")), \
         patch(f"{CONTROLLER}._build_execute_system_prompt", prompt), \
         patch(f"{CONTROLLER}._local_context", MagicMock(return_value=MagicMock())), \
         patch(f"{CONTROLLER}.get_tool_hub", return_value=hub), \
         patch(f"{CONTROLLER}._finalize", _finalize), \
         patch(f"{CONTROLLER}._complete_run_lifecycle", _complete):
        response = await execute(
            ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="do it")
        )
    return response


@pytest.mark.asyncio
async def test_execute_reports_the_root_budget_error_code():
    finalized: list[dict] = []
    run_statuses: list[str] = []

    response = await run_execute(finalized, run_statuses, prompt_error=TOO_LARGE)

    assert response.status_code == 400
    payload = body(response)
    assert payload["success"] is False
    assert payload["errorCode"] == ROOT_INSTRUCTIONS_TOO_LARGE


@pytest.mark.asyncio
async def test_execute_root_budget_failure_finalizes_the_execution():
    finalized: list[dict] = []
    run_statuses: list[str] = []

    await run_execute(finalized, run_statuses, prompt_error=TOO_LARGE)

    assert len(finalized) == 1
    assert finalized[0]["success"] is False
    assert "eager budget" in finalized[0]["error"]


@pytest.mark.asyncio
async def test_execute_root_budget_failure_closes_the_run():
    """The run must not be left active for a workspace that cannot be prompted."""
    finalized: list[dict] = []
    run_statuses: list[str] = []

    await run_execute(finalized, run_statuses, prompt_error=TOO_LARGE)

    assert run_statuses == [RUN_STATUS_FAILED]


@pytest.mark.asyncio
async def test_execute_succeeds_when_root_instructions_fit():
    finalized: list[dict] = []
    run_statuses: list[str] = []

    response = await run_execute(finalized, run_statuses)

    assert response.status_code == 200
    assert run_statuses == [RUN_STATUS_COMPLETED]
