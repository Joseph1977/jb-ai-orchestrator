# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Phase 3 AG-UI controller wiring tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ag_ui.core.types import UserMessage

from app.controllers.ag_ui_controller import (
    AGUIRunRequest,
    _build_fresh_system_prompt,
    abandon_agui_thread,
    close_agui_thread,
    run_agui_session,
)
from app.models.bindings import (
    INPUT_BINDING_REPLACEMENT_NOT_ALLOWED,
    INPUT_WORKSPACE_MISSING,
    SESSION_CLOSED,
    SESSION_CLOSING,
)
from app.models.execution_models import ExecutionStatus, LLMStateStatus
from app.services.binding_contract import BindingError
from app.services.binding_runtime import RUN_BINDING_END, RUN_BINDING_START
from app.services.run_lifecycle import RUN_STATUS_FAILED
from app.services.session_close_service import (
    CANCEL_REASON_CLOSE,
    CloseResult,
    RunCancelHandle,
)


FRONTEND_TOOLS = [
    {
        "name": "AskUser",
        "description": "Ask the user",
        "parameters": {"type": "object", "properties": {}},
    }
]


def _lifecycle_patches(*, ensure=AsyncMock(), create_run=None):
    create_run = create_run or AsyncMock(return_value=_fake_run())
    return [
        patch("app.controllers.ag_ui_controller.run_lifecycle_service.ensure_thread_session", ensure),
        patch("app.controllers.ag_ui_controller.run_lifecycle_service.try_create_active_run", create_run),
        patch("app.controllers.ag_ui_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)),
        patch("app.controllers.ag_ui_controller.run_lifecycle_service.get_thread_session", AsyncMock(return_value=None)),
        patch("app.controllers.ag_ui_controller.run_lifecycle_service.associate_execution_origin", AsyncMock()),
        patch("app.controllers.ag_ui_controller.session_close_service.manage_run", _noop_manage_run),
        patch("app.controllers.ag_ui_controller._finalize_run_segment", AsyncMock()),
        patch("app.controllers.ag_ui_controller._discard_stale_awaiting_and_cleanup_orphans", AsyncMock(return_value=0)),
    ]


def _resume_entry_patches(state, execution, *, hub=None, create_run=None, request_thread_id="thread-db"):
    patches = _lifecycle_patches(create_run=create_run)
    patches.extend([
        patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub or AsyncMock()),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.get_resume_hold_by_tool_call_id",
            AsyncMock(return_value=state),
        ),
        patch("app.controllers.ag_ui_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)),
        patch("app.controllers.ag_ui_controller.execution_state_service.recover_stale_pending_claims", AsyncMock(return_value=0)),
        patch("app.controllers.ag_ui_controller.execution_state_service.get_latest_pending_hold_for_thread", AsyncMock(return_value=None)),
        patch("app.controllers.ag_ui_controller.execution_state_service.get_latest_awaiting_state_for_thread", AsyncMock(return_value=None)),
        patch("app.controllers.ag_ui_controller._prepare_thread_claims", AsyncMock(return_value=(0, False))),
        patch("app.controllers.ag_ui_controller.agui_service.refresh_frontend_tools"),
    ])
    return patches


def _fake_run(run_pk=None):
    return SimpleNamespace(id=run_pk or uuid.uuid4())


@contextmanager
def _apply_patches(patches):
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


@asynccontextmanager
async def _noop_manage_run(**_kwargs):
    yield RunCancelHandle()


class _FakeSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return None


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


@pytest.mark.asyncio
async def test_bind_only_ensures_thread_session_no_execution():
    ensure = AsyncMock()
    create_execution = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    payload = AGUIRunRequest.model_validate(
        {"threadId": "thread-bind", "frontendTools": FRONTEND_TOOLS}
    )

    patches = _lifecycle_patches(ensure=ensure)
    with patch("app.controllers.ag_ui_controller.get_session", fake_get_session), \
         patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=object()), \
         patch("app.controllers.ag_ui_controller.execution_state_service.create_execution", create_execution), \
         patch("app.controllers.ag_ui_controller.agui_service.refresh_frontend_tools"):
        for p in patches:
            p.start()
        try:
            resp = await run_agui_session(payload)
            events = await _read_sse_events(resp)
        finally:
            for p in patches:
                p.stop()

    ensure.assert_awaited_once()
    create_execution.assert_not_called()
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["result"]["toolCount"] == 1


@pytest.mark.asyncio
async def test_bind_only_rejects_closed_thread():
    closed_session = SimpleNamespace(
        closed_at=datetime.now(timezone.utc),
        close_requested_at=None,
    )

    async def _get_thread_session(session, key):
        return closed_session

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    payload = AGUIRunRequest.model_validate({"threadId": "thread-closed"})

    with patch("app.controllers.ag_ui_controller.get_session", fake_get_session), \
         patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=object()), \
         patch("app.controllers.ag_ui_controller.run_lifecycle_service.ensure_thread_session", AsyncMock()), \
         patch("app.controllers.ag_ui_controller.run_lifecycle_service.get_thread_session", _get_thread_session), \
         patch("app.controllers.ag_ui_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.ag_ui_controller.agui_service.refresh_frontend_tools"):
        resp = await run_agui_session(payload)
        events = await _read_sse_events(resp)

    assert events[0]["type"] == "RUN_ERROR"
    assert SESSION_CLOSED in events[0]["message"]


