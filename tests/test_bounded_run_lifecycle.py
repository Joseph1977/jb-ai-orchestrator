# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Bounded run lifecycle: model/segment deadlines, output limits, stale reclaim."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.responses import JSONResponse

from app.config import Config
from app.controllers.ag_ui_controller import _run_failure_message
from app.controllers.orchestrator_controller import _managed_hub_process, execute, resume
from app.models.execution_models import Execution, ExecutionRun, ExecutionStatus, LLMStateStatus
from app.models.requests import ExecuteOrchestratorInput, OrchestratorResumeInput
from app.services.local_tool_provider import LocalToolContext
from app.services.mcp_agent_service import (
    LLMUpstreamError,
    MCPAgentService,
    _check_output_limit,
    _llm_upstream_error,
    _safe_litellm_exception,
)
from app.services.run_lifecycle import (
    DEFAULT_HEARTBEAT_STALE_SEC,
    RUN_STATUS_ACTIVE,
    RUN_STATUS_FAILED,
    RunLifecycleService,
    thread_key_for,
)
from app.services.session_close_service import (
    CANCEL_REASON_HEARTBEAT_FAILURE,
    CANCEL_REASON_SEGMENT_DEADLINE,
    RunCancelHandle,
    SessionCloseService,
    SessionCloseSettings,
)
from app.services.tool_hub import ToolExecutionHub
from test_run_lifecycle_phase3 import ConcurrentLifecycleSession, InMemoryLifecycleStore


def test_default_run_budgets_leave_cancellation_headroom():
    assert Config.LITELLM_MODEL_DEADLINE_SEC == 240
    assert Config.RUN_SEGMENT_DEADLINE_SEC == 270
    assert Config.LITELLM_MODEL_DEADLINE_SEC < Config.RUN_SEGMENT_DEADLINE_SEC
    assert Config.LITELLM_MAX_COMPLETION_TOKENS == 0
    assert Config.RUN_CANCELLATION_WARN_SEC == 5


@pytest.mark.parametrize(
    ("model_deadline", "segment_deadline", "message"),
    [
        (0, 270, "LITELLM_MODEL_DEADLINE_SEC"),
        (270, 270, "RUN_SEGMENT_DEADLINE_SEC"),
        (271, 270, "RUN_SEGMENT_DEADLINE_SEC"),
    ],
)
def test_invalid_run_budget_order_is_rejected(
    monkeypatch,
    model_deadline,
    segment_deadline,
    message,
):
    monkeypatch.setattr(Config, "LITELLM_MODEL_DEADLINE_SEC", model_deadline)
    monkeypatch.setattr(Config, "RUN_SEGMENT_DEADLINE_SEC", segment_deadline)
    with pytest.raises(ValueError, match=message):
        Config.validate_config()


@pytest.mark.asyncio
async def test_trickle_stream_hits_absolute_model_deadline(monkeypatch):
    monkeypatch.setattr(Config, "LITELLM_MODEL_DEADLINE_SEC", 0.05)

    class TrickleResponse:
        closed = False

        async def aiter_lines(self):
            while True:
                await asyncio.sleep(0.01)
                yield 'data: {"choices":[{"delta":{"content":"x"}}]}'

        @property
        def status_code(self):
            return 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            self.closed = True
            return False

    trickle_response = TrickleResponse()

    class FakeClient:
        def stream(self, *_args, **_kwargs):
            return trickle_response

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        "app.services.mcp_agent_service.httpx.AsyncClient",
        lambda *args, **kwargs: FakeClient(),
    )

    svc = MCPAgentService(mcp_server_configs=[], litellm_model_deadline_sec=0.05)
    with pytest.raises(LLMUpstreamError) as exc:
        await svc.call_litellm(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-4o",
            stream=True,
        )
    assert exc.value.code == "TIMEOUT"
    assert trickle_response.closed is True


@pytest.mark.asyncio
async def test_nonstream_hits_absolute_model_deadline(monkeypatch):
    async def slow_post(*_args, **_kwargs):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    class FakeClient:
        post = slow_post

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        "app.services.mcp_agent_service.httpx.AsyncClient",
        lambda *args, **kwargs: FakeClient(),
    )

    svc = MCPAgentService(mcp_server_configs=[], litellm_model_deadline_sec=0.05)
    with pytest.raises(LLMUpstreamError) as exc:
        await svc.call_litellm(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-4o",
            stream=False,
        )
    assert exc.value.code == "TIMEOUT"


@pytest.mark.asyncio
async def test_finish_reason_length_raises_output_limit():
    with pytest.raises(LLMUpstreamError) as exc:
        _check_output_limit(
            {
                "choices": [
                    {
                        "message": {
                            "content": "partial",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {"name": "grep", "arguments": "{\"p"},
                                }
                            ],
                        },
                        "finish_reason": "length",
                    }
                ]
            }
        )
    assert exc.value.code == "OUTPUT_LIMIT"


@pytest.mark.asyncio
async def test_stream_without_completion_reason_is_not_accepted():
    class IncompleteResponse:
        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"partial"}}]}'

    with pytest.raises(LLMUpstreamError) as exc:
        await MCPAgentService(mcp_server_configs=[])._consume_chat_stream(
            IncompleteResponse()
        )

    assert exc.value.code == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_done_sentinel_without_finish_reason_defaults_to_stop():
    class CompleteResponse:
        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"complete"}}]}'
            yield "data: [DONE]"

    result = await MCPAgentService(mcp_server_configs=[])._consume_chat_stream(
        CompleteResponse()
    )

    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["choices"][0]["message"]["content"] == "complete"


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        (
            "TIMEOUT",
            "TIMEOUT: The workflow run timed out. The session remains available; try again.",
        ),
        (
            "OUTPUT_LIMIT",
            "OUTPUT_LIMIT: The model reply was cut off. The session and any pending "
            "question remain available; try again.",
        ),
        (
            "RUN_LIFECYCLE_FAILED",
            "RUN_LIFECYCLE_FAILED: Workflow run tracking failed. The session remains "
            "available; try again.",
        ),
        ("UNAVAILABLE", "UNAVAILABLE: Model service is unavailable"),
    ],
)
def test_agui_run_failures_use_structured_public_messages(error_code, expected):
    assert (
        _run_failure_message(
            {
                "success": False,
                "error_code": error_code,
                "error": "private upstream detail",
            }
        )
        == expected
    )


