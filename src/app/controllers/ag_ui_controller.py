# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import contextlib
import copy
import json
import uuid
from asyncio import QueueEmpty
from types import SimpleNamespace
from typing import Any, Annotated, List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import or_, select

from ag_ui.core.types import Message, Tool as AGUITool, Context, ResumeEntry
from ag_ui.core.events import (
    RunStartedEvent,
    RunFinishedEvent,
    RunErrorEvent,
    TextMessageStartEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
)

from pathlib import Path

from app.config import Config
from app.controllers.agent_controller import get_tool_hub
from app.db.session import get_session
from app.models.execution_models import ExecutionStatus, LLMState, LLMStateStatus
from app.models.bindings import (
    INPUT_BINDING_REPLACEMENT_NOT_ALLOWED,
    OUTPUT_BINDING_REPLACEMENT_NOT_ALLOWED,
    SESSION_CLOSED,
    SESSION_CLOSING,
    ExecutionMode,
    LocationBinding,
    TransientCredentials,
)
from app.services.agui_interrupt import (
    build_persist_snapshot_events,
    build_run_finished_interrupt_event,
    extract_resume_entries,
    is_hook_permission_pending,
    normalize_resume_responses,
    tool_response_content,
)
from app.services.agui_messages import build_initial_messages
from app.services.agui_event_service import agui_event_service
from app.services.agui_service import agui_service
from app.services.context_compaction import collect_offload_references_from_state_payloads
from app.services.execution_state_service import execution_state_service
from app.services import workspace_manager
from app.services.binding_contract import (
    BindingError,
    assert_agui_phase1_input,
    assert_output_feature_enabled,
    input_from_workspace_path,
    provision_source,
    resolve_initiate_input,
    sanitize_binding,
    sanitize_execution_config,
    select_relative_workspace,
    should_provision_in_place,
    strip_secrets_from_mapping,
    synthetic_input_from_config,
)
from app.services.binding_runtime import (
    binding_system_prompt,
    ensure_input_workspace,
    refresh_segment_run_binding,
    segment_output_backend,
)
from app.services.local_tool_provider import LocalToolContext
from app.services.run_lifecycle import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RunClaimError,
    run_lifecycle_service,
    thread_key_for,
)
from app.services.runtime_paths import cleanup_unreferenced_offloads, ensure_runtime
from app.services.session_close_service import session_close_service
from app.services.storage import StorageError
from app.services.tool_hub import AGUIRunContext
from app.utils.logger import logger

router = APIRouter(prefix="/api/ag-ui", tags=["AG-UI"])

_RUN_CONFLICT = "RUN_CONFLICT"


class AGUIRunRequest(BaseModel):
    thread_id: Annotated[str, Field(alias="threadId")]
    run_id: Annotated[Optional[str], Field(default=None, alias="runId")]
    parent_run_id: Annotated[Optional[str], Field(default=None, alias="parentRunId")]
    model: Optional[str] = "gpt-3.5-turbo"
    max_tool_calls: Annotated[Optional[int], Field(default=None, alias="maxToolCalls")]
    llm_request_timeout_in_sec: Annotated[Optional[int], Field(default=None, alias="llmRequestTimeoutInSec")]
    messages: List[Message] = Field(default_factory=list)
    frontend_tools: Annotated[List[AGUITool], Field(default_factory=list, alias="frontendTools")]
    context: List[Context] = Field(default_factory=list)
    state: Optional[Any] = None
    resume: Annotated[Optional[List[ResumeEntry]], Field(default=None, alias="resume")]
    forwarded_props: Annotated[Optional[Any], Field(default=None, alias="forwardedProps")]
    workspace_path: Annotated[Optional[str], Field(default=None, alias="workspacePath")] = None
    in_place: Annotated[Optional[bool], Field(default=None, alias="inPlace")] = None
    input: Optional[LocationBinding] = None
    output: Optional[LocationBinding] = None
    mode: Optional[ExecutionMode] = None
    credentials: Optional[TransientCredentials] = None

    class Config:
        populate_by_name = True


class AGUIThreadAbandonResponse(BaseModel):
    thread_id: Annotated[str, Field(alias="threadId")]
    discarded: int
    status: str = "abandoned"

    class Config:
        populate_by_name = True


class AGUIThreadCloseResponse(BaseModel):
    thread_id: Annotated[str, Field(alias="threadId")]
    status: str
    already_closed: Annotated[bool, Field(alias="alreadyClosed")]
    discarded_holds: Annotated[int, Field(alias="discardedHolds")]
    runtime_deleted: Annotated[bool, Field(alias="runtimeDeleted")]
    workspace_deleted: Annotated[bool, Field(alias="workspaceDeleted")]
    workspace_deleted_count: Annotated[int, Field(alias="workspaceDeletedCount")]

    class Config:
        populate_by_name = True


class AGUIToolResponsePayload(BaseModel):
    tool_call_id: Annotated[str, Field(alias="toolCallId")]
    result: Optional[Any] = None
    error: Optional[str] = None

    class Config:
        populate_by_name = True

    def to_tool_result(self) -> dict:
        payload: dict = {}
        if self.result is not None:
            payload["result"] = self.result
        if self.error is not None:
            payload["error"] = self.error
        if not payload:
            payload["result"] = {"status": "acknowledged"}
        return payload


def _thread_state_scope(thread_id: str):
    key = thread_key_for(thread_id)
    return or_(LLMState.thread_id == thread_id, LLMState.thread_key == key)


def _input_access_token(credentials: Optional[TransientCredentials]) -> Optional[str]:
    return credentials.input_access_token if credentials else None


def _output_access_token(credentials: Optional[TransientCredentials]) -> Optional[str]:
    return credentials.output_access_token if credentials else None


async def _thread_close_error_code(session, thread_id: str) -> Optional[str]:
    key = thread_key_for(thread_id)
    row = await run_lifecycle_service.get_thread_session(session, key)
    if row is not None:
        if row.closed_at is not None:
            return SESSION_CLOSED
        if row.close_requested_at is not None:
            return SESSION_CLOSING
    if await run_lifecycle_service.reject_if_close_requested(
        session, thread_id=thread_id
    ):
        return SESSION_CLOSING
    return None