@pytest.mark.asyncio
async def test_fresh_no_input_creates_eager_execution_and_completes():
    exec_id = uuid.uuid4()
    hub = AsyncMock()
    hub.process_request = AsyncMock(return_value={
        "success": True,
        "response": "hello",
        "tool_calls_info": [],
    })
    create_calls: list = []

    async def _create_execution(**kwargs):
        create_calls.append(kwargs)
        return exec_id

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    payload = AGUIRunRequest.model_validate(
        {
            "threadId": "thread-fresh",
            "messages": [UserMessage(id="u1", content="hi")],
        }
    )

    patches = _lifecycle_patches()
    with patch("app.controllers.ag_ui_controller.get_session", fake_get_session), \
         patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub), \
         patch("app.controllers.ag_ui_controller._create_agui_execution", AsyncMock(side_effect=_create_execution)), \
         patch("app.controllers.ag_ui_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.ag_ui_controller._prepare_thread_claims", AsyncMock(return_value=(0, False))), \
         patch("app.controllers.ag_ui_controller._build_fresh_system_prompt", return_value=("", None)), \
         patch("app.controllers.ag_ui_controller._finalize", AsyncMock()), \
         patch("app.controllers.ag_ui_controller.agui_service.refresh_frontend_tools"):
        for p in patches:
            p.start()
        try:
            resp = await run_agui_session(payload)
            events = await _read_sse_events(resp)
        finally:
            for p in patches:
                p.stop()

    assert create_calls
    assert create_calls[0]["thread_id"] == "thread-fresh"
    assert events[-1]["type"] == "RUN_FINISHED"
    hub.process_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_fresh_builds_tagged_binding_block_once():
    config = {"mode": "workflow", "runtimePath": "/tmp/runtime", "output": None}
    with patch("app.services.harness.collect_manifest") as harness_collect, \
         patch("app.services.harness.render_system_prompt", return_value="Harness catalog"):
        harness_collect.return_value = SimpleNamespace(
            orchestration_type="generic",
            summary=MagicMock(return_value={"type": "generic"}),
            skills=[],
            commands=[],
            rules=[],
            agents=[],
            notes=[],
        )
        prompt, summary = _build_fresh_system_prompt("/tmp/ws", config)

    assert prompt.count(RUN_BINDING_START) == 1
    assert prompt.count(RUN_BINDING_END) == 1
    assert "Harness catalog" in prompt
    assert summary == {"type": "generic"}