@pytest.mark.asyncio
async def test_streamed_length_finish_reason_surfaces_output_limit():
    class LengthResponse:
        async def aiter_lines(self):
            yield (
                'data: {"choices":[{"delta":{"content":"partial"},'
                '"finish_reason":"length"}]}'
            )
            yield "data: [DONE]"

    result = await MCPAgentService(mcp_server_configs=[])._consume_chat_stream(
        LengthResponse()
    )

    with pytest.raises(LLMUpstreamError) as exc:
        _check_output_limit(result)

    assert exc.value.code == "OUTPUT_LIMIT"


@pytest.mark.asyncio
async def test_output_limit_prevents_tool_routing(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "LOCAL_TOOLS_ENABLED", False)
    mcp = MagicMock()
    mcp.fetch_mcp_tools = AsyncMock(return_value=[])
    mcp.litellm_request_timeout_in_sec = 30
    mcp.litellm_model_deadline_sec = 30
    mcp.litellm_max_completion_tokens = 128
    mcp.call_litellm = AsyncMock(
        side_effect=LLMUpstreamError("OUTPUT_LIMIT", "Model output limit reached")
    )
    mcp.execute_mcp_tool = AsyncMock()

    hub = ToolExecutionHub(
        mcp_service=mcp,
        agui_service=MagicMock(),
        agui_event_service=MagicMock(),
    )
    ctx = LocalToolContext(workspace_path=str(tmp_path))
    result = await hub.process_request(
        request="run grep",
        model="gpt-4o",
        local_context=ctx,
    )
    assert result["success"] is False
    assert result["error_code"] == "OUTPUT_LIMIT"
    mcp.execute_mcp_tool.assert_not_called()


@pytest.mark.asyncio
async def test_max_completion_tokens_forwarded_to_litellm(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    class FakeClient:
        async def post(self, _url, headers=None, json=None, timeout=None):
            captured["json"] = json
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        "app.services.mcp_agent_service.httpx.AsyncClient",
        lambda *args, **kwargs: FakeClient(),
    )

    svc = MCPAgentService(
        mcp_server_configs=[],
        litellm_max_completion_tokens=512,
        litellm_model_deadline_sec=5,
    )
    await svc.call_litellm(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        stream=False,
    )
    assert captured["json"]["max_tokens"] == 512


@pytest.mark.asyncio
async def test_disabled_completion_token_guard_is_not_sent(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    class FakeClient:
        async def post(self, _url, headers=None, json=None, timeout=None):
            captured["json"] = json
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        "app.services.mcp_agent_service.httpx.AsyncClient",
        lambda *args, **kwargs: FakeClient(),
    )

    await MCPAgentService(
        mcp_server_configs=[],
        litellm_max_completion_tokens=0,
        litellm_model_deadline_sec=5,
    ).call_litellm(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        stream=False,
    )

    assert "max_tokens" not in captured["json"]


@pytest.mark.asyncio
async def test_max_completion_tokens_forwarded_to_streaming_litellm(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}'
            yield "data: [DONE]"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class FakeClient:
        def stream(self, _method, _url, headers=None, json=None, timeout=None):
            captured["json"] = json
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        "app.services.mcp_agent_service.httpx.AsyncClient",
        lambda *args, **kwargs: FakeClient(),
    )

    svc = MCPAgentService(
        mcp_server_configs=[],
        litellm_max_completion_tokens=768,
        litellm_model_deadline_sec=5,
    )
    await svc.call_litellm(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o",
        stream=True,
    )
    assert captured["json"]["max_tokens"] == 768


@pytest.mark.asyncio
async def test_segment_deadline_cancels_worker(monkeypatch):
    settings = SessionCloseSettings(
        heartbeat_interval_sec=60,
        heartbeat_stale_sec=300,
        close_wait_timeout_sec=10,
        segment_deadline_sec=0.05,
        cancellation_warn_sec=5,
    )
    service = SessionCloseService(settings=settings, registry=MagicMock())
    service._registry.register = AsyncMock()
    service._registry.unregister = AsyncMock()
    service._lifecycle.heartbeat_run = AsyncMock(return_value=True)
    service._lifecycle.is_execution_close_requested = AsyncMock(return_value=False)
    service._lifecycle.is_thread_close_requested = AsyncMock(return_value=False)

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    monkeypatch.setattr(
        "app.services.session_close_service.get_session",
        fake_get_session,
    )

    async def worker():
        try:
            while True:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            raise

    worker_task = asyncio.create_task(worker())
    handle: RunCancelHandle | None = None

    async with service.manage_run(
        run_pk=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        task=worker_task,
    ) as cancel_handle:
        handle = cancel_handle
        with pytest.raises(asyncio.CancelledError):
            await worker_task

    assert handle is not None
    assert handle.reason == CANCEL_REASON_SEGMENT_DEADLINE
    assert worker_task.done()
    service._registry.unregister.assert_awaited_once()


