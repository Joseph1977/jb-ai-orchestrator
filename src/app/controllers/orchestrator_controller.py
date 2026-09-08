# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Orchestrator controller.

Lifecycle for executing a *folder* (a `.cursor` / `.claude` / generic playbook
bundle) with the harness owning all orchestration:

    POST /orchestrator/initiate   -> bind a folder to a session (no LLM call)
    POST /orchestrator/execute    -> run a prompt against the session
    POST /orchestrator/resume     -> continue a run awaiting user/tool input
    POST /orchestrator/{guid}/close -> close session lifecycle
    GET  /orchestrator/{guid}     -> session/run status

All await/resume flows are DB-backed, so any instance can resume (no sticky
sessions, no in-memory futures).
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from typing import Any, Awaitable, Callable, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.controllers.agent_controller import get_tool_hub
from app.db.session import get_session
from app.models.bindings import (
    SESSION_CLOSED,
    SESSION_CLOSING,
)
from app.models.execution_models import ExecutionStatus, LLMStateStatus
from app.models.requests import (
    ExecuteOrchestratorInput,
    ExecuteRequestResponse,
    ExecutionStatusResponse,
    InitiateOrchestratorInput,
    InitiateOrchestratorResponse,
    OrchestratorCloseResponse,
    OrchestratorResumeInput,
    PrimitiveSummary,
)
from app.services import workspace_manager
from app.services.binding_contract import (
    BindingError,
    assert_output_feature_enabled,
    provision_branch,
    provision_source,
    resolve_initiate_input,
    sanitize_execution_config,
    select_relative_workspace,
    should_provision_in_place,
    synthetic_input_from_config,
)
from app.services.binding_runtime import (
    ensure_input_workspace,
    refresh_segment_run_binding,
    segment_output_backend,
)
from app.services.agui_messages import build_authoritative_system_content
from app.services.execution_state_service import execution_state_service
from app.services.harness import (
    RootInstructionError,
    collect_manifest,
    render_system_prompt,
)
from app.services.local_tool_provider import LocalToolContext
from app.services.resume_options import (
    resolve_resume_max_tool_calls,
    resolve_resume_model,
)
from app.services.run_lifecycle import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RunClaimError,
    run_lifecycle_service,
    thread_key_for,
)
from app.services.runtime_paths import ensure_runtime
from app.services.session_close_service import (
    CANCEL_REASON_CLOSE,
    CANCEL_REASON_HEARTBEAT_FAILURE,
    CANCEL_REASON_SEGMENT_DEADLINE,
    RunCancelHandle,
    session_close_service,
)
from app.services.storage import StorageError
from app.services.tool_hub import AGUIRunContext
from app.utils.logger import logger

router = APIRouter(prefix="/orchestrator", tags=["Orchestrator"])

_RUN_CONFLICT = "RUN_CONFLICT"
_RUN_LIFECYCLE_FAILED = "RUN_LIFECYCLE_FAILED"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _primitives(refs) -> list[PrimitiveSummary]:
    return [
        PrimitiveSummary(name=r.name, path=r.path, description=r.description, kind=r.kind)
        for r in refs
    ]


def _input_access_token(credentials) -> Optional[str]:
    return credentials.input_access_token if credentials else None


def _output_access_token(credentials) -> Optional[str]:
    return credentials.output_access_token if credentials else None


def _session_closed_payload(*, error_code: str, message: str) -> dict[str, Any]:
    return {"success": False, "error": message, "errorCode": error_code}


def _run_conflict_payload() -> dict[str, Any]:
    return {
        "success": False,
        "error": "The previous workflow run is still stopping. Try again shortly.",
        "errorCode": _RUN_CONFLICT,
    }


async def _close_error_code(
    session,
    execution,
    *,
    thread_id: Optional[str] = None,
) -> Optional[str]:
    closed_at = getattr(execution, "closed_at", None)
    close_requested_at = getattr(execution, "close_requested_at", None)
    if closed_at is not None:
        return SESSION_CLOSED
    if close_requested_at is not None:
        return SESSION_CLOSING
    close_requested = await run_lifecycle_service.reject_if_close_requested(
        session,
        execution_id=execution.id,
        thread_id=thread_id,
    )
    if close_requested:
        if thread_id:
            thread = await run_lifecycle_service.get_thread_session(
                session,
                thread_key_for(thread_id),
            )
            if thread is not None and thread.closed_at is not None:
                return SESSION_CLOSED
        return SESSION_CLOSING
    return None