@pytest.mark.asyncio
async def test_resume_uses_db_thread_and_run_ids():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    run_pk = uuid.uuid4()
    state_payload = {
        "request": "prompt",
        "model": "gpt-4o",
        "pending_tools": [{"tool_call_id": "call_a", "function_name": "AskA", "source": "AGUI"}],
        "messages": [{"role": "system", "content": "Persisted harness"}],
    }
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        config={"mode": "workflow", "runtimePath": "/tmp/runtime"},
        source=None,
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
        "tool_calls_info": [],
    })
    create_run = AsyncMock(return_value=_fake_run(run_pk))

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    patches = _resume_entry_patches(state, execution, hub=hub, create_run=create_run)
    patches.extend([
        patch("app.controllers.ag_ui_controller.get_session", fake_get_session),
        patch("app.controllers.ag_ui_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)),
        patch("app.controllers.ag_ui_controller.execution_state_service.update_execution", AsyncMock()),
        patch("app.controllers.ag_ui_controller._ensure_workspace_for_agui_segment", AsyncMock(return_value="/tmp/ws")),
        patch("app.controllers.ag_ui_controller._finalize", AsyncMock()),
        patch("app.controllers.ag_ui_controller._complete_claimed_state", AsyncMock(return_value=True)),
    ])
    for p in patches:
        p.start()
    try:
        payload = AGUIRunRequest.model_validate({
            "threadId": "thread-from-db",
            "runId": "payload-run",
            "state": {"toolCallId": "call_a", "result": {"answer": "a"}},
        })
        resp = await run_agui_session(payload)
        await _read_sse_events(resp)
    finally:
        for p in patches:
            p.stop()

    create_run.assert_awaited_once()
    assert create_run.await_args.kwargs["thread_id"] == "thread-from-db"
    assert create_run.await_args.kwargs["run_id"] == "run-from-db"
    ctx = hub.process_request.await_args.kwargs["agui_context"]
    assert ctx.thread_id == "thread-from-db"
    assert ctx.run_id == "run-from-db"


@pytest.mark.asyncio
async def test_resume_rejects_input_retarget():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    stored_input = {
        "type": "shared_folder",
        "uri": "/work/a",
        "relativePath": ".",
    }
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/work/a",
        config={"mode": "workflow", "input": stored_input},
        source=None,
    )
    state = SimpleNamespace(
        id=state_id,
        execution_id=exec_id,
        state_payload={"pending_tools": [{"tool_call_id": "call_a"}]},
        status=LLMStateStatus.AWAITING_RESPONSE,
        thread_id="thread-db",
        run_id="run-db",
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    from app.models.bindings import LocationBinding, LocationType

    other_input = LocationBinding(
        type=LocationType.SHARED_FOLDER,
        uri="/work/b",
        relativePath=".",
    )

    patches = _resume_entry_patches(state, execution)
    patches.extend([
        patch("app.controllers.ag_ui_controller.get_session", fake_get_session),
    ])
    for p in patches:
        p.start()
    try:
        payload = AGUIRunRequest.model_validate({
            "threadId": "thread-db",
            "input": other_input.model_dump(by_alias=True),
            "state": {"toolCallId": "call_a", "result": {"answer": "a"}},
        })
        resp = await run_agui_session(payload)
        events = await _read_sse_events(resp)
    finally:
        for p in patches:
            p.stop()

    assert events[0]["type"] == "RUN_ERROR"
    assert INPUT_BINDING_REPLACEMENT_NOT_ALLOWED in events[0]["message"]


@pytest.mark.asyncio
async def test_resume_does_not_collect_manifest():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        config={"mode": "workflow", "runtimePath": "/tmp/runtime"},
        source=None,
    )
    state = SimpleNamespace(
        id=state_id,
        execution_id=exec_id,
        state_payload={
            "pending_tools": [{"tool_call_id": "call_a"}],
            "messages": [{"role": "system", "content": "Persisted"}],
        },
        status=LLMStateStatus.AWAITING_RESPONSE,
        thread_id="thread-db",
        run_id="run-db",
    )
    hub = AsyncMock()
    hub.process_request = AsyncMock(return_value={"success": True, "response": "ok", "tool_calls_info": []})

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.services.harness.collect_manifest") as collect_mock, \
         patch("app.controllers.ag_ui_controller.get_session", fake_get_session):
        patches = _resume_entry_patches(state, execution, hub=hub)
        patches.extend([
            patch("app.controllers.ag_ui_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)),
            patch("app.controllers.ag_ui_controller.execution_state_service.update_execution", AsyncMock()),
            patch("app.controllers.ag_ui_controller._ensure_workspace_for_agui_segment", AsyncMock(return_value="/tmp/ws")),
            patch("app.controllers.ag_ui_controller._finalize", AsyncMock()),
            patch("app.controllers.ag_ui_controller._complete_claimed_state", AsyncMock(return_value=True)),
        ])
        for p in patches:
            p.start()
        try:
            payload = AGUIRunRequest.model_validate({
                "threadId": "thread-db",
                "state": {"toolCallId": "call_a", "result": {"answer": "a"}},
            })
            resp = await run_agui_session(payload)
            await _read_sse_events(resp)
        finally:
            for p in patches:
                p.stop()

    collect_mock.assert_not_called()
    resume_state = hub.process_request.await_args.kwargs["resume_state"]
    assert resume_state["messages"][0]["content"].count(RUN_BINDING_START) == 1


@pytest.mark.asyncio
async def test_resume_existing_workspace_skips_input_token():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        config={
            "mode": "workflow",
            "runtimePath": "/tmp/runtime",
            "input": {"type": "shared_folder", "uri": "/tmp/ws", "relativePath": "."},
        },
        source=None,
    )
    state = SimpleNamespace(
        id=state_id,
        execution_id=exec_id,
        state_payload={"pending_tools": [{"tool_call_id": "call_a"}], "messages": []},
        status=LLMStateStatus.AWAITING_RESPONSE,
        thread_id="thread-db",
        run_id="run-db",
    )
    ensure = AsyncMock(return_value="/tmp/ws")
    hub = AsyncMock()
    hub.process_request = AsyncMock(return_value={"success": True, "response": "ok", "tool_calls_info": []})

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    patches = _resume_entry_patches(state, execution, hub=hub)
    patches.extend([
        patch("app.controllers.ag_ui_controller.get_session", fake_get_session),
        patch("app.controllers.ag_ui_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)),
        patch("app.controllers.ag_ui_controller.execution_state_service.update_execution", AsyncMock()),
        patch("app.controllers.ag_ui_controller._ensure_workspace_for_agui_segment", ensure),
        patch("app.controllers.ag_ui_controller._complete_claimed_state", AsyncMock(return_value=True)),
        patch("app.controllers.ag_ui_controller._finalize", AsyncMock()),
        patch("app.controllers.ag_ui_controller._finalize_run_segment", AsyncMock()),
        patch(
            "app.controllers.ag_ui_controller.agui_event_service.subscribe",
            AsyncMock(return_value=asyncio.Queue()),
        ),
        patch("app.controllers.ag_ui_controller.agui_event_service.unsubscribe", AsyncMock()),
    ])
    for p in patches:
        p.start()
    try:
        payload = AGUIRunRequest.model_validate({
            "threadId": "thread-db",
            "state": {"toolCallId": "call_a", "result": {"answer": "a"}},
        })
        resp = await run_agui_session(payload)
        await _read_sse_events(resp)
    finally:
        for p in patches:
            p.stop()

    assert ensure.await_args.kwargs["input_access_token"] is None


@pytest.mark.asyncio
async def test_resume_prep_failure_restores_claim_and_finishes_run():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    run_pk = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        config={"mode": "workflow"},
        source=None,
    )
    state = SimpleNamespace(
        id=state_id,
        execution_id=exec_id,
        state_payload={"pending_tools": [{"tool_call_id": "call_a"}], "messages": []},
        status=LLMStateStatus.AWAITING_RESPONSE,
        thread_id="thread-db",
        run_id="run-db",
    )
    abort = AsyncMock()
    restore_state = AsyncMock(return_value=True)
    restore_execution = AsyncMock()
    finalize_segment = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    create_run = AsyncMock(return_value=_fake_run(run_pk))
    patches = _resume_entry_patches(state, execution, create_run=create_run)
    patches.extend([
        patch("app.controllers.ag_ui_controller.get_session", fake_get_session),
        patch("app.controllers.ag_ui_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)),
        patch("app.controllers.ag_ui_controller.execution_state_service.update_execution", AsyncMock()),
        patch(
            "app.controllers.ag_ui_controller._ensure_workspace_for_agui_segment",
            AsyncMock(side_effect=BindingError(INPUT_WORKSPACE_MISSING, "missing")),
        ),
        patch("app.controllers.ag_ui_controller._restore_claimed_state", restore_state),
        patch("app.controllers.ag_ui_controller._restore_execution_awaiting", restore_execution),
        patch("app.controllers.ag_ui_controller._finalize", AsyncMock()),
        patch("app.controllers.ag_ui_controller._finalize_run_segment", finalize_segment),
    ])
    for p in patches:
        p.start()
    try:
        payload = AGUIRunRequest.model_validate({
            "threadId": "thread-db",
            "state": {"toolCallId": "call_a", "result": {"answer": "a"}},
        })
        resp = await run_agui_session(payload)
        events = await _read_sse_events(resp)
    finally:
        for p in patches:
            p.stop()

    restore_state.assert_awaited_once()
    restore_execution.assert_awaited_once()
    finalize_segment.assert_awaited_once()
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_ERROR"
    assert INPUT_WORKSPACE_MISSING in events[-1]["message"]


@pytest.mark.asyncio
async def test_fresh_orphan_cleanup_only_when_discarded(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.WORKSPACES_ROOT", str(tmp_path))
    monkeypatch.setattr("app.services.runtime_paths.Config.WORKSPACES_ROOT", str(tmp_path))
    from app.controllers.ag_ui_controller import _discard_stale_awaiting_and_cleanup_orphans
    from app.services.runtime_paths import ensure_runtime

    thread_id = "thread-orphan"
    runtime = ensure_runtime(thread_id=thread_id)
    offload = Path(runtime) / "offload"
    offload.mkdir(parents=True, exist_ok=True)
    (offload / "orphan.txt").write_text("drop", encoding="utf-8")

    class _EmptyScalars:
        def all(self):
            return []

    class _EmptyResult:
        def scalars(self):
            return _EmptyScalars()

    class _FakeSession:
        async def execute(self, _stmt):
            return _EmptyResult()

    @asynccontextmanager
    async def fake_get_session():
        yield _FakeSession()

    with patch("app.controllers.ag_ui_controller.get_session", fake_get_session), patch(
        "app.controllers.ag_ui_controller.execution_state_service.discard_awaiting_states_for_thread",
        AsyncMock(return_value=0),
    ) as discard_mock, patch(
        "app.controllers.ag_ui_controller.cleanup_unreferenced_offloads",
    ) as cleanup_mock:
        count = await _discard_stale_awaiting_and_cleanup_orphans(thread_id)

    assert count == 0
    cleanup_mock.assert_not_called()
    discard_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_thread_maps_status_codes():
    closed = CloseResult(
        status="closed",
        already_closed=False,
        discarded_holds=1,
        runtime_deleted=True,
        workspace_deleted=False,
        workspace_deleted_count=0,
    )
    closing = CloseResult(
        status="closing",
        already_closed=False,
        discarded_holds=0,
        runtime_deleted=False,
        workspace_deleted=False,
        workspace_deleted_count=0,
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.ag_ui_controller.get_session", fake_get_session), \
         patch("app.controllers.ag_ui_controller.run_lifecycle_service.ensure_thread_session", AsyncMock()), \
         patch("app.controllers.ag_ui_controller.session_close_service.close_thread", AsyncMock(return_value=closed)):
        resp = await close_agui_thread("thread-close")
    assert resp.status_code == 200
    body = json.loads(resp.body.decode())
    assert body["threadId"] == "thread-close"
    assert body["alreadyClosed"] is False
    assert body["discardedHolds"] == 1

    with patch("app.controllers.ag_ui_controller.get_session", fake_get_session), \
         patch("app.controllers.ag_ui_controller.run_lifecycle_service.ensure_thread_session", AsyncMock()), \
         patch("app.controllers.ag_ui_controller.session_close_service.close_thread", AsyncMock(return_value=closing)):
        resp = await close_agui_thread("thread-close")
    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_close_is_idempotent():
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

    with patch("app.controllers.ag_ui_controller.get_session", fake_get_session), \
         patch("app.controllers.ag_ui_controller.run_lifecycle_service.ensure_thread_session", AsyncMock()), \
         patch("app.controllers.ag_ui_controller.session_close_service.close_thread", AsyncMock(return_value=result)):
        resp = await close_agui_thread("thread-close")

    assert resp.status_code == 200
    assert json.loads(resp.body.decode())["alreadyClosed"] is True


@pytest.mark.asyncio
async def test_abandon_does_not_invoke_close_cleanup():
    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.ag_ui_controller.get_session", fake_get_session), \
         patch(
             "app.controllers.ag_ui_controller.execution_state_service.abandon_active_holds_for_thread",
             AsyncMock(return_value=2),
         ), \
         patch("app.controllers.ag_ui_controller.session_close_service.close_thread") as close_mock:
        resp = await abandon_agui_thread("thread-abandon")

    close_mock.assert_not_called()
    assert resp.discarded == 2


@pytest.mark.asyncio
async def test_cancellation_reconciles_without_restoring_close_discarded_hold():
    exec_id = uuid.uuid4()
    run_pk = uuid.uuid4()
    state_id = uuid.uuid4()
    restore = AsyncMock(return_value=True)
    finalize_segment = AsyncMock()
    cancel_event = asyncio.Event()
    cancel_event.set()

    @asynccontextmanager
    async def managing_run(**kwargs):
        yield RunCancelHandle(event=cancel_event, reason=CANCEL_REASON_CLOSE)

    async def segment_runner(_cancel_event):
        raise asyncio.CancelledError()

    with patch("app.controllers.ag_ui_controller.agui_event_service.subscribe", AsyncMock(return_value=asyncio.Queue())), \
         patch("app.controllers.ag_ui_controller.agui_event_service.unsubscribe", AsyncMock()), \
         patch("app.controllers.ag_ui_controller.session_close_service.manage_run", managing_run), \
         patch("app.controllers.ag_ui_controller._restore_claimed_state", restore), \
         patch("app.controllers.ag_ui_controller._finalize_run_segment", finalize_segment), \
         patch("app.controllers.ag_ui_controller._finalize", AsyncMock()):
        from app.controllers.ag_ui_controller import _stream_run

        resp = _stream_run(
            segment_runner=segment_runner,
            thread_id="thread-cancel",
            run_id="run-cancel",
            execution_id=exec_id,
            run_pk=run_pk,
            claimed_state_id=state_id,
        )
        events = await _read_sse_events(resp)

    restore.assert_not_called()
    finalize_segment.assert_awaited_once()
    assert SESSION_CLOSING in events[-1]["message"]


@pytest.mark.asyncio
async def test_closed_thread_rejected_before_fresh_execution():
    closed_session = SimpleNamespace(
        closed_at=datetime.now(timezone.utc),
        close_requested_at=None,
    )

    async def _get_thread_session(session, key):
        return closed_session

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    create_execution = AsyncMock()

    with patch("app.controllers.ag_ui_controller.get_session", fake_get_session), \
         patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=AsyncMock()), \
         patch("app.controllers.ag_ui_controller.run_lifecycle_service.ensure_thread_session", AsyncMock()), \
         patch("app.controllers.ag_ui_controller.run_lifecycle_service.get_thread_session", _get_thread_session), \
         patch("app.controllers.ag_ui_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.ag_ui_controller._create_agui_execution", create_execution), \
         patch("app.controllers.ag_ui_controller._prepare_thread_claims", AsyncMock(return_value=(0, False))), \
         patch("app.controllers.ag_ui_controller.agui_service.refresh_frontend_tools"):
        payload = AGUIRunRequest.model_validate(
            {
                "threadId": "thread-closed-fresh",
                "messages": [UserMessage(id="u1", content="hi")],
            }
        )
        resp = await run_agui_session(payload)
        events = await _read_sse_events(resp)

    create_execution.assert_not_called()
    assert events[0]["type"] == "RUN_ERROR"
    assert SESSION_CLOSED in events[0]["message"]


@pytest.mark.asyncio
async def test_resume_rejects_cross_thread_hold():
    state = SimpleNamespace(
        id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        state_payload={"pending_tools": [{"tool_call_id": "call_a"}]},
        status=LLMStateStatus.AWAITING_RESPONSE,
        thread_id="other-thread",
        run_id="run-other",
    )
    execution = SimpleNamespace(
        id=state.execution_id,
        workspace_path="/tmp/ws",
        config={"mode": "workflow"},
        source=None,
    )

    hold = AsyncMock(return_value=state)

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    patches = _resume_entry_patches(state, execution)
    patches.append(
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.get_resume_hold_by_tool_call_id",
            hold,
        )
    )
    patches.append(patch("app.controllers.ag_ui_controller.get_session", fake_get_session))
    for p in patches:
        p.start()
    try:
        payload = AGUIRunRequest.model_validate({
            "threadId": "request-thread",
            "state": {"toolCallId": "call_a", "result": {"answer": "a"}},
        })
        resp = await run_agui_session(payload)
        events = await _read_sse_events(resp)
    finally:
        for p in patches:
            p.stop()

    hold.assert_awaited()
    assert hold.await_args.kwargs.get("thread_id") == "request-thread"
    assert events[0]["type"] == "RUN_ERROR"
    assert "request-thread" in events[0]["message"]