@pytest.mark.asyncio
async def test_heartbeat_failure_keeps_claim_fresh_until_worker_stops(monkeypatch):
    settings = SessionCloseSettings(
        heartbeat_interval_sec=0.01,
        heartbeat_stale_sec=300,
        close_wait_timeout_sec=10,
        segment_deadline_sec=30,
        cancellation_warn_sec=5,
    )
    service = SessionCloseService(settings=settings, registry=MagicMock())
    service._registry.register = AsyncMock()
    service._registry.unregister = AsyncMock()
    heartbeat_attempts = 0

    async def heartbeat_run(*_args, **_kwargs):
        nonlocal heartbeat_attempts
        heartbeat_attempts += 1
        if heartbeat_attempts == 1:
            raise RuntimeError("db down")
        return True

    service._lifecycle.heartbeat_run = AsyncMock(side_effect=heartbeat_run)
    service._lifecycle.is_execution_close_requested = AsyncMock(return_value=False)
    service._lifecycle.is_thread_close_requested = AsyncMock(return_value=False)

    worker_cancelled = asyncio.Event()
    release_worker = asyncio.Event()

    async def worker():
        while not release_worker.is_set():
            try:
                await release_worker.wait()
            except asyncio.CancelledError:
                worker_cancelled.set()

    worker_task = asyncio.create_task(worker())
    handle: RunCancelHandle | None = None

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    monkeypatch.setattr(
        "app.services.session_close_service.get_session",
        fake_get_session,
    )

    async with service.manage_run(
        run_pk=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        task=worker_task,
    ) as cancel_handle:
        handle = cancel_handle
        await asyncio.wait_for(worker_cancelled.wait(), timeout=1)
        await asyncio.sleep(0.04)
        assert heartbeat_attempts >= 2
        release_worker.set()
        await worker_task

    assert handle is not None
    assert handle.reason == CANCEL_REASON_HEARTBEAT_FAILURE
    assert worker_task.done()


@pytest.mark.asyncio
async def test_heartbeat_and_teardown_share_one_cancellation_diagnostic(monkeypatch, caplog):
    settings = SessionCloseSettings(
        heartbeat_interval_sec=0.005,
        heartbeat_stale_sec=300,
        close_wait_timeout_sec=10,
        segment_deadline_sec=30,
        cancellation_warn_sec=0.01,
    )
    service = SessionCloseService(settings=settings, registry=MagicMock())
    service._registry.register = AsyncMock()
    service._registry.unregister = AsyncMock()
    heartbeat_attempts = 0

    async def heartbeat_run(*_args, **_kwargs):
        nonlocal heartbeat_attempts
        heartbeat_attempts += 1
        if heartbeat_attempts == 1:
            raise RuntimeError("db down")
        return True

    service._lifecycle.heartbeat_run = AsyncMock(side_effect=heartbeat_run)
    service._lifecycle.is_execution_close_requested = AsyncMock(return_value=False)
    service._lifecycle.is_thread_close_requested = AsyncMock(return_value=False)

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    monkeypatch.setattr(
        "app.services.session_close_service.get_session",
        fake_get_session,
    )

    cancellation_seen = asyncio.Event()
    release_worker = asyncio.Event()

    async def resistant_worker():
        while not release_worker.is_set():
            try:
                await release_worker.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()

    worker = asyncio.create_task(resistant_worker())
    context = service.manage_run(
        run_pk=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        task=worker,
    )
    await context.__aenter__()

    with caplog.at_level("ERROR"):
        await asyncio.wait_for(cancellation_seen.wait(), timeout=1)
        teardown = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.sleep(0.035)
        elapsed = [
            getattr(record, "elapsed_sec")
            for record in caplog.records
            if getattr(record, "event", None) == "worker_cancellation_slow"
        ]
        assert len(elapsed) >= 2
        assert elapsed == sorted(set(elapsed))
        release_worker.set()
        await asyncio.wait_for(teardown, timeout=1)


@pytest.mark.asyncio
async def test_stale_run_reclaimed_before_fresh_conflict():
    store = InMemoryLifecycleStore()
    exec_id = uuid.uuid4()
    store.add_execution(Execution(id=exec_id, status=ExecutionStatus.RUNNING))
    now = datetime.now(timezone.utc)
    stale = ExecutionRun(
        id=uuid.uuid4(),
        execution_id=exec_id,
        thread_key=thread_key_for("thread-a"),
        run_id="stale",
        status=RUN_STATUS_ACTIVE,
        heartbeat_at=now - timedelta(seconds=DEFAULT_HEARTBEAT_STALE_SEC + 30),
        started_at=now - timedelta(minutes=10),
    )
    store.execution_runs[stale.id] = stale
    session = ConcurrentLifecycleSession(store)
    svc = RunLifecycleService()

    reclaimed = await svc.try_create_active_run(
        session,
        execution_id=exec_id,
        run_id="replacement",
        thread_id="thread-a",
    )
    assert reclaimed is not None
    assert stale.status == RUN_STATUS_FAILED

    fresh = ExecutionRun(
        id=uuid.uuid4(),
        execution_id=exec_id,
        thread_key=thread_key_for("thread-a"),
        run_id="fresh",
        status=RUN_STATUS_ACTIVE,
        heartbeat_at=now,
        started_at=now,
    )
    store.execution_runs[fresh.id] = fresh

    blocked = await svc.try_create_active_run(
        session,
        execution_id=exec_id,
        run_id="blocked",
        thread_id="thread-a",
    )
    assert blocked is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("same_execution", "same_thread"),
    [(True, False), (False, True)],
)
async def test_stale_run_reclaimed_across_each_conflict_scope(
    same_execution,
    same_thread,
):
    store = InMemoryLifecycleStore()
    execution_id = uuid.uuid4()
    stale_execution_id = execution_id if same_execution else uuid.uuid4()
    store.add_execution(Execution(id=execution_id, status=ExecutionStatus.RUNNING))
    if stale_execution_id != execution_id:
        store.add_execution(
            Execution(id=stale_execution_id, status=ExecutionStatus.RUNNING)
        )
    thread_id = "thread-current"
    stale_thread_id = thread_id if same_thread else "thread-other"
    now = datetime.now(timezone.utc)
    stale = ExecutionRun(
        id=uuid.uuid4(),
        execution_id=stale_execution_id,
        thread_key=thread_key_for(stale_thread_id),
        run_id="stale",
        status=RUN_STATUS_ACTIVE,
        heartbeat_at=now - timedelta(seconds=DEFAULT_HEARTBEAT_STALE_SEC + 30),
        started_at=now - timedelta(minutes=10),
    )
    store.execution_runs[stale.id] = stale

    replacement = await RunLifecycleService().try_create_active_run(
        ConcurrentLifecycleSession(store),
        execution_id=execution_id,
        run_id="replacement",
        thread_id=thread_id,
    )

    assert replacement is not None
    assert stale.status == RUN_STATUS_FAILED