def _session_closed_stream(error_code: str) -> StreamingResponse:
    message = (
        "Session is closed"
        if error_code == SESSION_CLOSED
        else "Session close is in progress"
    )

    async def error_stream():
        yield _serialize_event(RunErrorEvent(message=f"{error_code}: {message}"))

    return StreamingResponse(error_stream(), media_type="text/event-stream")


def _run_conflict_stream() -> StreamingResponse:
    async def error_stream():
        yield _serialize_event(
            RunErrorEvent(
                message=f"{_RUN_CONFLICT}: Another run is active for this thread"
            )
        )

    return StreamingResponse(error_stream(), media_type="text/event-stream")


def _binding_error_stream(exc: BindingError) -> StreamingResponse:
    async def error_stream():
        yield _serialize_event(RunErrorEvent(message=f"{exc.code}: {exc.message}"))

    return StreamingResponse(error_stream(), media_type="text/event-stream")


def _storage_error_stream(exc: StorageError) -> StreamingResponse:
    async def error_stream():
        yield _serialize_event(RunErrorEvent(message=f"{exc.code}: {exc.message}"))

    return StreamingResponse(error_stream(), media_type="text/event-stream")


def _input_replacement_stream() -> StreamingResponse:
    async def error_stream():
        yield _serialize_event(
            RunErrorEvent(
                message=(
                    f"{INPUT_BINDING_REPLACEMENT_NOT_ALLOWED}: "
                    "input binding cannot be changed during resume"
                )
            )
        )

    return StreamingResponse(error_stream(), media_type="text/event-stream")


def _requested_input_differs(
    *,
    requested_input: Optional[LocationBinding],
    requested_workspace_path: Optional[str],
    stored_config: dict,
    stored_workspace: Optional[str],
) -> bool:
    stored_input = stored_config.get("input")
    if requested_input is not None:
        requested_sanitized = sanitize_binding(requested_input)
        if requested_sanitized != (stored_input or None):
            return True
    if requested_workspace_path:
        if not stored_workspace:
            return True
        try:
            requested_resolved = str(Path(requested_workspace_path).expanduser().resolve())
            stored_resolved = str(Path(stored_workspace).expanduser().resolve())
        except OSError:
            requested_resolved = requested_workspace_path
            stored_resolved = stored_workspace
        if requested_resolved != stored_resolved:
            return True
    return False


def _invoke_make_task(make_task, cancel_event: Optional[asyncio.Event]):
    try:
        return make_task(cancel_event)
    except TypeError:
        return make_task()


def _close_discarded_claim(cancel_event: Optional[asyncio.Event]) -> bool:
    if cancel_event is None:
        return False
    is_set = getattr(cancel_event, "is_set", None)
    if callable(is_set):
        result = is_set()
        if asyncio.iscoroutine(result):
            return False
        return bool(result)
    return False


async def _terminalize_failed_execution(
    execution_id: uuid.UUID,
    *,
    provisioned: bool = False,
    error: str,
) -> None:
    if provisioned:
        workspace_manager.cleanup(execution_id)
    await _finalize(execution_id, {"success": False, "error": error})


async def _abort_fresh_run_prep(
    run_pk: uuid.UUID,
    thread_id: str,
    execution_id: uuid.UUID,
    *,
    provisioned: bool = False,
    error: str,
) -> None:
    await _abort_active_run(run_pk, thread_id, restore_claim=False)
    await _terminalize_failed_execution(
        execution_id,
        provisioned=provisioned,
        error=error,
    )


def _minimal_execution_config(
    *,
    runtime_path: str,
    mode: Optional[ExecutionMode] = None,
    output_binding: Optional[LocationBinding] = None,
) -> dict:
    config: dict[str, Any] = {
        "mode": (mode or ExecutionMode.WORKFLOW).value,
        "runtimePath": runtime_path,
    }
    if output_binding is not None and Config.OUTPUT_BINDINGS_ENABLED:
        config["output"] = sanitize_binding(output_binding)
    else:
        config["output"] = None
    return strip_secrets_from_mapping(config)


def _resolve_local_context(
    workspace_path: Optional[str],
    in_place: Optional[bool],
    *,
    runtime_path: Optional[str] = None,
    thread_id: Optional[str] = None,
    mode: str = "working_copy",
    output_backend=None,
) -> Optional[LocalToolContext]:
    """Build a LocalToolContext when the client binds a workspace directory."""
    if not workspace_path or not str(workspace_path).strip():
        return None
    path = Path(str(workspace_path).strip()).expanduser()
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path)
    want_inplace = True if in_place is None else bool(in_place)
    if want_inplace and not Config.ALLOW_INPLACE_WORKSPACE:
        logger.warning(
            "Ignoring workspacePath=%s: ALLOW_INPLACE_WORKSPACE is false",
            resolved,
        )
        return None
    if not Path(resolved).is_dir():
        logger.warning("Ignoring workspacePath=%s: not a directory", resolved)
        return None
    if want_inplace:
        try:
            from app.services import path_policy
            from app.services.workspace_manager import WorkspaceError

            path_policy.check_workspace_root_allowed(resolved)
        except WorkspaceError as exc:
            logger.warning("Ignoring workspacePath=%s: %s", resolved, exc)
            return None
    if not runtime_path and thread_id:
        runtime_path = ensure_runtime(thread_id=thread_id)
    return LocalToolContext(
        workspace_path=resolved,
        in_place=want_inplace,
        runtime_path=runtime_path,
        mode=mode,
        output_backend=output_backend,
    )


def _local_context_from_segment(
    execution,
    config: dict,
    *,
    output_access_token: Optional[str] = None,
    workspace_path: Optional[str] = None,
) -> LocalToolContext:
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


def _build_fresh_system_prompt(
    workspace_path: Optional[str],
    execution_config: dict,
) -> tuple[str, Optional[dict]]:
    """Single harness discovery pass and one tagged binding refresh."""
    harness: Optional[str] = None
    manifest_summary: Optional[dict] = None
    if workspace_path:
        try:
            from app.services.harness import collect_manifest, render_system_prompt

            manifest = collect_manifest(workspace_path)
            manifest_summary = manifest.summary()
            logger.info(
                "AG-UI harness discovery: workspace=%s type=%s skills=%d commands=%d "
                "rules=%d agents=%d notes=%s",
                workspace_path,
                manifest.orchestration_type,
                len(manifest.skills),
                len(manifest.commands),
                len(manifest.rules),
                len(manifest.agents),
                manifest.notes,
            )
            harness = render_system_prompt(manifest)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "AG-UI harness discovery failed for %s: %s", workspace_path, exc
            )

    combined = "\n\n".join(part for part in (harness, binding_system_prompt(execution_config)) if part)
    refreshed = refresh_segment_run_binding(
        [{"role": "system", "content": combined or ""}],
        execution_config,
    )
    content = refreshed[0].get("content")
    system_prompt = content if isinstance(content, str) else str(content or "")
    return system_prompt, manifest_summary