@pytest.mark.asyncio
async def test_resume_rejects_input_when_stored_null():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path=None,
        config={"mode": "workflow"},
        source=None,
    )
    state = SimpleNamespace(
        id=state_id,
        execution_id=exec_id,
        state_payload={"pending_tools": [{"tool_call_id": "call_a"}]},
        status=LLMStateStatus.AWAITING_RESPONSE,
        thread_id="thread-db",
        run_id="run-db",
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    from app.models.bindings import LocationBinding, LocationType

    other_input = LocationBinding(
        type=LocationType.SHARED_FOLDER,
        uri="/work/b",
        relativePath=".",
    )

    patches = _resume_entry_patches(state, execution)
    patches.append(patch("app.controllers.ag_ui_controller.get_session", fake_get_session))
    for p in patches:
        p.start()
    try:
        payload = AGUIRunRequest.model_validate({
            "threadId": "thread-db",
            "input": other_input.model_dump(by_alias=True),
            "state": {"toolCallId": "call_a", "result": {"answer": "a"}},
        })
        resp = await run_agui_session(payload)
        events = await _read_sse_events(resp)
    finally:
        for p in patches:
            p.stop()

    assert events[0]["type"] == "RUN_ERROR"
    assert INPUT_BINDING_REPLACEMENT_NOT_ALLOWED in events[0]["message"]


@pytest.mark.asyncio
async def test_claim_failure_terminalizes_execution_without_cleanup():
    exec_id = uuid.uuid4()
    terminalize = AsyncMock()
    cleanup = MagicMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    patches = _lifecycle_patches(create_run=AsyncMock(return_value=None))
    with patch("app.controllers.ag_ui_controller.get_session", fake_get_session), \
         patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=AsyncMock()), \
         patch("app.controllers.ag_ui_controller._create_agui_execution", AsyncMock(return_value=exec_id)), \
         patch("app.controllers.ag_ui_controller._terminalize_failed_execution", terminalize), \
         patch("app.controllers.ag_ui_controller.workspace_manager.cleanup", cleanup), \
         patch("app.controllers.ag_ui_controller._prepare_thread_claims", AsyncMock(return_value=(0, False))), \
         patch("app.controllers.ag_ui_controller.agui_service.refresh_frontend_tools"):
        for p in patches:
            p.start()
        try:
            payload = AGUIRunRequest.model_validate(
                {"threadId": "thread-conflict", "messages": [UserMessage(id="u1", content="hi")]}
            )
            resp = await run_agui_session(payload)
            events = await _read_sse_events(resp)
        finally:
            for p in patches:
                p.stop()

    terminalize.assert_awaited_once()
    assert terminalize.await_args.kwargs["provisioned"] is False
    cleanup.assert_not_called()
    assert "RUN_CONFLICT" in events[0]["message"]