@pytest.mark.asyncio
async def test_heartbeat_failure_is_not_reported_as_timeout(monkeypatch):
    @asynccontextmanager
    async def fail_heartbeat(*_args, task, **_kwargs):
        handle = RunCancelHandle(reason=CANCEL_REASON_HEARTBEAT_FAILURE)
        handle.event.set()
        task.cancel()
        yield handle

    monkeypatch.setattr(
        "app.controllers.orchestrator_controller.session_close_service.manage_run",
        fail_heartbeat,
    )

    result = await _managed_hub_process(
        run_pk=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        thread_id=None,
        make_coro=lambda _cancel_event: asyncio.sleep(30),
    )

    assert result["error_code"] == "RUN_LIFECYCLE_FAILED"


@pytest.mark.asyncio
async def test_slow_worker_cancellation_is_logged_until_worker_stops(monkeypatch, caplog):
    settings = SessionCloseSettings(
        heartbeat_interval_sec=60,
        heartbeat_stale_sec=300,
        close_wait_timeout_sec=10,
        segment_deadline_sec=0.01,
        cancellation_warn_sec=0.01,
    )
    service = SessionCloseService(settings=settings, registry=MagicMock())
    service._registry.register = AsyncMock()
    service._registry.unregister = AsyncMock()
    service._lifecycle.heartbeat_run = AsyncMock(return_value=True)
    service._lifecycle.is_execution_close_requested = AsyncMock(return_value=False)
    service._lifecycle.is_thread_close_requested = AsyncMock(return_value=False)

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    monkeypatch.setattr(
        "app.services.session_close_service.get_session",
        fake_get_session,
    )

    async def slow_to_cancel():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            raise

    task = asyncio.create_task(slow_to_cancel())
    with caplog.at_level("ERROR"):
        async with service.manage_run(
            run_pk=uuid.uuid4(),
            execution_id=uuid.uuid4(),
            task=task,
        ):
            with pytest.raises(asyncio.CancelledError):
                await task

    diagnostics = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "worker_cancellation_slow"
    ]
    assert len(diagnostics) >= 2
    assert [getattr(record, "elapsed_sec", 0) for record in diagnostics] == sorted(
        getattr(record, "elapsed_sec", 0) for record in diagnostics
    )
    count_after_stop = len(diagnostics)
    await asyncio.sleep(0.03)
    assert (
        sum(
            getattr(record, "event", None) == "worker_cancellation_slow"
            for record in caplog.records
        )
        == count_after_stop
    )


@pytest.mark.asyncio
async def test_cancellation_resistant_worker_keeps_claim_fresh(monkeypatch):
    monkeypatch.setattr(
        "app.services.run_lifecycle.DEFAULT_HEARTBEAT_STALE_SEC",
        0.04,
    )
    monkeypatch.setattr(Config, "RUN_HEARTBEAT_STALE_SEC", 0.04)
    store = InMemoryLifecycleStore()
    execution_id = uuid.uuid4()
    thread_id = "slow-cancellation-thread"
    store.add_execution(Execution(id=execution_id, status=ExecutionStatus.RUNNING))
    lifecycle = RunLifecycleService()
    run = await lifecycle.try_create_active_run(
        ConcurrentLifecycleSession(store),
        execution_id=execution_id,
        run_id="first",
        thread_id=thread_id,
    )
    assert run is not None

    async def refresh_heartbeat(_session, run_pk):
        store.execution_runs[run_pk].heartbeat_at = datetime.now(timezone.utc)
        return True

    heartbeat_run = AsyncMock(side_effect=refresh_heartbeat)
    lifecycle.heartbeat_run = heartbeat_run

    @asynccontextmanager
    async def fake_get_session():
        yield ConcurrentLifecycleSession(store)

    monkeypatch.setattr(
        "app.services.session_close_service.get_session",
        fake_get_session,
    )
    registry = MagicMock()
    registry.register = AsyncMock()
    registry.unregister = AsyncMock()
    service = SessionCloseService(
        lifecycle=lifecycle,
        registry=registry,
        settings=SessionCloseSettings(
            heartbeat_interval_sec=0.005,
            heartbeat_stale_sec=0.04,
            close_wait_timeout_sec=1,
            segment_deadline_sec=60,
            cancellation_warn_sec=1,
        ),
    )
    release = asyncio.Event()
    cancellation_seen = asyncio.Event()

    async def cancellation_resistant_worker():
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()

    worker = asyncio.create_task(cancellation_resistant_worker())
    context = service.manage_run(
        run_pk=run.id,
        execution_id=execution_id,
        task=worker,
        thread_id=thread_id,
    )
    await context.__aenter__()
    teardown = asyncio.create_task(context.__aexit__(None, None, None))
    await asyncio.wait_for(cancellation_seen.wait(), timeout=1)
    await asyncio.sleep(0.06)

    try:
        blocked = await lifecycle.try_create_active_run(
            ConcurrentLifecycleSession(store),
            execution_id=execution_id,
            run_id="second",
            thread_id=thread_id,
        )

        assert heartbeat_run.await_count > 0
        assert blocked is None
        assert store.execution_runs[run.id].status == RUN_STATUS_ACTIVE
        assert not teardown.done()
    finally:
        release.set()
        await asyncio.wait_for(teardown, timeout=1)