def _session_closed_response(error_code: str) -> JSONResponse:
    message = (
        "Session is closed"
        if error_code == SESSION_CLOSED
        else "Session close is in progress"
    )
    return JSONResponse(
        status_code=409,
        content=_session_closed_payload(error_code=error_code, message=message),
    )


def _frontend_tool_names(frontend_tools: Optional[list[dict]]) -> list[str]:
    names: list[str] = []
    for tool in frontend_tools or []:
        name = str((tool or {}).get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _build_execute_system_prompt(
    config: dict,
    *,
    workspace_path: str,
    orchestration_type: Optional[str],
    frontend_tools: Optional[list[dict]] = None,
) -> str:
    harness: Optional[str] = None
    try:
        manifest = collect_manifest(workspace_path, orchestration_type)
        harness = render_system_prompt(
            manifest,
            base=config.get("systemContext"),
        )
    except RootInstructionError:
        # Root instructions over the eager budget are a configuration error the
        # caller has to see. Falling back here would run the segment against
        # stale eager context, quietly dropping the instructions the workspace
        # author relies on -- the opposite of failing explicitly.
        raise
    except Exception:
        logger.warning(
            "Harness discovery failed for %s; falling back to stored context",
            workspace_path,
            exc_info=True,
        )
    if not harness:
        parts: list[str] = []
        if config.get("systemContext"):
            parts.append(str(config["systemContext"]))
        if config.get("eagerContext"):
            parts.append(str(config["eagerContext"]))
        harness = "\n\n".join(parts)
    refreshed = refresh_segment_run_binding(
        [{"role": "system", "content": harness or ""}],
        config,
    )
    content = refreshed[0].get("content")
    prompt = content if isinstance(content, str) else str(content or "")
    # The tool schemas alone leave the model free to answer a question in prose.
    # Name the registered interaction tools in the authoritative prompt, the
    # same way the AG-UI relay does, so both channels state the same contract.
    # Binding placeholders are resolved first: the catalog carries none.
    composed = build_authoritative_system_content(
        harness_prompt=prompt,
        has_frontend_tools=bool(frontend_tools),
        frontend_tool_names=_frontend_tool_names(frontend_tools),
    )
    return composed or prompt


def _refresh_resume_messages(
    state_payload: dict,
    config: dict,
) -> list[dict]:
    return refresh_segment_run_binding(
        state_payload.get("messages") or [],
        config,
    )


def _local_context(
    execution,
    config: Optional[dict] = None,
    *,
    output_access_token: Optional[str] = None,
    workspace_path: Optional[str] = None,
) -> LocalToolContext:
    if config is None:
        config = _segment_config(execution)
    runtime = config.get("runtimePath")
    if not runtime and execution.id:
        runtime = ensure_runtime(execution_id=execution.id)
    return LocalToolContext(
        workspace_path=workspace_path or execution.workspace_path,
        in_place=bool(config.get("inPlace", False)),
        runtime_path=runtime,
        mode=str(config.get("mode") or "working_copy"),
        output_backend=segment_output_backend(
            config,
            output_access_token=output_access_token,
        ),
    )


def _pending_interrupt_payload(state_payload: Optional[dict]) -> tuple[list[dict], list[str]]:
    from app.services.agui_interrupt import build_interrupts_from_pending

    pending_tools = (state_payload or {}).get("pending_tools") or []
    if not pending_tools and (state_payload or {}).get("pending_tool"):
        pending_tools = [(state_payload or {})["pending_tool"]]
    interrupts = build_interrupts_from_pending(pending_tools)
    return (
        [item.model_dump(by_alias=True, exclude_none=True) for item in interrupts],
        [
            str(item.get("tool_call_id"))
            for item in pending_tools
            if item.get("tool_call_id")
        ],
    )


def _run_response(execution_id: uuid.UUID, status: ExecutionStatus, result: dict, state_id=None) -> dict:
    payload = {
        "success": result.get("success", False),
        "response": result.get("response"),
        "error": result.get("error"),
        "tool_calls_made": result.get("tool_calls_made", 0),
        "total_tokens": result.get("total_tokens", 0),
        "promptTokenCount": result.get("prompt_tokens", 0),
        "candidatesTokenCount": result.get("completion_tokens", 0),
        "partial_response": result.get("partial_response"),
        "tool_calls_info": result.get("tool_calls_info") or [],
        "agui_tool_calls": result.get("agui_tool_calls") or [],
        "executionGuid": str(execution_id),
        "executionStatus": status.value,
    }
    error_code = result.get("error_code") or result.get("errorCode")
    if error_code:
        payload["errorCode"] = error_code
    if status == ExecutionStatus.AWAITING_RESPONSE:
        payload["awaitsResponse"] = True
        if state_id:
            payload["stateGuid"] = str(state_id)
        interrupts, pending_ids = _pending_interrupt_payload(result)
        if interrupts:
            payload["interrupts"] = interrupts
            payload["pendingToolCallIds"] = pending_ids
    return payload


async def _persist_awaiting(
    execution_id: uuid.UUID,
    result: dict,
    *,
    thread_id: Optional[str],
    run_id: Optional[str],
) -> uuid.UUID:
    state_payload = result.get("state")
    pending_tools = result.get("pending_tools") or []
    if not pending_tools and result.get("pending_tool"):
        pending_tools = [result["pending_tool"]]
    pending_tool = pending_tools[0] if pending_tools else {}
    if not state_payload:
        raise HTTPException(status_code=500, detail="Awaiting-response state missing payload")
    async with get_session() as session:
        state = await execution_state_service.create_state(
            session,
            execution_id=execution_id,
            payload=state_payload,
            status=LLMStateStatus.AWAITING_RESPONSE,
            thread_id=thread_id,
            run_id=run_id,
            tool_call_id=pending_tool.get("tool_call_id"),
        )
        await execution_state_service.update_execution(
            session, execution_id, status=ExecutionStatus.AWAITING_RESPONSE
        )
        return state.id


async def _finalize(execution_id: uuid.UUID, result: dict) -> ExecutionStatus:
    status = ExecutionStatus.COMPLETED if result.get("success") else ExecutionStatus.FAILED
    async with get_session() as session:
        await execution_state_service.update_execution(
            session,
            execution_id,
            status=status,
            result=result,
            error_message=None if result.get("success") else result.get("error"),
        )
    return status


async def _record_segment_failure(
    execution_id: uuid.UUID,
    *,
    status: ExecutionStatus,
    result: dict,
) -> None:
    """Record the failed attempt without disabling the persistent session."""
    async with get_session() as session:
        await execution_state_service.update_execution(
            session,
            execution_id,
            status=status,
            result=result,
            error_message=result.get("error") or "Workflow segment failed",
        )


async def _finish_active_run(
    run_pk: uuid.UUID,
    *,
    status: str,
) -> None:
    async with get_session() as session:
        await run_lifecycle_service.finish_run(
            session,
            run_pk,
            status=status,
            preserve_completed_execution=True,
            update_execution_status=False,
        )


async def _complete_run_lifecycle(
    run_pk: uuid.UUID,
    execution_id: uuid.UUID,
    *,
    run_status: str,
) -> None:
    await _finish_active_run(run_pk, status=run_status)
    await session_close_service.reconcile_execution_close(execution_id)


async def _restore_claimed_state_if_needed(
    state_id: Optional[uuid.UUID],
    *,
    restore: bool,
) -> None:
    if not restore or state_id is None:
        return
    async with get_session() as session:
        if not await execution_state_service.restore_claimed_state(session, state_id):
            logger.warning("Failed to restore claimed state %s during cleanup", state_id)


async def _ensure_workspace_for_segment(
    execution_id: uuid.UUID,
    execution,
    config: dict,
    *,
    input_access_token: Optional[str] = None,
) -> str:
    workspace_path = await ensure_input_workspace(
        execution_id,
        config,
        workspace_path=execution.workspace_path,
        input_access_token=input_access_token,
    )
    if workspace_path != (execution.workspace_path or ""):
        async with get_session() as session:
            await execution_state_service.update_execution(
                session,
                execution_id,
                workspace_path=workspace_path,
            )
        execution.workspace_path = workspace_path
    return workspace_path


async def _restore_execution_awaiting(
    execution_id: uuid.UUID,
    result: Optional[dict] = None,
) -> None:
    if result is None:
        async with get_session() as session:
            await execution_state_service.update_execution(
                session,
                execution_id,
                status=ExecutionStatus.AWAITING_RESPONSE,
            )
        return
    await _record_segment_failure(
        execution_id,
        status=ExecutionStatus.AWAITING_RESPONSE,
        result=result,
    )


async def _restore_execution_pending(execution_id: uuid.UUID, result: dict) -> None:
    await _record_segment_failure(
        execution_id,
        status=ExecutionStatus.PENDING,
        result=result,
    )


async def _managed_hub_process(
    *,
    run_pk: uuid.UUID,
    execution_id: uuid.UUID,
    thread_id: Optional[str],
    make_coro: Callable[[asyncio.Event], Awaitable[Any]],
) -> Any:
    ready = asyncio.Event()
    shared: dict[str, Any] = {}

    async def _hub_runner() -> Any:
        await ready.wait()
        return await make_coro(shared["cancel_event"])

    task = asyncio.create_task(_hub_runner())
    cancel_handle: Optional[RunCancelHandle] = None
    try:
        try:
            async with session_close_service.manage_run(
                run_pk=run_pk,
                execution_id=execution_id,
                task=task,
                thread_id=thread_id,
            ) as handle:
                cancel_handle = handle
                shared["cancel_event"] = cancel_handle.event
                ready.set()
                result = await task
                if cancel_handle.reason is not None:
                    raise asyncio.CancelledError
                return result
        finally:
            if not ready.is_set():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    except asyncio.CancelledError:
        if cancel_handle is None or cancel_handle.reason in (None, CANCEL_REASON_CLOSE):
            raise
        if cancel_handle.reason == CANCEL_REASON_HEARTBEAT_FAILURE:
            return {
                "success": False,
                "error": "Workflow run tracking failed",
                "error_code": _RUN_LIFECYCLE_FAILED,
            }
        if cancel_handle.reason != CANCEL_REASON_SEGMENT_DEADLINE:
            raise
        return {
            "success": False,
            "error": "Run timed out",
            "error_code": "TIMEOUT",
        }


def _segment_config(execution) -> dict:
    return synthetic_input_from_config(
        execution.config,
        getattr(execution, "source", None),
    )


def _binding_error_response(exc: BindingError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"success": False, "error": exc.message, "errorCode": exc.code},
    )