@pytest.mark.asyncio
async def test_claim_failure_terminalizes_after_claim_session_closed():
    exec_id = uuid.uuid4()
    open_sessions = 0
    terminalize = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        nonlocal open_sessions
        open_sessions += 1
        yield object()
        open_sessions -= 1

    async def _terminalize(*_args, **_kwargs):
        assert open_sessions == 0, "terminalize must not run inside an open DB session"
        await terminalize(*_args, **_kwargs)

    patches = _lifecycle_patches(create_run=AsyncMock(return_value=None))
    with patch("app.controllers.ag_ui_controller.get_session", fake_get_session), \
         patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=AsyncMock()), \
         patch("app.controllers.ag_ui_controller._create_agui_execution", AsyncMock(return_value=exec_id)), \
         patch("app.controllers.ag_ui_controller._terminalize_failed_execution", _terminalize), \
         patch("app.controllers.ag_ui_controller._prepare_thread_claims", AsyncMock(return_value=(0, False))), \
         patch("app.controllers.ag_ui_controller.agui_service.refresh_frontend_tools"):
        for p in patches:
            p.start()
        try:
            payload = AGUIRunRequest.model_validate(
                {"threadId": "thread-deadlock", "messages": [UserMessage(id="u1", content="hi")]}
            )
            resp = await run_agui_session(payload)
            await _read_sse_events(resp)
        finally:
            for p in patches:
                p.stop()

    terminalize.assert_awaited_once()