@pytest.mark.asyncio
async def test_worker_teardown_preserves_outer_cancellation():
    service = SessionCloseService(registry=MagicMock())
    worker = asyncio.create_task(asyncio.sleep(30))
    teardown = asyncio.create_task(
        service._await_worker_with_diagnostics(
            worker,
            run_pk=uuid.uuid4(),
            execution_id=uuid.uuid4(),
        )
    )
    await asyncio.sleep(0)
    teardown.cancel()

    with pytest.raises(asyncio.CancelledError):
        await teardown


@pytest.mark.asyncio
async def test_close_reconciliation_rechecks_stale_heartbeat_atomically():
    run = SimpleNamespace(id=uuid.uuid4(), execution_id=uuid.uuid4())
    lifecycle = MagicMock()
    lifecycle.list_stale_runs = AsyncMock(return_value=[run])
    lifecycle.finish_run = AsyncMock(return_value=True)
    service = SessionCloseService(lifecycle=lifecycle, registry=MagicMock())
    session = MagicMock()
    session.execute = AsyncMock()

    assert await service._reconcile_stale_runs(session, execution_id=run.execution_id) == 1

    finish_kwargs = lifecycle.finish_run.await_args.kwargs
    assert finish_kwargs["heartbeat_before"] is not None


@pytest.mark.asyncio
async def test_execute_returns_structured_run_conflict():
    exec_id = uuid.uuid4()
    execution = AsyncMock(
        id=exec_id,
        workspace_path=None,
        config={},
        closed_at=None,
        close_requested_at=None,
        orchestration_type="workflow",
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=None)), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)):
        response = await execute(
            ExecuteOrchestratorInput(orchestratorGuid=exec_id, prompt="hello")
        )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
    body = response.body.decode()
    assert "RUN_CONFLICT" in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "error_message"),
    [
        ("TIMEOUT", "Run timed out"),
        ("RUN_LIFECYCLE_FAILED", "Workflow run tracking failed"),
    ],
)
async def test_resume_failure_restores_claim_and_returns_code(
    error_code,
    error_message,
):
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
    execution = AsyncMock(
        id=exec_id,
        workspace_path=None,
        config={},
        closed_at=None,
        close_requested_at=None,
    )
    state = AsyncMock(
        id=state_id,
        execution_id=exec_id,
        state_payload=state_payload,
        status=LLMStateStatus.AWAITING_RESPONSE,
        thread_id=None,
        run_id="run-claim",
    )
    fake_run = SimpleNamespace(id=uuid.uuid4())
    restore = AsyncMock(return_value=True)
    restore_execution = AsyncMock()
    finalize = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_state", AsyncMock(return_value=state)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.recover_stale_pending_claims", AsyncMock(return_value=0)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.restore_claimed_state", restore), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=fake_run)), \
         patch("app.controllers.orchestrator_controller._ensure_workspace_for_segment", AsyncMock(return_value=None)), \
         patch("app.controllers.orchestrator_controller.get_tool_hub", return_value=MagicMock()), \
         patch("app.controllers.orchestrator_controller._managed_hub_process", AsyncMock(return_value={
             "success": False,
             "error": error_message,
             "error_code": error_code,
         })), \
         patch("app.controllers.orchestrator_controller._finalize", finalize), \
         patch("app.controllers.orchestrator_controller._complete_run_lifecycle", AsyncMock()), \
         patch("app.controllers.orchestrator_controller._restore_execution_awaiting", restore_execution), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)):
        response = await resume(
            OrchestratorResumeInput(
                orchestratorGuid=exec_id,
                stateGuid=state_id,
                toolCallId="call_a",
                result={"answer": "a"},
            )
        )

    assert isinstance(response, JSONResponse)
    body = response.body.decode()
    assert error_code in body
    restore.assert_awaited()
    restore_execution.assert_awaited_once_with(
        exec_id,
        {
            "success": False,
            "error": error_message,
            "error_code": error_code,
        },
    )
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_run_conflict_is_structured_json():
    state_id = uuid.uuid4()
    exec_id = uuid.uuid4()
    execution = AsyncMock(id=exec_id, workspace_path=None, config={}, closed_at=None, close_requested_at=None)
    state = AsyncMock(
        id=state_id,
        execution_id=exec_id,
        state_payload={"pending_tools": [{"tool_call_id": "call_a"}]},
        status=LLMStateStatus.AWAITING_RESPONSE,
        thread_id=None,
        run_id="run-claim",
    )
    restore = AsyncMock(return_value=True)

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.get_state", AsyncMock(return_value=state)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.recover_stale_pending_claims", AsyncMock(return_value=0)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.try_claim_state_for_resume", AsyncMock(return_value=True)), \
         patch("app.controllers.orchestrator_controller.execution_state_service.restore_claimed_state", restore), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.try_create_active_run", AsyncMock(return_value=None)), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.reject_if_close_requested", AsyncMock(return_value=False)):
        response = await resume(
            OrchestratorResumeInput(
                orchestratorGuid=exec_id,
                stateGuid=state_id,
                toolCallId="call_a",
                result={"answer": "a"},
            )
        )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
    assert "RUN_CONFLICT" in response.body.decode()
    restore.assert_awaited()


def test_transport_timeout_maps_to_timeout_code():
    err = _safe_litellm_exception(httpx.ReadTimeout("slow"))
    assert isinstance(err, LLMUpstreamError)
    assert err.code == "TIMEOUT"


@pytest.mark.parametrize("status_code", [408, 504])
def test_upstream_timeout_status_maps_to_timeout_code(status_code):
    assert _llm_upstream_error(status_code).code == "TIMEOUT"