def _storage_error_response(exc: StorageError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"success": False, "error": exc.message, "errorCode": exc.code},
    )


def _root_instruction_error_response(exc: RootInstructionError) -> JSONResponse:
    """Surface an over-budget root playbook as a workspace configuration error."""
    return JSONResponse(
        status_code=400,
        content={
            "success": False,
            "error": str(exc),
            "errorCode": exc.code,
        },
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/initiate", response_model=InitiateOrchestratorResponse)
async def initiate(request: InitiateOrchestratorInput):
    """Bind a folder to a new orchestrator session. No LLM invocation."""
    try:
        assert_output_feature_enabled(request.output)
        input_binding, mode, legacy_writable = resolve_initiate_input(
            input_binding=request.input,
            folder=request.folder,
            in_place=bool(request.inPlace),
            mode=request.mode,
        )
    except BindingError as exc:
        return JSONResponse(
            status_code=400,
            content=InitiateOrchestratorResponse(
                success=False, error=exc.message, errorCode=exc.code
            ).model_dump(mode="json"),
        )

    source = provision_source(input_binding)
    async with get_session() as session:
        execution = await execution_state_service.create_execution(
            session, status=ExecutionStatus.PENDING, source=source
        )
        execution_id = execution.id
        await run_lifecycle_service.associate_execution_origin(
            session,
            execution_id,
            origin="orchestrator",
        )

    logger.info(
        "Initiating orchestrator %s type=%s mode=%s",
        execution_id,
        input_binding.type.value,
        mode.value,
    )

    try:
        ws = await workspace_manager.provision(
            execution_id,
            source,
            in_place=should_provision_in_place(
                input_binding, legacy_writable=legacy_writable
            ),
            branch=provision_branch(input_binding),
            input_access_token=_input_access_token(request.credentials),
        )
        workspace_path = select_relative_workspace(ws.path, input_binding.relative_path)
        manifest = collect_manifest(workspace_path, request.orchestrationType)
        runtime_path = ensure_runtime(execution_id=execution_id)

        config = sanitize_execution_config(
            input_binding=input_binding,
            mode=mode,
            output_binding=request.output,
            runtime_path=runtime_path,
            legacy_writable_in_place=legacy_writable,
            extra={
                "systemContext": request.systemContext,
                "eagerContext": manifest.eager_context,
                "model": request.model,
                "maxToolCalls": request.maxToolCalls,
                "sourceKind": ws.source_kind.value,
            },
        )
        async with get_session() as session:
            await execution_state_service.update_execution(
                session,
                execution_id,
                workspace_path=workspace_path,
                orchestration_type=manifest.orchestration_type,
                config=config,
            )
    except BindingError as exc:
        async with get_session() as session:
            await execution_state_service.update_execution(
                session, execution_id, status=ExecutionStatus.FAILED, error_message=exc.message
            )
        workspace_manager.cleanup(execution_id)
        return JSONResponse(
            status_code=400,
            content=InitiateOrchestratorResponse(
                success=False, orchestratorGuid=execution_id, error=exc.message, errorCode=exc.code
            ).model_dump(mode="json"),
        )
    except RootInstructionError as exc:
        # Same workspace configuration error execute reports, so it carries the
        # same code here rather than falling through to the generic handler and
        # arriving without one.
        logger.error("Root instructions exceed the eager budget: %s", exc)
        async with get_session() as session:
            await execution_state_service.update_execution(
                session, execution_id, status=ExecutionStatus.FAILED, error_message=str(exc)
            )
        workspace_manager.cleanup(execution_id)
        return JSONResponse(
            status_code=400,
            content=InitiateOrchestratorResponse(
                success=False,
                orchestratorGuid=execution_id,
                error=str(exc),
                errorCode=exc.code,
            ).model_dump(mode="json"),
        )
    except Exception as exc:
        logger.error("Failed to initiate orchestrator %s: %s", execution_id, exc)
        async with get_session() as session:
            await execution_state_service.update_execution(
                session, execution_id, status=ExecutionStatus.FAILED, error_message=str(exc)
            )
        workspace_manager.cleanup(execution_id)
        return JSONResponse(
            status_code=400,
            content=InitiateOrchestratorResponse(
                success=False, orchestratorGuid=execution_id, error=str(exc)
            ).model_dump(mode="json"),
        )

    return InitiateOrchestratorResponse(
        success=True,
        orchestratorGuid=execution_id,
        orchestrationType=manifest.orchestration_type,
        detected=manifest.detected,
        confidence=manifest.confidence,
        workspacePath=workspace_path,
        sourceKind=ws.source_kind.value,
        input=config.get("input"),
        output=config.get("output"),
        mode=config.get("mode"),
        agents=_primitives(manifest.agents),
        skills=_primitives(manifest.skills),
        rules=_primitives(manifest.rules),
        commands=_primitives(manifest.commands),
        notes=[*ws.notes, *manifest.notes],
    )


@router.post("/execute", response_model=ExecuteRequestResponse)
async def execute(request: ExecuteOrchestratorInput):
    """Run a prompt against an initiated orchestrator session."""
    execution_id = request.orchestratorGuid
    run_id = request.runId or str(uuid.uuid4())

    async with get_session() as session:
        execution = await execution_state_service.get_execution(session, execution_id)
        if not execution:
            raise HTTPException(status_code=404, detail="Orchestrator session not found")
        close_code = await _close_error_code(
            session, execution, thread_id=request.threadId
        )
        if close_code:
            return _session_closed_response(close_code)
        segment_config = _segment_config(execution)
        orchestration_type = execution.orchestration_type

    async with get_session() as session:
        execution = await execution_state_service.get_execution(session, execution_id)
        close_code = await _close_error_code(
            session, execution, thread_id=request.threadId
        )
        if close_code:
            return _session_closed_response(close_code)
        try:
            active_run = await run_lifecycle_service.try_create_active_run(
                session,
                execution_id=execution_id,
                run_id=run_id,
                thread_id=request.threadId,
                origin="orchestrator",
                channel="orchestrator",
            )
        except RunClaimError as exc:
            return _session_closed_response(exc.code)
        if active_run is None:
            return JSONResponse(status_code=409, content=_run_conflict_payload())
        run_pk = active_run.id
        await execution_state_service.update_execution(
            session, execution_id, status=ExecutionStatus.RUNNING
        )

    run_status = RUN_STATUS_FAILED
    output_token = _output_access_token(request.credentials)
    model = request.model or segment_config.get("model") or "gpt-3.5-turbo"
    max_calls = (
        request.maxToolCalls
        if request.maxToolCalls is not None
        else segment_config.get("maxToolCalls")
    )
    include_agui = bool(request.frontendTools)
    agui_context = (
        AGUIRunContext(thread_id=request.threadId, run_id=run_id)
        if request.threadId
        else None
    )

    async def _run_execute_segment(cancel_event: asyncio.Event) -> dict:
        workspace_path = await _ensure_workspace_for_segment(
            execution_id,
            execution,
            segment_config,
            input_access_token=_input_access_token(request.credentials),
        )
        system_prompt = _build_execute_system_prompt(
            segment_config,
            workspace_path=workspace_path,
            orchestration_type=orchestration_type,
            frontend_tools=request.frontendTools,
        )
        local_ctx = _local_context(
            execution,
            segment_config,
            output_access_token=output_token,
            workspace_path=workspace_path,
        )
        hub = get_tool_hub()
        return await hub.process_request(
            request=request.prompt,
            model=model,
            max_tool_calls=max_calls,
            requested_tools=request.tools,
            include_agui_tools=include_agui,
            agui_context=agui_context,
            local_context=local_ctx,
            system_prompt=system_prompt,
            frontend_tools=request.frontendTools,
            cancel_event=cancel_event,
        )

    try:
        try:
            result = await _managed_hub_process(
                run_pk=run_pk,
                execution_id=execution_id,
                thread_id=request.threadId,
                make_coro=_run_execute_segment,
            )
        except asyncio.CancelledError:
            return _session_closed_response(SESSION_CLOSING)
        except BindingError as exc:
            await _restore_execution_pending(
                execution_id,
                {
                    "success": False,
                    "error": exc.message,
                    "error_code": exc.code,
                },
            )
            return _binding_error_response(exc)
        except StorageError as exc:
            await _restore_execution_pending(
                execution_id,
                {
                    "success": False,
                    "error": exc.message,
                    "error_code": exc.code,
                },
            )
            return _storage_error_response(exc)
        except RootInstructionError as exc:
            logger.error("Root instructions exceed the eager budget: %s", exc)
            await _restore_execution_pending(
                execution_id,
                {
                    "success": False,
                    "error": str(exc),
                    "error_code": exc.code,
                },
            )
            return _root_instruction_error_response(exc)
        except Exception as exc:
            logger.error("Orchestrator execute %s failed: %s", execution_id, exc)
            await _restore_execution_pending(
                execution_id,
                {"success": False, "error": str(exc)},
            )
            raise HTTPException(status_code=500, detail=f"Execute failed: {str(exc)}")

        if not result.get("success"):
            run_status = RUN_STATUS_FAILED
            await _restore_execution_pending(execution_id, result)
            return JSONResponse(
                content=_run_response(execution_id, ExecutionStatus.PENDING, result)
            )

        if result.get("awaits_response"):
            try:
                state_id = await _persist_awaiting(
                    execution_id, result, thread_id=request.threadId, run_id=run_id
                )
            except Exception as exc:
                await _restore_execution_pending(
                    execution_id,
                    {
                        "success": False,
                        "error": f"Failed to persist awaiting state: {exc}",
                    },
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to persist awaiting state: {exc}",
                )
            run_status = RUN_STATUS_COMPLETED
            return JSONResponse(
                content=_run_response(
                    execution_id, ExecutionStatus.AWAITING_RESPONSE, result, state_id
                )
            )

        status = await _finalize(execution_id, result)
        run_status = RUN_STATUS_COMPLETED
        return JSONResponse(content=_run_response(execution_id, status, result))
    finally:
        await _complete_run_lifecycle(run_pk, execution_id, run_status=run_status)


@router.post("/resume", response_model=ExecuteRequestResponse)
async def resume(request: OrchestratorResumeInput):
    """Resume an orchestrator run that awaited user/tool input (pod-agnostic)."""
    execution_id = request.orchestratorGuid
    state_id = request.stateGuid

    async with get_session() as session:
        execution = await execution_state_service.get_execution(session, execution_id)
        if not execution:
            raise HTTPException(status_code=404, detail="Orchestrator session not found")
        recovered = await execution_state_service.recover_stale_pending_claims(
            session, execution_id=execution_id
        )
        if recovered:
            logger.info(
                "Recovered %d stale resume claim(s) for execution_id=%s",
                recovered,
                execution_id,
            )
        state = await execution_state_service.get_state(session, state_id)
        if not state or state.execution_id != execution_id:
            raise HTTPException(status_code=404, detail="LLM state not found for session")
        state_payload = state.state_payload
        state_status = state.status
        state_thread_id = getattr(state, "thread_id", None)
        state_run_id = getattr(state, "run_id", None) or str(uuid.uuid4())
        segment_config = _segment_config(execution)
        close_code = await _close_error_code(
            session, execution, thread_id=state_thread_id
        )
        if close_code:
            return _session_closed_response(close_code)

    if state_status == LLMStateStatus.PENDING:
        raise HTTPException(status_code=409, detail="Resume already in progress for this state")
    if state_status != LLMStateStatus.AWAITING_RESPONSE:
        raise HTTPException(status_code=400, detail="State is not awaiting a response")

    pending_tools = state_payload.get("pending_tools") or []
    if not pending_tools and state_payload.get("pending_tool"):
        pending_tools = [state_payload["pending_tool"]]
    pending_ids: set[str] = set()
    for pt in pending_tools:
        if pt.get("tool_call_id"):
            pending_ids.add(str(pt["tool_call_id"]))
        if pt.get("interrupt_id"):
            pending_ids.add(str(pt["interrupt_id"]))
    if request.toolCallId not in pending_ids:
        raise HTTPException(status_code=400, detail="toolCallId does not match pending tool batch")

    tool_result = request.result if request.result is not None else {}
    if not isinstance(tool_result, dict):
        tool_result = {"result": tool_result}
    if request.error:
        tool_result["error"] = request.error
    tool_result["tool_call_id"] = request.toolCallId
    tool_result["interrupt_id"] = request.toolCallId

    async with get_session() as session:
        close_code = await _close_error_code(
            session, execution, thread_id=state_thread_id
        )
        if close_code:
            return _session_closed_response(close_code)
        claimed = await execution_state_service.try_claim_state_for_resume(session, state_id)
        if not claimed:
            raise HTTPException(status_code=409, detail="Resume already claimed for this state")
        try:
            active_run = await run_lifecycle_service.try_create_active_run(
                session,
                execution_id=execution_id,
                run_id=state_run_id,
                thread_id=state_thread_id,
                origin="orchestrator",
                channel="orchestrator",
            )
        except RunClaimError as exc:
            await execution_state_service.restore_claimed_state(session, state_id)
            return _session_closed_response(exc.code)
        if active_run is None:
            await execution_state_service.restore_claimed_state(session, state_id)
            return JSONResponse(status_code=409, content=_run_conflict_payload())
        run_pk = active_run.id
        await execution_state_service.update_execution(
            session, execution_id, status=ExecutionStatus.RUNNING
        )

    run_status = RUN_STATUS_FAILED
    restore_claim = False
    restore_execution = False
    failure_result: Optional[dict] = None
    output_token = _output_access_token(request.credentials)

    async def _run_resume_segment(cancel_event: asyncio.Event) -> dict:
        workspace_path = await _ensure_workspace_for_segment(
            execution_id,
            execution,
            segment_config,
            input_access_token=_input_access_token(request.credentials),
        )
        resume_state = copy.deepcopy(state_payload)
        resume_state["messages"] = _refresh_resume_messages(
            resume_state, segment_config
        )
        resolved_model = resolve_resume_model(
            request.model,
            resume_state.get("model"),
            segment_config.get("model"),
        )
        resolved_max_calls = resolve_resume_max_tool_calls(
            request.maxToolCalls,
            resume_state.get("max_calls"),
            segment_config.get("maxToolCalls"),
        )
        local_ctx = _local_context(
            execution,
            segment_config,
            output_access_token=output_token,
            workspace_path=workspace_path,
        )
        hub = get_tool_hub()
        return await hub.process_request(
            request=resume_state.get("request", ""),
            model=resolved_model,
            max_tool_calls=resolved_max_calls,
            requested_tools=resume_state.get("requested_tools"),
            resume_state=resume_state,
            resume_tool_results=[tool_result],
            local_context=local_ctx,
            cancel_event=cancel_event,
        )

    try:
        try:
            result = await _managed_hub_process(
                run_pk=run_pk,
                execution_id=execution_id,
                thread_id=state_thread_id,
                make_coro=_run_resume_segment,
            )
        except asyncio.CancelledError:
            return _session_closed_response(SESSION_CLOSING)
        except BindingError as exc:
            restore_claim = True
            restore_execution = True
            failure_result = {
                "success": False,
                "error": exc.message,
                "error_code": exc.code,
            }
            return _binding_error_response(exc)
        except StorageError as exc:
            restore_claim = True
            restore_execution = True
            failure_result = {
                "success": False,
                "error": exc.message,
                "error_code": exc.code,
            }
            return _storage_error_response(exc)
        except Exception as exc:
            restore_claim = True
            restore_execution = True
            failure_result = {"success": False, "error": str(exc)}
            raise HTTPException(status_code=500, detail=f"Resume failed: {str(exc)}")

        if result.get("awaits_response"):
            try:
                new_state_id = await _persist_awaiting(
                    execution_id,
                    result,
                    thread_id=state_thread_id,
                    run_id=state_run_id,
                )
            except Exception as exc:
                restore_claim = True
                restore_execution = True
                failure_result = {
                    "success": False,
                    "error": f"Failed to persist awaiting state: {exc}",
                }
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to persist awaiting state: {exc}",
                )
            async with get_session() as session:
                if not await execution_state_service.complete_claimed_state(session, state_id):
                    restored = await execution_state_service.rollback_partial_resume_settlement(
                        session,
                        claimed_state_id=state_id,
                        new_state_id=new_state_id,
                    )
                    if not restored:
                        logger.error(
                            "Partial orchestrator resume settlement failed: claimed=%s new=%s",
                            state_id,
                            new_state_id,
                        )
                        restore_claim = True
                    restore_execution = True
                    failure_result = {
                        "success": False,
                        "error": "Failed to settle resume claim",
                    }
                    raise HTTPException(
                        status_code=500,
                        detail="Failed to settle resume claim",
                    )
            run_status = RUN_STATUS_COMPLETED
            return JSONResponse(
                content=_run_response(
                    execution_id, ExecutionStatus.AWAITING_RESPONSE, result, new_state_id
                )
            )

        if not result.get("success"):
            restore_claim = True
            restore_execution = True
            failure_result = result
            return JSONResponse(
                content=_run_response(
                    execution_id,
                    ExecutionStatus.AWAITING_RESPONSE,
                    result,
                    state_id,
                )
            )

        async with get_session() as session:
            if not await execution_state_service.complete_claimed_state(session, state_id):
                logger.error(
                    "Failed to complete claimed state %s after successful orchestrator resume",
                    state_id,
                )
                restore_claim = True
                restore_execution = True
                failure_result = {
                    "success": False,
                    "error": "Failed to settle resume claim",
                }
                raise HTTPException(status_code=500, detail="Failed to settle resume claim")
        status = await _finalize(execution_id, result)
        run_status = RUN_STATUS_COMPLETED
        return JSONResponse(content=_run_response(execution_id, status, result))
    finally:
        try:
            await _restore_claimed_state_if_needed(state_id, restore=restore_claim)
            if restore_execution:
                await _restore_execution_awaiting(execution_id, failure_result)
        finally:
            await _complete_run_lifecycle(run_pk, execution_id, run_status=run_status)