@pytest.mark.asyncio
async def test_prep_failure_cleans_provisioned_sandbox_and_finishes_run():
    exec_id = uuid.uuid4()
    run_pk = uuid.uuid4()
    finalize = AsyncMock()
    finalize_segment = AsyncMock()
    cleanup = MagicMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    binding = MagicMock()
    binding.type.value = "shared_folder"
    binding.relative_path = "."

    patches = _lifecycle_patches(create_run=AsyncMock(return_value=_fake_run(run_pk)))
    patches.extend([
        patch("app.controllers.ag_ui_controller.get_session", fake_get_session),
        patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=AsyncMock()),
        patch("app.controllers.ag_ui_controller._create_agui_execution", AsyncMock(return_value=exec_id)),
        patch("app.controllers.ag_ui_controller.execution_state_service.update_execution", AsyncMock()),
        patch("app.controllers.ag_ui_controller.resolve_initiate_input", return_value=(binding, MagicMock(value="workflow"), False)),
        patch("app.controllers.ag_ui_controller.assert_agui_phase1_input"),
        patch("app.controllers.ag_ui_controller.sanitize_execution_config", return_value={"mode": "workflow"}),
        patch("app.controllers.ag_ui_controller.provision_source", return_value="/src"),
        patch("app.controllers.ag_ui_controller.workspace_manager.provision", AsyncMock(return_value=SimpleNamespace(path="/copy", in_place=False))),
        patch("app.controllers.ag_ui_controller.select_relative_workspace", return_value="/copy/ws"),
        patch("app.controllers.ag_ui_controller.should_provision_in_place", return_value=False),
        patch(
            "app.controllers.ag_ui_controller._build_fresh_system_prompt",
            side_effect=BindingError("RUN_BINDING_AMBIGUOUS", "ambiguous"),
        ),
        patch("app.controllers.ag_ui_controller._finalize", finalize),
        patch("app.controllers.ag_ui_controller._finalize_run_segment", finalize_segment),
        patch("app.controllers.ag_ui_controller.workspace_manager.cleanup", cleanup),
        patch("app.controllers.ag_ui_controller._prepare_thread_claims", AsyncMock(return_value=(0, False))),
        patch("app.controllers.ag_ui_controller.agui_service.refresh_frontend_tools"),
    ])

    with _apply_patches(patches):
        payload = AGUIRunRequest.model_validate({
            "threadId": "thread-prep-fail",
            "input": {"type": "shared_folder", "uri": "/src"},
            "messages": [UserMessage(id="u1", content="go")],
        })
        resp = await run_agui_session(payload)
        await _read_sse_events(resp)

    cleanup.assert_called_once_with(exec_id)
    finalize.assert_awaited_once()
    finalize_segment.assert_awaited_once()