@pytest.mark.asyncio
async def test_stale_finalization_rechecks_heartbeat_atomically():
    captured = {}

    class EmptyResult:
        @staticmethod
        def scalar_one_or_none():
            return None

    class CapturingSession:
        async def execute(self, statement):
            captured["statement"] = statement
            return EmptyResult()

    await RunLifecycleService().finish_run(
        CapturingSession(),
        uuid.uuid4(),
        status=RUN_STATUS_FAILED,
        heartbeat_before=datetime.now(timezone.utc),
    )

    sql = str(captured["statement"])
    assert "execution_runs.heartbeat_at <" in sql


def _apply_patches_context(patches):
    from contextlib import ExitStack

    class _Ctx:
        def __enter__(self):
            self._stack = ExitStack()
            self._stack.__enter__()
            for item in patches:
                self._stack.enter_context(item)
            return self

        def __exit__(self, *args):
            return self._stack.__exit__(*args)

    return _Ctx()


def _patch_target(target: str, value):
    if isinstance(value, (AsyncMock, MagicMock)):
        return patch(target, value)
    if callable(value) and not isinstance(value, MagicMock):
        return patch(target, value)
    return patch(target, return_value=value)


def _agui_fresh_run_base_patches(*, exec_id, run_pk, fake_get_session, **overrides):
    patches = {
        "app.controllers.ag_ui_controller.get_session": fake_get_session,
        "app.controllers.ag_ui_controller._create_agui_execution": AsyncMock(return_value=exec_id),
        "app.controllers.ag_ui_controller.run_lifecycle_service.ensure_thread_session": AsyncMock(),
        "app.controllers.ag_ui_controller.run_lifecycle_service.reject_if_close_requested": AsyncMock(
            return_value=False
        ),
        "app.controllers.ag_ui_controller.run_lifecycle_service.get_thread_session": AsyncMock(
            return_value=None
        ),
        "app.controllers.ag_ui_controller.run_lifecycle_service.try_create_active_run": AsyncMock(
            return_value=SimpleNamespace(id=run_pk)
        ),
        "app.controllers.ag_ui_controller.run_lifecycle_service.associate_execution_origin": AsyncMock(),
        "app.controllers.ag_ui_controller.execution_state_service.update_execution": AsyncMock(),
        "app.controllers.ag_ui_controller._prepare_thread_claims": AsyncMock(return_value=(0, False)),
        "app.controllers.ag_ui_controller.agui_service.refresh_frontend_tools": MagicMock(),
    }
    patches.update(overrides)
    return [_patch_target(target, value) for target, value in patches.items()]


def _agui_stream_patches():
    return [
        patch(
            "app.controllers.ag_ui_controller.agui_event_service.subscribe",
            AsyncMock(return_value=asyncio.Queue()),
        ),
        patch("app.controllers.ag_ui_controller.agui_event_service.unsubscribe", AsyncMock()),
    ]


@pytest.mark.asyncio
async def test_agui_manage_run_starts_before_segment_prep():
    order: list[str] = []
    exec_id = uuid.uuid4()
    run_pk = uuid.uuid4()

    @asynccontextmanager
    async def tracking_manage_run(**_kwargs):
        order.append("manage_run_enter")
        yield RunCancelHandle()
        order.append("manage_run_exit")

    def slow_build_prompt(*_args, **_kwargs):
        order.append("prep")
        return "system", None

    hub = SimpleNamespace(
        process_request=AsyncMock(
            return_value={"success": True, "response": "ok", "tool_calls_info": []}
        )
    )

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    patches = _agui_fresh_run_base_patches(
        exec_id=exec_id,
        run_pk=run_pk,
        fake_get_session=fake_get_session,
        **{
            "app.controllers.ag_ui_controller.get_tool_hub": hub,
            "app.controllers.ag_ui_controller._discard_stale_awaiting_and_cleanup_orphans": AsyncMock(
                return_value=0
            ),
            "app.controllers.ag_ui_controller._build_fresh_system_prompt": slow_build_prompt,
            "app.controllers.ag_ui_controller.session_close_service.manage_run": tracking_manage_run,
            "app.controllers.ag_ui_controller._finalize_run_segment": AsyncMock(),
            "app.controllers.ag_ui_controller._finalize": AsyncMock(),
            "app.controllers.ag_ui_controller.build_initial_messages": lambda **_kwargs: [],
        },
    )
    patches.extend(_agui_stream_patches())

    from ag_ui.core.types import UserMessage
    from app.controllers.ag_ui_controller import AGUIRunRequest, run_agui_session

    with _apply_patches_context(patches):
        resp = await run_agui_session(
            AGUIRunRequest.model_validate(
                {
                    "threadId": "thread-managed-prep",
                    "messages": [UserMessage(id="u1", content="go")],
                }
            )
        )
        async for _ in resp.body_iterator:
            pass

    assert order.index("manage_run_enter") < order.index("prep")
    assert order.index("prep") < order.index("manage_run_exit")