@router.post("/{orchestrator_guid}/close", response_model=OrchestratorCloseResponse)
async def close_orchestrator(orchestrator_guid: UUID):
    async with get_session() as session:
        execution = await execution_state_service.get_execution(session, orchestrator_guid)
        if not execution:
            raise HTTPException(status_code=404, detail="Orchestrator session not found")

    result = await session_close_service.close_execution(orchestrator_guid)
    status_code = 200 if result.status == "closed" else 202
    body = OrchestratorCloseResponse(
        status=result.status,
        already_closed=result.already_closed,
        discarded_holds=result.discarded_holds,
        runtime_deleted=result.runtime_deleted,
        workspace_deleted=result.workspace_deleted,
        workspace_deleted_count=result.workspace_deleted_count,
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(by_alias=True))


@router.get("/{orchestrator_guid}", response_model=ExecutionStatusResponse)
async def get_orchestrator_status(orchestrator_guid: UUID):
    async with get_session() as session:
        execution = await execution_state_service.get_execution(session, orchestrator_guid)
        if not execution:
            raise HTTPException(status_code=404, detail="Orchestrator session not found")
        latest_state = await execution_state_service.get_latest_state_for_execution(
            session, orchestrator_guid
        )

    awaits_response = execution.status == ExecutionStatus.AWAITING_RESPONSE
    state_id = None
    interrupts: list[dict] = []
    pending_ids: list[str] = []
    if awaits_response and latest_state and latest_state.status == LLMStateStatus.AWAITING_RESPONSE:
        state_id = latest_state.id
        interrupts, pending_ids = _pending_interrupt_payload(latest_state.state_payload)

    return ExecutionStatusResponse(
        executionGuid=orchestrator_guid,
        status=execution.status,
        result=execution.result,
        error=execution.error_message,
        awaitsResponse=awaits_response,
        stateGuid=state_id,
        interrupts=interrupts or None,
        pendingToolCallIds=pending_ids or None,
        closeRequestedAt=execution.close_requested_at,
        closedAt=execution.closed_at,
    )