@pytest.mark.asyncio
async def test_prep_failure_inplace_does_not_mark_provisioned_for_cleanup():
    exec_id = uuid.uuid4()
    run_pk = uuid.uuid4()
    finalize = AsyncMock()
    cleanup = MagicMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    binding = MagicMock()
    binding.type.value = "shared_folder"
    binding.relative_path = "."

    patches = _lifecycle_patches(create_run=AsyncMock(return_value=_fake_run(run_pk)))
    patches.extend([
        patch("app.controllers.ag_ui_controller.get_session", fake_get_session),
        patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=AsyncMock()),
        patch("app.controllers.ag_ui_controller._create_agui_execution", AsyncMock(return_value=exec_id)),
        patch("app.controllers.ag_ui_controller.execution_state_service.update_execution", AsyncMock()),
        patch("app.controllers.ag_ui_controller.resolve_initiate_input", return_value=(binding, MagicMock(value="workflow"), False)),
        patch("app.controllers.ag_ui_controller.assert_agui_phase1_input"),
        patch("app.controllers.ag_ui_controller.sanitize_execution_config", return_value={"mode": "workflow"}),
        patch("app.controllers.ag_ui_controller.provision_source", return_value="/src"),
        patch(
            "app.controllers.ag_ui_controller.workspace_manager.provision",
            AsyncMock(return_value=SimpleNamespace(path="/src", in_place=True)),
        ),
        patch("app.controllers.ag_ui_controller.select_relative_workspace", return_value="/src"),
        patch("app.controllers.ag_ui_controller.should_provision_in_place", return_value=True),
        patch(
            "app.controllers.ag_ui_controller._build_fresh_system_prompt",
            side_effect=BindingError("RUN_BINDING_AMBIGUOUS", "ambiguous"),
        ),
        patch("app.controllers.ag_ui_controller._finalize", finalize),
        patch("app.controllers.ag_ui_controller._finalize_run_segment", AsyncMock()),
        patch("app.controllers.ag_ui_controller.workspace_manager.cleanup", cleanup),
        patch("app.controllers.ag_ui_controller._prepare_thread_claims", AsyncMock(return_value=(0, False))),
        patch("app.controllers.ag_ui_controller.agui_service.refresh_frontend_tools"),
    ])

    with _apply_patches(patches):
        payload = AGUIRunRequest.model_validate({
            "threadId": "thread-inplace-fail",
            "input": {"type": "shared_folder", "uri": "/src"},
            "messages": [UserMessage(id="u1", content="go")],
        })
        resp = await run_agui_session(payload)
        await _read_sse_events(resp)

    finalize.assert_awaited_once()
    cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_fresh_input_only_winner_provisions():
    exec_winner = uuid.uuid4()
    exec_loser = uuid.uuid4()
    provision = AsyncMock(return_value=SimpleNamespace(path="/copy", in_place=False))
    create_calls: list[uuid.UUID] = []
    terminalize = AsyncMock()

    async def _create_execution(**kwargs):
        eid = exec_winner if len(create_calls) == 0 else exec_loser
        create_calls.append(eid)
        return eid

    claim_results = iter([_fake_run(), None])

    async def _try_create_active_run(session, **kwargs):
        return next(claim_results)

    binding = MagicMock()
    binding.type.value = "shared_folder"
    binding.relative_path = "."

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    hub = SimpleNamespace(
        process_request=AsyncMock(
            return_value={"success": True, "response": "ok", "tool_calls_info": []}
        )
    )

    shared_patches = [
        patch("app.controllers.ag_ui_controller.get_session", fake_get_session),
        patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=hub),
        patch("app.controllers.ag_ui_controller._create_agui_execution", AsyncMock(side_effect=_create_execution)),
        patch("app.controllers.ag_ui_controller.run_lifecycle_service.try_create_active_run", _try_create_active_run),
        patch("app.controllers.ag_ui_controller.run_lifecycle_service.ensure_thread_session", AsyncMock()),
        patch("app.controllers.ag_ui_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)),
        patch("app.controllers.ag_ui_controller.run_lifecycle_service.get_thread_session", AsyncMock(return_value=None)),
        patch("app.controllers.ag_ui_controller.run_lifecycle_service.associate_execution_origin", AsyncMock()),
        patch("app.controllers.ag_ui_controller.execution_state_service.update_execution", AsyncMock()),
        patch("app.controllers.ag_ui_controller.resolve_initiate_input", return_value=(binding, MagicMock(value="workflow"), False)),
        patch("app.controllers.ag_ui_controller.assert_agui_phase1_input"),
        patch("app.controllers.ag_ui_controller.sanitize_execution_config", return_value={"mode": "workflow"}),
        patch("app.controllers.ag_ui_controller.provision_source", return_value="/src"),
        patch("app.controllers.ag_ui_controller.workspace_manager.provision", provision),
        patch("app.controllers.ag_ui_controller.select_relative_workspace", return_value="/copy/ws"),
        patch("app.controllers.ag_ui_controller.should_provision_in_place", return_value=False),
        patch("app.controllers.ag_ui_controller._build_fresh_system_prompt", return_value=("", None)),
        patch("app.controllers.ag_ui_controller.build_initial_messages", return_value=[]),
        patch("app.controllers.ag_ui_controller._discard_stale_awaiting_and_cleanup_orphans", AsyncMock(return_value=0)),
        patch("app.controllers.ag_ui_controller.session_close_service.manage_run", _noop_manage_run),
        patch("app.controllers.ag_ui_controller._finalize_run_segment", AsyncMock()),
        patch("app.controllers.ag_ui_controller._finalize", AsyncMock()),
        patch("app.controllers.ag_ui_controller._terminalize_failed_execution", terminalize),
        patch("app.controllers.ag_ui_controller._prepare_thread_claims", AsyncMock(return_value=(0, False))),
        patch("app.controllers.ag_ui_controller.agui_service.refresh_frontend_tools"),
    ]

    payload = AGUIRunRequest.model_validate({
        "threadId": "thread-race",
        "input": {"type": "shared_folder", "uri": "/src"},
        "messages": [UserMessage(id="u1", content="go")],
    })

    for p in shared_patches:
        p.start()
    try:
        winner_resp = await run_agui_session(payload)
        await _read_sse_events(winner_resp)
        loser_resp = await run_agui_session(payload)
        loser_events = await _read_sse_events(loser_resp)
    finally:
        for p in shared_patches:
            p.stop()

    provision.assert_awaited_once()
    terminalize.assert_awaited_once()
    assert terminalize.await_args.kwargs["provisioned"] is False
    assert "RUN_CONFLICT" in loser_events[0]["message"]


@pytest.mark.asyncio
async def test_stream_persist_failure_finishes_run_once():
    exec_id = uuid.uuid4()
    run_pk = uuid.uuid4()
    claimed_state_id = uuid.uuid4()
    finalize_segment = AsyncMock()
    restore = AsyncMock(return_value=True)
    restore_execution = AsyncMock()
    call_order: list[str] = []

    async def finalize_run(*_args, **_kwargs):
        call_order.append("finalize_run")

    finalize_segment.side_effect = finalize_run

    @asynccontextmanager
    async def managing_run(**_kwargs):
        yield RunCancelHandle()

    async def failing_persist(*_args, **_kwargs):
        raise RuntimeError("persist failed")

    async def segment_runner(_cancel_event):
        return {
            "success": True,
            "awaits_response": True,
            "state": {"request": "x", "messages": []},
            "pending_tools": [{"tool_call_id": "call-next"}],
            "tool_calls_info": [],
        }

    with patch("app.controllers.ag_ui_controller.agui_event_service.subscribe", AsyncMock(return_value=asyncio.Queue())), \
         patch("app.controllers.ag_ui_controller.agui_event_service.unsubscribe", AsyncMock()), \
         patch("app.controllers.ag_ui_controller.session_close_service.manage_run", managing_run), \
         patch("app.controllers.ag_ui_controller._persist_await", failing_persist), \
         patch("app.controllers.ag_ui_controller._restore_claimed_state", restore), \
         patch("app.controllers.ag_ui_controller._restore_execution_awaiting", restore_execution), \
         patch("app.controllers.ag_ui_controller._finalize_run_segment", finalize_segment), \
         patch("app.controllers.ag_ui_controller._finalize", AsyncMock()):
        from app.controllers.ag_ui_controller import _stream_run

        resp = _stream_run(
            segment_runner=segment_runner,
            thread_id="thread-persist",
            run_id="run-persist",
            execution_id=exec_id,
            run_pk=run_pk,
            claimed_state_id=claimed_state_id,
        )
        events = await _read_sse_events(resp)
        call_order.append("terminal_event_received")

    finalize_segment.assert_awaited_once()
    restore.assert_awaited_once_with(claimed_state_id)
    restore_execution.assert_awaited_once()
    assert events[-1]["type"] == "RUN_ERROR"
    assert call_order == ["finalize_run", "terminal_event_received"]