def _render_messages_to_prompt(messages: List[Message], contexts: List[Context]) -> str:
    parts: List[str] = []

    if contexts:
        context_lines = [f"{ctx.description}: {ctx.value}" for ctx in contexts if ctx.description and ctx.value]
        if context_lines:
            parts.append("Context:\n" + "\n".join(context_lines))

    for message in messages:
        role = message.role.capitalize()
        content = _extract_message_content(message)
        if content:
            parts.append(f"{role}: {content}")

    return "\n\n".join(parts)


def _extract_message_content(message: Message) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        fragments = []
        for fragment in content:
            fragment_type = getattr(fragment, "type", None)
            if fragment_type == "text":
                fragments.append(getattr(fragment, "text", ""))
        return "\n".join(filter(None, fragments))

    return ""


def _serialize_event(event) -> str:
    envelope = None
    if hasattr(event, "event") and hasattr(event, "thread_id") and hasattr(event, "run_id"):
        envelope = event
        event = envelope.event

    payload = event.model_dump(by_alias=True, exclude_none=True)
    if envelope:
        metadata = {}
        if envelope.thread_id:
            metadata["threadId"] = envelope.thread_id
        if envelope.run_id:
            metadata["runId"] = envelope.run_id
        payload.update({k: v for k, v in metadata.items() if k not in payload})

    return f"data: {json.dumps(payload)}\n\n"


def _extract_tool_response(state_payload: Any) -> Optional[AGUIToolResponsePayload]:
    if not state_payload:
        return None

    candidate = None
    if isinstance(state_payload, dict):
        if "toolResponse" in state_payload and isinstance(state_payload["toolResponse"], dict):
            candidate = state_payload["toolResponse"]
        else:
            candidate = state_payload

    if not isinstance(candidate, dict):
        return None

    try:
        return AGUIToolResponsePayload.model_validate(candidate)
    except ValidationError:
        return None


def _envelope_matches_run(envelope, thread_id: str, run_id: str) -> bool:
    if getattr(envelope, "thread_id", None) not in (None, thread_id):
        return False
    run_scope = getattr(envelope, "run_id", None)
    if run_scope is not None and run_scope != run_id:
        return False
    return True


async def _create_agui_execution(
    *,
    thread_id: str,
    source: Optional[str] = None,
    workspace_path: Optional[str] = None,
    config: Optional[dict] = None,
) -> uuid.UUID:
    payload = strip_secrets_from_mapping(dict(config or {}))
    async with get_session() as session:
        execution = await execution_state_service.create_execution(
            session,
            status=ExecutionStatus.RUNNING,
            source=source,
            workspace_path=workspace_path,
            config=payload,
        )
        await run_lifecycle_service.associate_execution_origin(
            session,
            execution.id,
            origin="agui",
            thread_id=thread_id,
        )
        return execution.id


async def _persist_await(
    execution_id,
    result: dict,
    thread_id: str,
    run_id: str,
    *,
    workspace_path: Optional[str] = None,
    in_place: bool = False,
    runtime_path: Optional[str] = None,
) -> uuid.UUID:
    state_payload = result.get("state")
    pending_tools = result.get("pending_tools") or []
    if not pending_tools and result.get("pending_tool"):
        pending_tools = [result["pending_tool"]]
    primary = pending_tools[0] if pending_tools else {}
    async with get_session() as session:
        state = await execution_state_service.create_state(
            session,
            execution_id=execution_id,
            payload=state_payload,
            status=LLMStateStatus.AWAITING_RESPONSE,
            thread_id=thread_id,
            run_id=run_id,
            tool_call_id=primary.get("tool_call_id"),
        )
        update_kwargs: dict = {"status": ExecutionStatus.AWAITING_RESPONSE}
        execution = await execution_state_service.get_execution(session, execution_id)
        merged = strip_secrets_from_mapping(dict((execution.config if execution else None) or {}))
        if workspace_path:
            update_kwargs["workspace_path"] = workspace_path
            merged["inPlace"] = in_place
        if runtime_path:
            merged["runtimePath"] = runtime_path
        update_kwargs["config"] = merged
        await execution_state_service.update_execution(
            session, execution_id, **update_kwargs
        )
        return state.id


async def _finalize(execution_id, result: dict) -> None:
    status = ExecutionStatus.COMPLETED if result.get("success") else ExecutionStatus.FAILED
    async with get_session() as session:
        await execution_state_service.update_execution(
            session,
            execution_id,
            status=status,
            result=result if result.get("success") else None,
            error_message=None if result.get("success") else result.get("error"),
        )


async def _complete_claimed_state(state_id) -> bool:
    async with get_session() as session:
        return await execution_state_service.complete_claimed_state(session, state_id)


async def _restore_claimed_state(state_id) -> bool:
    async with get_session() as session:
        return await execution_state_service.restore_claimed_state(session, state_id)


async def _rollback_partial_resume_settlement(*, claimed_state_id, new_state_id) -> bool:
    async with get_session() as session:
        return await execution_state_service.rollback_partial_resume_settlement(
            session,
            claimed_state_id=claimed_state_id,
            new_state_id=new_state_id,
        )


async def _finish_active_run(
    run_pk: uuid.UUID,
    *,
    status: str,
    update_execution_status: bool = False,
) -> None:
    async with get_session() as session:
        await run_lifecycle_service.finish_run(
            session,
            run_pk,
            status=status,
            update_execution_status=update_execution_status,
        )


async def _finalize_run_segment(
    run_pk: uuid.UUID,
    thread_id: str,
    *,
    run_status: str,
) -> None:
    await _finish_active_run(run_pk, status=run_status, update_execution_status=False)
    await session_close_service.reconcile_thread_close(thread_id)