@pytest.mark.asyncio
async def test_agui_prep_failure_cleans_provisioned_workspace_without_task_leak():
    from ag_ui.core.types import UserMessage
    from app.controllers.ag_ui_controller import AGUIRunRequest, run_agui_session
    from app.services.binding_contract import BindingError
    from test_agui_phase3 import _apply_patches, _fake_run, _lifecycle_patches

    exec_id = uuid.uuid4()
    run_pk = uuid.uuid4()
    cleanup = MagicMock()
    finalize = AsyncMock()
    finalize_segment = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    binding = MagicMock()
    binding.type.value = "shared_folder"
    binding.relative_path = "."

    patches = _lifecycle_patches(create_run=AsyncMock(return_value=_fake_run(run_pk)))
    patches.extend(_agui_stream_patches())
    patches.extend([
        patch("app.controllers.ag_ui_controller.get_session", fake_get_session),
        patch("app.controllers.ag_ui_controller.get_tool_hub", return_value=MagicMock()),
        patch("app.controllers.ag_ui_controller._create_agui_execution", AsyncMock(return_value=exec_id)),
        patch("app.controllers.ag_ui_controller.execution_state_service.update_execution", AsyncMock()),
        patch("app.controllers.ag_ui_controller.resolve_initiate_input", return_value=(binding, MagicMock(value="workflow"), False)),
        patch("app.controllers.ag_ui_controller.assert_agui_phase1_input"),
        patch("app.controllers.ag_ui_controller.sanitize_execution_config", return_value={"mode": "workflow", "runtimePath": "/rt"}),
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
        patch("app.controllers.ag_ui_controller.build_initial_messages", return_value=[]),
    ])

    with _apply_patches(patches):
        resp = await run_agui_session(
            AGUIRunRequest.model_validate(
                {
                    "threadId": "thread-prep-managed-fail",
                    "input": {"type": "shared_folder", "uri": "/src"},
                    "messages": [UserMessage(id="u1", content="go")],
                }
            )
        )
        async for _ in resp.body_iterator:
            pass

    cleanup.assert_called_once_with(exec_id)
    finalize.assert_awaited_once()
    finalize_segment.assert_awaited_once()


@pytest.mark.asyncio
async def test_agui_prep_cancellation_cleans_provisioned_workspace(monkeypatch):
    settings = SessionCloseSettings(
        heartbeat_interval_sec=60,
        heartbeat_stale_sec=300,
        close_wait_timeout_sec=10,
        segment_deadline_sec=0.05,
        cancellation_warn_sec=5,
    )
    service = SessionCloseService(settings=settings, registry=MagicMock())
    service._registry.register = AsyncMock()
    service._registry.unregister = AsyncMock()
    service._lifecycle.heartbeat_run = AsyncMock(return_value=True)
    service._lifecycle.is_execution_close_requested = AsyncMock(return_value=False)
    service._lifecycle.is_thread_close_requested = AsyncMock(return_value=False)

    cleanup = MagicMock()
    finalize = AsyncMock()
    exec_id = uuid.uuid4()
    run_pk = uuid.uuid4()
    prep_started = asyncio.Event()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    async def slow_provision(*_args, **_kwargs):
        prep_started.set()
        await asyncio.sleep(30)
        return SimpleNamespace(path="/copy", in_place=False)

    binding = MagicMock()
    binding.type.value = "shared_folder"
    binding.relative_path = "."

    monkeypatch.setattr(
        "app.services.session_close_service.get_session",
        fake_get_session,
    )

    patches = _agui_fresh_run_base_patches(
        exec_id=exec_id,
        run_pk=run_pk,
        fake_get_session=fake_get_session,
        **{
            "app.controllers.ag_ui_controller.get_tool_hub": SimpleNamespace(
                process_request=AsyncMock(
                    return_value={"success": True, "response": "ok", "tool_calls_info": []}
                )
            ),
            "app.controllers.ag_ui_controller._discard_stale_awaiting_and_cleanup_orphans": AsyncMock(
                return_value=0
            ),
            "app.controllers.ag_ui_controller._build_fresh_system_prompt": lambda *_a, **_k: ("system", None),
            "app.controllers.ag_ui_controller.session_close_service.manage_run": service.manage_run,
            "app.controllers.ag_ui_controller._finalize_run_segment": AsyncMock(),
            "app.controllers.ag_ui_controller._finalize": finalize,
            "app.controllers.ag_ui_controller.workspace_manager.cleanup": cleanup,
            "app.controllers.ag_ui_controller.workspace_manager.provision": slow_provision,
            "app.controllers.ag_ui_controller.resolve_initiate_input": (
                binding,
                MagicMock(value="workflow"),
                False,
            ),
            "app.controllers.ag_ui_controller.assert_agui_phase1_input": MagicMock(),
            "app.controllers.ag_ui_controller.sanitize_execution_config": {
                "mode": "workflow",
                "runtimePath": "/rt",
            },
            "app.controllers.ag_ui_controller.provision_source": "/src",
            "app.controllers.ag_ui_controller.select_relative_workspace": "/copy/ws",
            "app.controllers.ag_ui_controller.should_provision_in_place": False,
            "app.controllers.ag_ui_controller.build_initial_messages": lambda **_k: [],
        },
    )
    patches.extend(_agui_stream_patches())

    from ag_ui.core.types import UserMessage
    from app.controllers.ag_ui_controller import AGUIRunRequest, run_agui_session

    with _apply_patches_context(patches):
        resp = await run_agui_session(
            AGUIRunRequest.model_validate(
                {
                    "threadId": "thread-prep-cancel",
                    "input": {"type": "shared_folder", "uri": "/src"},
                    "messages": [UserMessage(id="u1", content="go")],
                }
            )
        )

        async def _consume():
            async for _ in resp.body_iterator:
                pass

        consumer = asyncio.create_task(_consume())
        await asyncio.wait_for(prep_started.wait(), timeout=1)
        await consumer

    service._registry.unregister.assert_awaited_once()
    assert prep_started.is_set()
    cleanup.assert_called_once_with(exec_id)
    finalize.assert_awaited_once()
    assert finalize.await_args.args[1]["error_code"] == "TIMEOUT"