@pytest.mark.asyncio
async def test_fresh_run_run_claim_error_returns_session_closed_stream():
    exec_id = uuid.uuid4()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    from app.services.run_lifecycle import RunClaimError

    with patch("app.controllers.ag_ui_controller.get_session", fake_get_session), \
         patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=AsyncMock()), \
         patch("app.controllers.ag_ui_controller._create_agui_execution", AsyncMock(return_value=exec_id)), \
         patch("app.controllers.ag_ui_controller.run_lifecycle_service.ensure_thread_session", AsyncMock()), \
         patch("app.controllers.ag_ui_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)), \
         patch("app.controllers.ag_ui_controller.run_lifecycle_service.get_thread_session", AsyncMock(return_value=None)), \
         patch(
             "app.controllers.ag_ui_controller.run_lifecycle_service.try_create_active_run",
             AsyncMock(side_effect=RunClaimError(code=SESSION_CLOSED, message="Session is closed")),
         ), \
         patch(
             "app.controllers.ag_ui_controller.execution_state_service.update_execution",
             AsyncMock(),
         ) as update_execution, \
         patch("app.controllers.ag_ui_controller._prepare_thread_claims", AsyncMock(return_value=(0, False))), \
         patch("app.controllers.ag_ui_controller.agui_service.refresh_frontend_tools"):
        payload = AGUIRunRequest.model_validate(
            {"threadId": "thread-claim-closed", "messages": [UserMessage(id="u1", content="hi")]}
        )
        resp = await run_agui_session(payload)
        events = await _read_sse_events(resp)

    update_execution.assert_awaited_once()
    assert update_execution.await_args.args[1] == exec_id
    assert update_execution.await_args.kwargs["status"] == ExecutionStatus.FAILED
    assert update_execution.await_args.kwargs["error_message"] == "Session is closed"
    assert events[0]["type"] == "RUN_ERROR"
    assert SESSION_CLOSED in events[0]["message"]


@pytest.mark.asyncio
async def test_resume_run_claim_error_restores_claim_and_streams_closed():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        config={"mode": "workflow"},
        source=None,
    )
    state = SimpleNamespace(
        id=state_id,
        execution_id=exec_id,
        state_payload={"pending_tools": [{"tool_call_id": "call_a"}], "messages": []},
        status=LLMStateStatus.AWAITING_RESPONSE,
        thread_id="thread-db",
        run_id="run-db",
    )
    restore_state = AsyncMock(return_value=True)

    from app.services.run_lifecycle import RunClaimError

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    create_run = AsyncMock(
        side_effect=RunClaimError(code=SESSION_CLOSING, message="Session close is in progress")
    )
    patches = _resume_entry_patches(state, execution, create_run=create_run)
    patches.extend([
        patch("app.controllers.ag_ui_controller.get_session", fake_get_session),
        patch("app.controllers.ag_ui_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)),
        patch("app.controllers.ag_ui_controller.execution_state_service.restore_claimed_state", restore_state),
        patch("app.controllers.ag_ui_controller.execution_state_service.update_execution", AsyncMock()),
    ])
    for p in patches:
        p.start()
    try:
        payload = AGUIRunRequest.model_validate({
            "threadId": "thread-db",
            "state": {"toolCallId": "call_a", "result": {"answer": "a"}},
        })
        resp = await run_agui_session(payload)
        events = await _read_sse_events(resp)
    finally:
        for p in patches:
            p.stop()

    restore_state.assert_awaited_once()
    assert events[0]["type"] == "RUN_ERROR"
    assert SESSION_CLOSING in events[0]["message"]


@pytest.mark.asyncio
async def test_resume_prep_failure_restores_execution_awaiting():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    run_pk = uuid.uuid4()
    execution = SimpleNamespace(
        id=exec_id,
        workspace_path="/tmp/ws",
        config={"mode": "workflow"},
        source=None,
    )
    state = SimpleNamespace(
        id=state_id,
        execution_id=exec_id,
        state_payload={"pending_tools": [{"tool_call_id": "call_a"}], "messages": []},
        status=LLMStateStatus.AWAITING_RESPONSE,
        thread_id="thread-db",
        run_id="run-db",
    )
    abort = AsyncMock()
    restore_state = AsyncMock(return_value=True)
    restore_execution = AsyncMock()
    finalize_segment = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    create_run = AsyncMock(return_value=_fake_run(run_pk))
    patches = _resume_entry_patches(state, execution, create_run=create_run)
    patches.extend([
        patch("app.controllers.ag_ui_controller.get_session", fake_get_session),
        patch("app.controllers.ag_ui_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)),
        patch("app.controllers.ag_ui_controller.execution_state_service.update_execution", AsyncMock()),
        patch(
            "app.controllers.ag_ui_controller._ensure_workspace_for_agui_segment",
            AsyncMock(side_effect=BindingError(INPUT_WORKSPACE_MISSING, "missing")),
        ),
        patch("app.controllers.ag_ui_controller._restore_claimed_state", restore_state),
        patch("app.controllers.ag_ui_controller._restore_execution_awaiting", restore_execution),
        patch("app.controllers.ag_ui_controller._finalize", AsyncMock()),
        patch("app.controllers.ag_ui_controller._finalize_run_segment", finalize_segment),
    ])
    for p in patches:
        p.start()
    try:
        payload = AGUIRunRequest.model_validate({
            "threadId": "thread-db",
            "state": {"toolCallId": "call_a", "result": {"answer": "a"}},
        })
        resp = await run_agui_session(payload)
        await _read_sse_events(resp)
    finally:
        for p in patches:
            p.stop()

    restore_state.assert_awaited_once()
    restore_execution.assert_awaited_once()
    finalize_segment.assert_awaited_once()