async def _restore_execution_awaiting(execution_id: uuid.UUID) -> None:
    async with get_session() as session:
        await execution_state_service.update_execution(
            session,
            execution_id,
            status=ExecutionStatus.AWAITING_RESPONSE,
        )


async def _abort_active_run(
    run_pk: uuid.UUID,
    thread_id: str,
    *,
    state_id: Optional[uuid.UUID] = None,
    restore_claim: bool = False,
    restore_execution_awaiting: bool = False,
    execution_id: Optional[uuid.UUID] = None,
) -> None:
    if restore_claim and state_id is not None:
        async with get_session() as session:
            await execution_state_service.restore_claimed_state(session, state_id)
    if restore_execution_awaiting and execution_id is not None:
        await _restore_execution_awaiting(execution_id)
    await _finalize_run_segment(run_pk, thread_id, run_status=RUN_STATUS_FAILED)


async def _ensure_workspace_for_agui_segment(
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


async def _prepare_thread_claims(thread_id: str) -> tuple[int, bool]:
    if not thread_id:
        return 0, False
    async with get_session() as session:
        recovered = await execution_state_service.recover_stale_pending_claims(
            session, thread_id=thread_id
        )
        active = await execution_state_service.get_latest_pending_hold_for_thread(
            session, thread_id
        )
    if recovered:
        logger.info(
            "Recovered %d stale resume claim(s) for thread_id=%s",
            recovered,
            thread_id,
        )
    return recovered, active is not None


async def _discard_stale_awaiting_and_cleanup_orphans(thread_id: str) -> int:
    live_payloads: list[dict] = []
    discarded = 0
    async with get_session() as session:
        scope = _thread_state_scope(thread_id)
        pending_result = await session.execute(
            select(LLMState).where(scope).where(LLMState.status == LLMStateStatus.PENDING)
        )
        for state in pending_result.scalars().all():
            if state.state_payload:
                live_payloads.append(state.state_payload)
        discarded = await execution_state_service.discard_awaiting_states_for_thread(
            session, thread_id
        )

    if not discarded:
        return 0

    logger.info(
        "AG-UI fresh run discarded %d stale awaiting hold(s) for thread_id=%s",
        discarded,
        thread_id,
    )
    live_refs = collect_offload_references_from_state_payloads(live_payloads)
    runtime_path = ensure_runtime(thread_id=thread_id)
    cleanup_unreferenced_offloads(runtime_path, referenced_logical_paths=live_refs)
    return discarded


def _stream_run(
    *,
    make_task,
    thread_id: str,
    run_id: str,
    execution_id: uuid.UUID,
    run_pk: uuid.UUID,
    parent_run_id: Optional[str] = None,
    claimed_state_id=None,
    pre_events: Optional[List[Any]] = None,
    workspace_path: Optional[str] = None,
    in_place: bool = False,
    runtime_path: Optional[str] = None,
):
    async def event_stream():
        queue = await agui_event_service.subscribe()
        queue_task: Optional[asyncio.Task] = None
        settled = False
        cancel_event: Optional[asyncio.Event] = None
        final_run_status = RUN_STATUS_FAILED

        async def _cancel_queue_task():
            nonlocal queue_task
            if queue_task:
                queue_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await queue_task
                queue_task = None

        async def _execute_managed():
            nonlocal cancel_event
            ready = asyncio.Event()
            shared: dict[str, asyncio.Event] = {}

            async def _runner():
                await ready.wait()
                return await _invoke_make_task(make_task, shared["cancel_event"])

            task = asyncio.create_task(_runner())
            try:
                async with session_close_service.manage_run(
                    run_pk=run_pk,
                    execution_id=execution_id,
                    task=task,
                    thread_id=thread_id,
                ) as ce:
                    cancel_event = ce
                    shared["cancel_event"] = ce
                    ready.set()
                    return await task
            finally:
                if not ready.is_set():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        run_task = asyncio.create_task(_execute_managed())

        try:
            yield _serialize_event(
                RunStartedEvent(thread_id=thread_id, run_id=run_id, parent_run_id=parent_run_id)
            )
            for event in pre_events or []:
                yield _serialize_event(event)

            while True:
                wait_set = {run_task}
                if queue_task is None:
                    queue_task = asyncio.create_task(queue.get())
                wait_set.add(queue_task)

                done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

                if queue_task in done:
                    envelope = queue_task.result()
                    queue_task = None
                    if _envelope_matches_run(envelope, thread_id, run_id):
                        yield _serialize_event(envelope)
                    continue

                if run_task in done:
                    break

            await _cancel_queue_task()

            while True:
                try:
                    pending = queue.get_nowait()
                except QueueEmpty:
                    break
                if _envelope_matches_run(pending, thread_id, run_id):
                    yield _serialize_event(pending)

            result = await run_task

            if result.get("awaits_response"):
                try:
                    state_id = await _persist_await(
                        execution_id,
                        result,
                        thread_id,
                        run_id,
                        workspace_path=workspace_path,
                        in_place=in_place,
                        runtime_path=runtime_path,
                    )
                except Exception:
                    if claimed_state_id and not _close_discarded_claim(cancel_event):
                        restored = await _restore_claimed_state(claimed_state_id)
                        if not restored:
                            logger.warning(
                                "Failed to restore claimed state %s after persist error",
                                claimed_state_id,
                            )
                    settled = True
                    raise
                if claimed_state_id:
                    if not await _complete_claimed_state(claimed_state_id):
                        restored = await _rollback_partial_resume_settlement(
                            claimed_state_id=claimed_state_id,
                            new_state_id=state_id,
                        )
                        settled = True
                        if not restored:
                            logger.error(
                                "Partial resume settlement failed: claimed=%s new=%s",
                                claimed_state_id,
                                state_id,
                            )
                        yield _serialize_event(
                            RunErrorEvent(message="Failed to settle resume claim")
                        )
                        return
                settled = True
                final_run_status = RUN_STATUS_COMPLETED
                pending_tools = result.get("pending_tools") or []
                if not pending_tools and result.get("pending_tool"):
                    pending_tools = [result["pending_tool"]]
                state_payload = result.get("state") or {}
                state_payload = dict(state_payload)
                state_payload["executionGuid"] = str(execution_id)
                state_payload["stateGuid"] = str(state_id)
                for event in build_persist_snapshot_events(
                    state_payload=state_payload,
                    pending_tools=pending_tools,
                ):
                    yield _serialize_event(event)
                yield _serialize_event(
                    build_run_finished_interrupt_event(
                        thread_id=thread_id,
                        run_id=run_id,
                        pending_tools=pending_tools,
                        execution_guid=str(execution_id),
                        state_guid=str(state_id),
                    )
                )
                return

            assistant_response = result.get("response") or result.get("partial_response")
            if assistant_response and not result.get("text_streamed"):
                message_id = str(uuid.uuid4())
                yield _serialize_event(TextMessageStartEvent(message_id=message_id, role="assistant"))
                yield _serialize_event(TextMessageContentEvent(message_id=message_id, delta=assistant_response))
                yield _serialize_event(TextMessageEndEvent(message_id=message_id))

            await _finalize(execution_id, result)

            if not result.get("success"):
                if claimed_state_id and not _close_discarded_claim(cancel_event):
                    if not await _restore_claimed_state(claimed_state_id):
                        logger.warning(
                            "Failed to restore claimed state %s after result failure",
                            claimed_state_id,
                        )
                settled = True
                yield _serialize_event(RunErrorEvent(message=result.get("error") or "AG-UI run failed"))
                return

            if claimed_state_id:
                if not await _complete_claimed_state(claimed_state_id):
                    settled = True
                    logger.error(
                        "Failed to complete claimed state %s after successful final result",
                        claimed_state_id,
                    )
                    yield _serialize_event(
                        RunErrorEvent(message="Failed to settle resume claim")
                    )
                    return
            settled = True
            final_run_status = RUN_STATUS_COMPLETED

            if result.get("success"):
                yield _serialize_event(
                    RunFinishedEvent(
                        thread_id=thread_id,
                        run_id=run_id,
                        result={"toolCalls": result.get("tool_calls_info", [])},
                    )
                )
        except asyncio.CancelledError:
            if claimed_state_id and not settled and not _close_discarded_claim(cancel_event):
                if not await _restore_claimed_state(claimed_state_id):
                    logger.warning(
                        "Failed to restore claimed state %s after stream cancellation",
                        claimed_state_id,
                    )
            yield _serialize_event(
                RunErrorEvent(message=f"{SESSION_CLOSING}: Session close is in progress")
            )
            return
        except Exception as exc:
            logger.error(f"AG-UI run failed: {exc}")
            if claimed_state_id and not settled and not _close_discarded_claim(cancel_event):
                if not await _restore_claimed_state(claimed_state_id):
                    logger.warning(
                        "Failed to restore claimed state %s after run exception",
                        claimed_state_id,
                    )
                settled = True
            if not run_task.done():
                run_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await run_task
            yield _serialize_event(RunErrorEvent(message=str(exc)))
        finally:
            await _cancel_queue_task()
            await agui_event_service.unsubscribe(queue)
            await _finalize_run_segment(run_pk, thread_id, run_status=final_run_status)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def _handle_tool_response(
    responses: List[dict],
    frontend_tools: List[dict],
    *,
    request_thread_id: str,
    requested_input: Optional[LocationBinding] = None,
    requested_workspace_path: Optional[str] = None,
    requested_output: Optional[LocationBinding] = None,
    input_access_token: Optional[str] = None,
    output_access_token: Optional[str] = None,
):
    if not responses:
        async def error_stream():
            yield _serialize_event(RunErrorEvent(message="No tool responses provided"))

        return StreamingResponse(error_stream(), media_type="text/event-stream")

    primary = responses[0]
    tool_call_id = (
        primary.get("tool_call_id")
        or primary.get("toolCallId")
        or primary.get("interrupt_id")
        or primary.get("interruptId")
    )

    async with get_session() as session:
        await execution_state_service.recover_stale_pending_claims(
            session, thread_id=request_thread_id
        )
        state = await execution_state_service.get_resume_hold_by_tool_call_id(
            session, tool_call_id, thread_id=request_thread_id
        )
        if not state:
            logger.error(
                "No pending AG-UI state for tool_call_id=%s thread_id=%s",
                tool_call_id,
                request_thread_id,
            )

            async def error_stream():
                yield _serialize_event(
                    RunErrorEvent(message=f"No pending tool call {tool_call_id}")
                )

            return StreamingResponse(error_stream(), media_type="text/event-stream")

        if state.thread_id and state.thread_id != request_thread_id:
            async def cross_thread_stream():
                yield _serialize_event(
                    RunErrorEvent(
                        message=(
                            f"No pending tool call {tool_call_id} for thread "
                            f"{request_thread_id}"
                        )
                    )
                )

            return StreamingResponse(cross_thread_stream(), media_type="text/event-stream")

        if state.status == LLMStateStatus.PENDING:
            async def in_progress_stream():
                yield _serialize_event(
                    RunErrorEvent(message=f"Tool call {tool_call_id} resume already in progress")
                )

            return StreamingResponse(in_progress_stream(), media_type="text/event-stream")

        execution = await execution_state_service.get_execution(session, state.execution_id)
        state_payload = state.state_payload
        state_id = state.id
        execution_id = state.execution_id
        state_thread_id = state.thread_id or request_thread_id
        state_run_id = state.run_id or str(uuid.uuid4())
        stored_workspace = execution.workspace_path if execution else None
        stored_config = synthetic_input_from_config(
            (execution.config or {}) if execution else {},
            execution.source if execution else None,
        )

        close_code = await _thread_close_error_code(session, request_thread_id)
        if close_code:
            return _session_closed_stream(close_code)

        if requested_output is not None and sanitize_binding(requested_output) != stored_config.get("output"):
            async def replacement_error_stream():
                yield _serialize_event(
                    RunErrorEvent(
                        message=(
                            f"{OUTPUT_BINDING_REPLACEMENT_NOT_ALLOWED}: "
                            "output binding cannot be changed during resume"
                        )
                    )
                )

            return StreamingResponse(
                replacement_error_stream(), media_type="text/event-stream"
            )

        if _requested_input_differs(
            requested_input=requested_input,
            requested_workspace_path=requested_workspace_path,
            stored_config=stored_config,
            stored_workspace=stored_workspace,
        ):
            return _input_replacement_stream()

        claimed = await execution_state_service.try_claim_state_for_resume(session, state_id)
        if not claimed:
            msgs = state_payload.get("messages") or []
            already = any(
                m.get("role") == "tool" and m.get("tool_call_id") == tool_call_id for m in msgs
            )
            if already:
                async def idempotent_stream():
                    yield _serialize_event(
                        RunStartedEvent(thread_id=state_thread_id, run_id=state_run_id)
                    )
                    yield _serialize_event(
                        RunFinishedEvent(
                            thread_id=state_thread_id,
                            run_id=state_run_id,
                            result={"idempotent": True, "toolCallId": tool_call_id},
                        )
                    )

                return StreamingResponse(idempotent_stream(), media_type="text/event-stream")

            async def duplicate_stream():
                yield _serialize_event(
                    RunErrorEvent(message=f"Tool call {tool_call_id} was already resumed")
                )

            return StreamingResponse(duplicate_stream(), media_type="text/event-stream")

        try:
            active_run = await run_lifecycle_service.try_create_active_run(
                session,
                execution_id=execution_id,
                run_id=state_run_id,
                thread_id=state_thread_id,
                origin="agui",
                channel="agui",
            )
        except RunClaimError as exc:
            await execution_state_service.restore_claimed_state(session, state_id)
            return _session_closed_stream(exc.code)
        if active_run is None:
            await execution_state_service.restore_claimed_state(session, state_id)

            async def conflict_stream():
                yield _serialize_event(
                    RunErrorEvent(
                        message=f"{_RUN_CONFLICT}: Another run is active for this thread"
                    )
                )

            return StreamingResponse(conflict_stream(), media_type="text/event-stream")

        run_pk = active_run.id
        await execution_state_service.update_execution(
            session, execution_id, status=ExecutionStatus.RUNNING
        )

    try:
        workspace_path = await _ensure_workspace_for_agui_segment(
            execution_id,
            execution,
            stored_config,
            input_access_token=input_access_token,
        )
        resume_state = copy.deepcopy(state_payload)
        resume_state["messages"] = refresh_segment_run_binding(
            resume_state.get("messages") or [],
            stored_config,
        )
        local_context = _local_context_from_segment(
            execution,
            stored_config,
            output_access_token=output_access_token,
            workspace_path=workspace_path,
        )
    except BindingError as exc:
        await _abort_active_run(
            run_pk,
            state_thread_id,
            state_id=state_id,
            restore_claim=True,
            restore_execution_awaiting=True,
            execution_id=execution_id,
        )
        return _binding_error_stream(exc)
    except StorageError as exc:
        await _abort_active_run(
            run_pk,
            state_thread_id,
            state_id=state_id,
            restore_claim=True,
            restore_execution_awaiting=True,
            execution_id=execution_id,
        )
        return _storage_error_stream(exc)

    bound_workspace = local_context.workspace_path if local_context else workspace_path
    bound_inplace = local_context.in_place if local_context else bool(stored_config.get("inPlace", False))
    bound_runtime = local_context.runtime_path if local_context else stored_config.get("runtimePath")

    resume_payloads = []
    for resp in responses:
        if isinstance(resp, AGUIToolResponsePayload):
            resume_payloads.append(resp.to_tool_result() | {"tool_call_id": resp.tool_call_id})
        elif isinstance(resp, dict):
            tc = resp.get("tool_call_id") or resp.get("toolCallId")
            payload = dict(resp)
            if tc:
                payload["tool_call_id"] = tc
            resume_payloads.append(payload)

    pending_tools = list(state_payload.get("pending_tools") or [])
    if not pending_tools and state_payload.get("pending_tool"):
        pending_tools = [state_payload["pending_tool"]]
    updated_payload = dict(resume_state)
    pre_events = []
    if any(is_hook_permission_pending(pt) for pt in pending_tools):
        hook_events, updated_payload = await get_tool_hub().prepare_hook_permission_resume_events(
            state_payload=updated_payload,
            resume_tool_results=resume_payloads,
            local_context=local_context,
            model=updated_payload.get("model", "gpt-3.5-turbo"),
            lite_llm_timeout=updated_payload.get("lite_llm_request_timeout_in_sec"),
            agui_context=AGUIRunContext(thread_id=state_thread_id, run_id=state_run_id),
        )
        pre_events.extend(hook_events)

    def make_task(cancel_event=None):
        return get_tool_hub().process_request(
            request=updated_payload.get("request", ""),
            model=updated_payload.get("model", "gpt-3.5-turbo"),
            max_tool_calls=updated_payload.get("max_calls"),
            requested_tools=updated_payload.get("requested_tools"),
            resume_state=updated_payload,
            resume_tool_results=resume_payloads,
            agui_context=AGUIRunContext(thread_id=state_thread_id, run_id=state_run_id),
            local_context=local_context,
            frontend_tools=frontend_tools,
            cancel_event=cancel_event,
        )

    return _stream_run(
        make_task=make_task,
        thread_id=state_thread_id,
        run_id=state_run_id,
        execution_id=execution_id,
        run_pk=run_pk,
        claimed_state_id=state_id,
        pre_events=pre_events,
        workspace_path=bound_workspace,
        in_place=bound_inplace,
        runtime_path=bound_runtime,
    )


@router.post("/run")
async def run_agui_session(payload: AGUIRunRequest):
    """Entry point for AG-UI clients to start a run via HTTP POST + SSE."""
    tool_execution_hub = get_tool_hub()

    agui_service.refresh_frontend_tools([tool.model_dump(by_alias=True) for tool in payload.frontend_tools])

    run_id = payload.run_id or str(uuid.uuid4())
    thread_id = payload.thread_id
    parent_run_id = payload.parent_run_id

    frontend_tools = [tool.model_dump(by_alias=True) for tool in payload.frontend_tools]
    agui_service.refresh_frontend_tools(frontend_tools)

    tool_response = _extract_tool_response(payload.state)
    resume_entries = list(payload.resume or extract_resume_entries(payload.state) or [])
    is_resume_request = bool(resume_entries or tool_response)

    try:
        assert_output_feature_enabled(payload.output)
    except BindingError as exc:
        return _binding_error_stream(exc)

    async with get_session() as session:
        await run_lifecycle_service.ensure_thread_session(session, thread_id)
        close_code = await _thread_close_error_code(session, thread_id)
        if close_code:
            return _session_closed_stream(close_code)

    if not payload.messages and not is_resume_request:
        logger.info("AG-UI bind-only run detected (no messages). Skipping LLM invocation.")
        events: List[Any] = [
            RunStartedEvent(thread_id=thread_id, run_id=run_id, parent_run_id=parent_run_id),
            RunFinishedEvent(
                thread_id=thread_id,
                run_id=run_id,
                result={
                    "message": "Frontend tools registered",
                    "toolCount": len(payload.frontend_tools),
                },
            ),
        ]

        async def bind_only_stream():
            for event in events:
                yield _serialize_event(event)

        return StreamingResponse(bind_only_stream(), media_type="text/event-stream")

    _, active_pending = await _prepare_thread_claims(thread_id)
    if not is_resume_request and active_pending:
        async def blocked_fresh_run_stream():
            yield _serialize_event(
                RunErrorEvent(
                    message="Resume in progress for this thread; cannot start a fresh run"
                )
            )

        return StreamingResponse(blocked_fresh_run_stream(), media_type="text/event-stream")

    if is_resume_request:
        awaiting_state_payload: Optional[dict] = None
        if active_pending:
            async def in_progress_stream():
                yield _serialize_event(
                    RunErrorEvent(
                        message="Resume already in progress for this thread"
                    )
                )

            return StreamingResponse(in_progress_stream(), media_type="text/event-stream")

        async with get_session() as session:
            pending_hold = await execution_state_service.get_latest_pending_hold_for_thread(
                session, thread_id
            )
            if pending_hold:
                async def in_progress_stream():
                    yield _serialize_event(
                        RunErrorEvent(
                            message="Resume already in progress for this thread"
                        )
                    )

                return StreamingResponse(in_progress_stream(), media_type="text/event-stream")

            awaiting = await execution_state_service.get_latest_awaiting_state_for_thread(
                session, thread_id
            )
            if awaiting:
                awaiting_state_payload = awaiting.state_payload

        resume_responses = normalize_resume_responses(
            resume_entries=resume_entries,
            legacy_tool_response=(
                tool_response.model_dump(by_alias=True) if tool_response else None
            ),
            state_payload=awaiting_state_payload,
        )

        if resume_entries and not resume_responses:
            logger.error(
                "AG-UI resume[] could not be matched to pending tools thread_id=%s run_id=%s",
                thread_id,
                run_id,
            )

            async def invalid_resume_stream():
                yield _serialize_event(
                    RunErrorEvent(
                        message="Resume entries could not be matched to pending tool calls"
                    )
                )

            return StreamingResponse(invalid_resume_stream(), media_type="text/event-stream")

        legacy_dicts = []
        if tool_response and not resume_responses:
            legacy_dicts.append(
                {"tool_call_id": tool_response.tool_call_id, **tool_response.to_tool_result()}
            )
        all_responses = resume_responses or legacy_dicts
        return await _handle_tool_response(
            all_responses,
            frontend_tools,
            request_thread_id=thread_id,
            requested_input=payload.input,
            requested_workspace_path=payload.workspace_path,
            requested_output=payload.output,
            input_access_token=_input_access_token(payload.credentials),
            output_access_token=_output_access_token(payload.credentials),
        )

    bound_execution_id: Optional[uuid.UUID] = None
    execution_config: Optional[dict] = None
    input_binding = None
    mode: Optional[ExecutionMode] = None
    legacy_writable = False
    workspace_hint = payload.workspace_path
    in_place_hint = True if payload.in_place is None else bool(payload.in_place)
    source: Optional[str] = None

    try:
        if payload.input is not None:
            input_binding, mode, legacy_writable = resolve_initiate_input(
                input_binding=payload.input,
                folder=None,
                in_place=False,
                mode=payload.mode,
            )
            assert_agui_phase1_input(input_binding)
            execution_config = sanitize_execution_config(
                input_binding=input_binding,
                mode=mode or ExecutionMode.WORKFLOW,
                output_binding=payload.output,
                runtime_path=ensure_runtime(thread_id=thread_id),
                legacy_writable_in_place=legacy_writable,
            )
            source = provision_source(input_binding)
        elif payload.workspace_path:
            input_binding, legacy_writable = input_from_workspace_path(
                payload.workspace_path, in_place=in_place_hint
            )
            mode = payload.mode or (
                ExecutionMode.WORKING_COPY if legacy_writable else ExecutionMode.WORKFLOW
            )
        else:
            input_binding = None
            mode = payload.mode
            legacy_writable = False
    except BindingError as exc:
        return _binding_error_stream(exc)

    if input_binding is not None and execution_config is None:
        execution_config = sanitize_execution_config(
            input_binding=input_binding,
            mode=mode or ExecutionMode.WORKFLOW,
            output_binding=payload.output,
            runtime_path=ensure_runtime(thread_id=thread_id),
            legacy_writable_in_place=legacy_writable,
        )

    runtime_path = ensure_runtime(thread_id=thread_id)
    if execution_config is None:
        execution_config = _minimal_execution_config(
            runtime_path=runtime_path,
            mode=mode,
            output_binding=payload.output,
        )
    elif not execution_config.get("runtimePath"):
        execution_config = strip_secrets_from_mapping(
            {**execution_config, "runtimePath": runtime_path}
        )

    bound_execution_id = await _create_agui_execution(
        thread_id=thread_id,
        source=source,
        workspace_path=workspace_hint if payload.workspace_path and not payload.input else None,
        config=execution_config,
    )

    active_run = None
    run_pk: Optional[uuid.UUID] = None
    async with get_session() as session:
        try:
            active_run = await run_lifecycle_service.try_create_active_run(
                session,
                execution_id=bound_execution_id,
                run_id=run_id,
                thread_id=thread_id,
                origin="agui",
                channel="agui",
            )
        except RunClaimError as exc:
            await execution_state_service.update_execution(
                session,
                bound_execution_id,
                status=ExecutionStatus.FAILED,
                error_message=exc.message,
            )
            return _session_closed_stream(exc.code)
        if active_run is not None:
            run_pk = active_run.id
            await execution_state_service.update_execution(
                session, bound_execution_id, status=ExecutionStatus.RUNNING
            )

    if active_run is None:
        await _terminalize_failed_execution(
            bound_execution_id,
            provisioned=False,
            error=f"{_RUN_CONFLICT}: Another run is active for this thread",
        )
        return _run_conflict_stream()

    provisioned = False
    local_context: Optional[LocalToolContext] = None
    initial_messages: Optional[list] = None
    harness_manifest_summary: Optional[dict] = None
    try:
        execution_stub = SimpleNamespace(
            id=bound_execution_id,
            workspace_path=workspace_hint,
            source=source,
        )
        if input_binding is not None:
            ws = await workspace_manager.provision(
                bound_execution_id,
                source,
                in_place=should_provision_in_place(
                    input_binding, legacy_writable=legacy_writable
                ),
                input_access_token=_input_access_token(payload.credentials),
            )
            provisioned = not ws.in_place
            workspace_hint = select_relative_workspace(ws.path, input_binding.relative_path)
            in_place_hint = ws.in_place
            async with get_session() as session:
                await execution_state_service.update_execution(
                    session,
                    bound_execution_id,
                    workspace_path=workspace_hint,
                    config=execution_config,
                )
            execution_stub.workspace_path = workspace_hint
        elif payload.workspace_path:
            workspace_hint = await _ensure_workspace_for_agui_segment(
                bound_execution_id,
                execution_stub,
                execution_config,
                input_access_token=_input_access_token(payload.credentials),
            )
            in_place_hint = bool(execution_config.get("inPlace", in_place_hint))

        local_context = _resolve_local_context(
            workspace_hint,
            in_place_hint,
            thread_id=thread_id,
            mode=(mode or ExecutionMode.WORKFLOW).value,
            runtime_path=execution_config.get("runtimePath"),
            output_backend=segment_output_backend(
                execution_config,
                output_access_token=_output_access_token(payload.credentials),
            ),
        )
        if local_context is None and workspace_hint:
            local_context = _local_context_from_segment(
                execution_stub,
                execution_config,
                output_access_token=_output_access_token(payload.credentials),
                workspace_path=workspace_hint,
            )

        bound_workspace = local_context.workspace_path if local_context else workspace_hint
        bound_inplace = local_context.in_place if local_context else in_place_hint

        system_prompt, harness_manifest_summary = _build_fresh_system_prompt(
            bound_workspace,
            execution_config,
        )
        initial_messages = build_initial_messages(
            harness_prompt=system_prompt,
            agui_messages=payload.messages,
            legacy_request=None,
            contexts=payload.context,
            has_frontend_tools=bool(frontend_tools),
            frontend_tool_names=[tool.get("name", "") for tool in frontend_tools],
        )
        await _discard_stale_awaiting_and_cleanup_orphans(thread_id)
    except BindingError as exc:
        await _abort_fresh_run_prep(
            run_pk,
            thread_id,
            bound_execution_id,
            provisioned=provisioned,
            error=exc.message,
        )
        return _binding_error_stream(exc)
    except StorageError as exc:
        await _abort_fresh_run_prep(
            run_pk,
            thread_id,
            bound_execution_id,
            provisioned=provisioned,
            error=exc.message,
        )
        return _storage_error_stream(exc)
    except Exception:
        await _abort_fresh_run_prep(
            run_pk,
            thread_id,
            bound_execution_id,
            provisioned=provisioned,
            error="Input provisioning failed",
        )

        async def provision_error_stream():
            yield _serialize_event(RunErrorEvent(message="Input provisioning failed"))

        return StreamingResponse(provision_error_stream(), media_type="text/event-stream")

    bound_workspace = local_context.workspace_path if local_context else workspace_hint
    bound_inplace = local_context.in_place if local_context else in_place_hint

    logger.info(
        "Starting AG-UI run thread_id=%s run_id=%s workspace=%s execution_id=%s",
        thread_id,
        run_id,
        bound_workspace or "(none)",
        bound_execution_id,
    )

    def make_task(cancel_event=None):
        return tool_execution_hub.process_request(
            request="",
            initial_messages=initial_messages,
            model=payload.model or "gpt-3.5-turbo",
            max_tool_calls=payload.max_tool_calls,
            requested_tools=None,
            lite_llm_request_timeout_in_sec=payload.llm_request_timeout_in_sec,
            include_agui_tools=True,
            agui_context=AGUIRunContext(
                thread_id=thread_id,
                run_id=run_id,
                parent_run_id=parent_run_id,
            ),
            frontend_tools=frontend_tools,
            local_context=local_context,
            harness_manifest=harness_manifest_summary,
            cancel_event=cancel_event,
        )

    return _stream_run(
        make_task=make_task,
        thread_id=thread_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        execution_id=bound_execution_id,
        run_pk=run_pk,
        workspace_path=bound_workspace,
        in_place=bound_inplace,
        runtime_path=local_context.runtime_path if local_context else execution_config.get("runtimePath"),
    )


@router.post(
    "/threads/{thread_id}/close",
    response_model=AGUIThreadCloseResponse,
)
async def close_agui_thread(thread_id: str):
    async with get_session() as session:
        await run_lifecycle_service.ensure_thread_session(session, thread_id)

    result = await session_close_service.close_thread(thread_id)
    status_code = 200 if result.status == "closed" else 202
    body = AGUIThreadCloseResponse(
        threadId=thread_id,
        status=result.status,
        alreadyClosed=result.already_closed,
        discardedHolds=result.discarded_holds,
        runtimeDeleted=result.runtime_deleted,
        workspaceDeleted=result.workspace_deleted,
        workspaceDeletedCount=result.workspace_deleted_count,
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(by_alias=True))


@router.post(
    "/threads/{thread_id}/abandon",
    response_model=AGUIThreadAbandonResponse,
)
async def abandon_agui_thread(thread_id: str) -> AGUIThreadAbandonResponse:
    """Explicit recovery when a client never received an interrupt id."""
    async with get_session() as session:
        discarded = await execution_state_service.abandon_active_holds_for_thread(
            session, thread_id
        )
    if discarded:
        logger.info(
            "AG-UI thread abandon discarded %d active hold(s) for thread_id=%s",
            discarded,
            thread_id,
        )
    return AGUIThreadAbandonResponse(
        threadId=thread_id,
        discarded=discarded,
        status="abandoned",
    )


@router.get("/events")
async def stream_agui_events():
    """Global SSE stream for AG-UI tool call events."""
    queue = await agui_event_service.subscribe()

    async def event_stream():
        try:
            while True:
                event = await queue.get()
                yield _serialize_event(event)
        finally:
            await agui_event_service.unsubscribe(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