@pytest.mark.asyncio
async def test_agui_prep_failure_finalizes_claim_before_terminal_event():
    from app.controllers.ag_ui_controller import _stream_run

    order: list[str] = []

    @asynccontextmanager
    async def manage_run(**_kwargs):
        yield RunCancelHandle()

    async def segment_runner(_cancel_event):
        return {
            "success": False,
            "error": "timed out",
            "error_code": "TIMEOUT",
            "_prep_failed": True,
        }

    async def finalize_segment(*_args, **_kwargs):
        order.append("claim_finalized")

    with patch(
        "app.controllers.ag_ui_controller.agui_event_service.subscribe",
        AsyncMock(return_value=asyncio.Queue()),
    ), patch(
        "app.controllers.ag_ui_controller.agui_event_service.unsubscribe",
        AsyncMock(),
    ), patch(
        "app.controllers.ag_ui_controller.session_close_service.manage_run",
        manage_run,
    ), patch(
        "app.controllers.ag_ui_controller._finalize",
        AsyncMock(),
    ), patch(
        "app.controllers.ag_ui_controller._finalize_run_segment",
        finalize_segment,
    ):
        response = _stream_run(
            segment_runner=segment_runner,
            thread_id="terminal-order",
            run_id="run",
            execution_id=uuid.uuid4(),
            run_pk=uuid.uuid4(),
        )
        async for chunk in response.body_iterator:
            if "RUN_ERROR" in chunk:
                order.append("terminal_event")

    assert order == ["claim_finalized", "terminal_event"]


@pytest.mark.asyncio
async def test_agui_deadline_waits_for_resistant_worker_before_terminal_event():
    from app.controllers.ag_ui_controller import _stream_run

    order: list[str] = []
    cancellation_seen = asyncio.Event()
    release_worker = asyncio.Event()

    @asynccontextmanager
    async def manage_run(*, task, **_kwargs):
        handle = RunCancelHandle()

        async def cancel_worker():
            await asyncio.sleep(0)
            handle.reason = CANCEL_REASON_SEGMENT_DEADLINE
            handle.event.set()
            task.cancel()

        asyncio.create_task(cancel_worker())
        yield handle

    async def segment_runner(_cancel_event):
        try:
            await release_worker.wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release_worker.wait()
        order.append("worker_stopped")
        return {"success": True, "response": "late"}

    async def finalize_segment(*_args, **_kwargs):
        order.append("claim_finalized")

    with patch(
        "app.controllers.ag_ui_controller.agui_event_service.subscribe",
        AsyncMock(return_value=asyncio.Queue()),
    ), patch(
        "app.controllers.ag_ui_controller.agui_event_service.unsubscribe",
        AsyncMock(),
    ), patch(
        "app.controllers.ag_ui_controller.session_close_service.manage_run",
        manage_run,
    ), patch(
        "app.controllers.ag_ui_controller._finalize",
        AsyncMock(),
    ), patch(
        "app.controllers.ag_ui_controller._finalize_run_segment",
        finalize_segment,
    ):
        response = _stream_run(
            segment_runner=segment_runner,
            thread_id="deadline-order",
            run_id="run",
            execution_id=uuid.uuid4(),
            run_pk=uuid.uuid4(),
        )

        async def consume():
            async for chunk in response.body_iterator:
                if "RUN_ERROR" in chunk:
                    order.append("terminal_event")

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(cancellation_seen.wait(), timeout=1)
        await asyncio.sleep(0)
        assert order == []
        release_worker.set()
        await asyncio.wait_for(consumer, timeout=1)

    assert order == ["worker_stopped", "claim_finalized", "terminal_event"]


@pytest.mark.asyncio
async def test_agui_resume_failure_restores_awaiting_before_terminal_event():
    from app.controllers.ag_ui_controller import _stream_run

    execution_id = uuid.uuid4()
    state_id = uuid.uuid4()
    restore_state = AsyncMock(return_value=True)
    restore_execution = AsyncMock()
    finalize_execution = AsyncMock()
    finalize_segment = AsyncMock()

    @asynccontextmanager
    async def manage_run(**_kwargs):
        yield RunCancelHandle()

    failure = {
        "success": False,
        "error": "provider unavailable",
        "error_code": "UNAVAILABLE",
    }

    with patch(
        "app.controllers.ag_ui_controller.agui_event_service.subscribe",
        AsyncMock(return_value=asyncio.Queue()),
    ), patch(
        "app.controllers.ag_ui_controller.agui_event_service.unsubscribe",
        AsyncMock(),
    ), patch(
        "app.controllers.ag_ui_controller.session_close_service.manage_run",
        manage_run,
    ), patch(
        "app.controllers.ag_ui_controller._restore_claimed_state",
        restore_state,
    ), patch(
        "app.controllers.ag_ui_controller._restore_execution_awaiting",
        restore_execution,
    ), patch(
        "app.controllers.ag_ui_controller._finalize",
        finalize_execution,
    ), patch(
        "app.controllers.ag_ui_controller._finalize_run_segment",
        finalize_segment,
    ):
        response = _stream_run(
            segment_runner=lambda _event: asyncio.sleep(0, result=failure),
            thread_id="resume-recovery",
            run_id="run",
            execution_id=execution_id,
            run_pk=uuid.uuid4(),
            claimed_state_id=state_id,
        )
        chunks = [chunk async for chunk in response.body_iterator]

    assert any("RUN_ERROR" in chunk for chunk in chunks)
    restore_state.assert_awaited_once_with(state_id)
    restore_execution.assert_awaited_once_with(execution_id, failure)
    finalize_execution.assert_not_awaited()
    finalize_segment.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_failure_state_preserves_last_attempt_diagnostics():
    from app.controllers.orchestrator_controller import _restore_execution_pending

    execution_id = uuid.uuid4()
    failure = {
        "success": False,
        "error": "model unavailable",
        "error_code": "UNAVAILABLE",
    }
    update_execution = AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    with patch(
        "app.controllers.orchestrator_controller.get_session",
        fake_get_session,
    ), patch(
        "app.controllers.orchestrator_controller.execution_state_service.update_execution",
        update_execution,
    ):
        await _restore_execution_pending(execution_id, failure)

    update_execution.assert_awaited_once_with(
        ANY,
        execution_id,
        status=ExecutionStatus.PENDING,
        result=failure,
        error_message="model unavailable",
    )
