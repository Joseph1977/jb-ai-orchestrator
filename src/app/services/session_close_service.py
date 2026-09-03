# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Phase 3 close coordinator: transactional close, heartbeat, and cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.db.session import get_session
from app.models.execution_models import Execution, ExecutionStatus
from app.services.execution_state_service import execution_state_service
from app.services.run_lifecycle import (
    RUN_STATUS_FAILED,
    RunLifecycleService,
    run_lifecycle_service,
    thread_key_for,
)
from app.services.run_registry import RunRegistry, run_registry
from app.services.runtime_paths import (
    CleanupOutcome,
    RuntimeDeletionResult,
    delete_execution_runtime,
    delete_thread_runtime,
    runtime_root_for_execution,
)
from app.services.workspace_manager import WorkspaceCleanupResult, cleanup_execution_sandbox
from app.utils.logger import logger

SESSION_CLOSED_ERROR = "session_closed"
_POLL_INTERVAL_SEC = 0.25

CANCEL_REASON_CLOSE = "close_requested"
CANCEL_REASON_SEGMENT_DEADLINE = "segment_deadline"
CANCEL_REASON_HEARTBEAT_FAILURE = "heartbeat_failure"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RunCancelHandle:
    """Cancellation signal shared between manage_run and segment workers."""

    event: asyncio.Event = field(default_factory=asyncio.Event)
    reason: Optional[str] = None


@dataclass(frozen=True)
class SessionCloseSettings:
    heartbeat_interval_sec: int
    heartbeat_stale_sec: int
    close_wait_timeout_sec: int
    segment_deadline_sec: int
    cancellation_warn_sec: int


@dataclass(frozen=True)
class CloseResult:
    status: str
    already_closed: bool
    discarded_holds: int
    runtime_deleted: bool
    workspace_deleted: bool
    workspace_deleted_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_session_close_settings() -> SessionCloseSettings:
    """Load and clamp close/heartbeat settings from config."""
    raw_interval = getattr(Config, "RUN_HEARTBEAT_INTERVAL_SEC", 5)
    raw_stale = getattr(Config, "RUN_HEARTBEAT_STALE_SEC", 300)
    raw_close_wait = getattr(Config, "CLOSE_WAIT_TIMEOUT_SEC", 10)
    raw_segment_deadline = getattr(Config, "RUN_SEGMENT_DEADLINE_SEC", 270)
    raw_cancel_warn = getattr(Config, "RUN_CANCELLATION_WARN_SEC", 5)
    try:
        interval = int(raw_interval)
    except (TypeError, ValueError):
        interval = 5
    try:
        stale = int(raw_stale)
    except (TypeError, ValueError):
        stale = 300
    try:
        close_wait = int(raw_close_wait)
    except (TypeError, ValueError):
        close_wait = 10
    try:
        segment_deadline = int(raw_segment_deadline)
    except (TypeError, ValueError):
        segment_deadline = 270
    try:
        cancel_warn = int(raw_cancel_warn)
    except (TypeError, ValueError):
        cancel_warn = 5
    interval = max(1, interval)
    stale = max(interval + 1, stale)
    close_wait = max(1, close_wait)
    segment_deadline = max(1, segment_deadline)
    cancel_warn = max(1, cancel_warn)
    return SessionCloseSettings(
        heartbeat_interval_sec=interval,
        heartbeat_stale_sec=stale,
        close_wait_timeout_sec=close_wait,
        segment_deadline_sec=segment_deadline,
        cancellation_warn_sec=cancel_warn,
    )


class SessionCloseService:
    """Coordinates persisted close lifecycle with pod-local cancellation."""

    def __init__(
        self,
        *,
        lifecycle: Optional[RunLifecycleService] = None,
        registry: Optional[RunRegistry] = None,
        settings: Optional[SessionCloseSettings] = None,
    ) -> None:
        self._lifecycle = lifecycle or run_lifecycle_service
        self._registry = registry or run_registry
        self._settings = settings or resolve_session_close_settings()

    @property
    def settings(self) -> SessionCloseSettings:
        return self._settings

    async def close_execution(self, execution_id: uuid.UUID) -> CloseResult:
        return await self._close_execution_scope(execution_id)

    async def close_thread(self, thread_id: str) -> CloseResult:
        return await self._close_thread_scope(thread_id)

    async def reconcile_execution_close(self, execution_id: uuid.UUID) -> CloseResult:
        return await self._finalize_if_ready(execution_id=execution_id)

    async def reconcile_thread_close(self, thread_id: str) -> CloseResult:
        return await self._finalize_if_ready(thread_id=thread_id)

    @asynccontextmanager
    async def manage_run(
        self,
        *,
        run_pk: uuid.UUID,
        execution_id: uuid.UUID,
        task: asyncio.Task,
        thread_id: Optional[str] = None,
        thread_key: Optional[str] = None,
    ) -> AsyncIterator[RunCancelHandle]:
        cancel_handle = RunCancelHandle()
        key = thread_key or (thread_key_for(thread_id) if thread_id else None)
        await self._registry.register(
            run_pk=run_pk,
            execution_id=execution_id,
            task=task,
            cancel_event=cancel_handle.event,
            thread_key=key,
        )
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(
                run_pk=run_pk,
                execution_id=execution_id,
                task=task,
                cancel_handle=cancel_handle,
                thread_id=thread_id,
                thread_key=key,
            )
        )
        segment_watchdog = asyncio.create_task(
            self._segment_deadline_watchdog(
                cancel_handle=cancel_handle,
                task=task,
                deadline_sec=self._settings.segment_deadline_sec,
                run_pk=run_pk,
                execution_id=execution_id,
            )
        )
        try:
            yield cancel_handle
        finally:
            segment_watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await segment_watchdog
            try:
                await self._await_worker_with_diagnostics(
                    task,
                    run_pk=run_pk,
                    execution_id=execution_id,
                    cancel_reason=cancel_handle.reason,
                )
            finally:
                # The liveness signal must outlive the work it describes. A
                # cancellation-resistant worker therefore keeps its claim fresh
                # until it has actually stopped.
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
                await self._registry.unregister(run_pk)

    async def _segment_deadline_watchdog(
        self,
        *,
        cancel_handle: RunCancelHandle,
        task: asyncio.Task,
        deadline_sec: int,
        run_pk: uuid.UUID,
        execution_id: uuid.UUID,
    ) -> None:
        try:
            await asyncio.sleep(deadline_sec)
            if task.done():
                return
            cancel_handle.reason = CANCEL_REASON_SEGMENT_DEADLINE
            cancel_handle.event.set()
            task.cancel()
            await self._warn_if_cancellation_slow(
                task,
                run_pk=run_pk,
                execution_id=execution_id,
                cancel_reason=CANCEL_REASON_SEGMENT_DEADLINE,
            )
        except asyncio.CancelledError:
            raise

    async def _warn_if_cancellation_slow(
        self,
        task: asyncio.Task,
        *,
        run_pk: uuid.UUID,
        execution_id: uuid.UUID,
        cancel_reason: str,
    ) -> None:
        await asyncio.sleep(self._settings.cancellation_warn_sec)
        if task.done():
            return
        logger.error(
            "Worker cancellation exceeded diagnostic threshold: "
            "run_pk=%s execution_id=%s cancel_reason=%s elapsed_sec=%s",
            run_pk,
            execution_id,
            cancel_reason,
            self._settings.cancellation_warn_sec,
            extra={
                "run_pk": str(run_pk),
                "execution_id": str(execution_id),
                "cancel_reason": cancel_reason,
                "elapsed_sec": self._settings.cancellation_warn_sec,
                "event": "worker_cancellation_slow",
            },
        )

    async def _await_worker_with_diagnostics(
        self,
        task: asyncio.Task,
        *,
        run_pk: uuid.UUID,
        execution_id: uuid.UUID,
        cancel_reason: Optional[str],
    ) -> None:
        warning_task: Optional[asyncio.Task] = None
        was_done = task.done()
        if not was_done:
            if cancel_reason is None:
                cancel_reason = "unknown"
            task.cancel()
            warning_task = asyncio.create_task(
                self._warn_if_cancellation_slow(
                    task,
                    run_pk=run_pk,
                    execution_id=execution_id,
                    cancel_reason=cancel_reason,
                )
            )
        outer_cancel: Optional[asyncio.CancelledError] = None
        try:
            await task
        except asyncio.CancelledError as exc:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                outer_cancel = exc
        except Exception:
            if not was_done:
                logger.exception(
                    "Worker task raised after cancellation for run_pk=%s execution_id=%s",
                    run_pk,
                    execution_id,
                )
        finally:
            if warning_task is not None:
                warning_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await warning_task
        if outer_cancel is not None:
            raise outer_cancel

    async def start_heartbeat(
        self,
        *,
        run_pk: uuid.UUID,
        execution_id: uuid.UUID,
        task: asyncio.Task,
        cancel_event: asyncio.Event,
        thread_id: Optional[str] = None,
        thread_key: Optional[str] = None,
    ) -> asyncio.Task:
        key = thread_key or (thread_key_for(thread_id) if thread_id else None)
        cancel_handle = RunCancelHandle(event=cancel_event)
        return asyncio.create_task(
            self._heartbeat_loop(
                run_pk=run_pk,
                execution_id=execution_id,
                task=task,
                cancel_handle=cancel_handle,
                thread_id=thread_id,
                thread_key=key,
            )
        )

    async def _heartbeat_loop(
        self,
        *,
        run_pk: uuid.UUID,
        execution_id: uuid.UUID,
        task: asyncio.Task,
        cancel_handle: RunCancelHandle,
        thread_id: Optional[str],
        thread_key: Optional[str],
    ) -> None:
        interval = self._settings.heartbeat_interval_sec
        warning_task: Optional[asyncio.Task] = None
        try:
            while not task.done():
                try:
                    should_cancel = False
                    async with get_session() as session:
                        if await self._lifecycle.is_execution_close_requested(
                            session, execution_id
                        ):
                            should_cancel = True
                        elif thread_id or thread_key:
                            if await self._lifecycle.is_thread_close_requested(
                                session,
                                thread_id=thread_id,
                                thread_key=thread_key,
                            ):
                                should_cancel = True
                        await self._lifecycle.heartbeat_run(session, run_pk)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Heartbeat attempt failed for run_pk=%s execution_id=%s",
                        run_pk,
                        execution_id,
                    )
                    if cancel_handle.reason is None:
                        cancel_handle.reason = CANCEL_REASON_HEARTBEAT_FAILURE
                        cancel_handle.event.set()
                        if not task.done():
                            task.cancel()
                            warning_task = asyncio.create_task(
                                self._warn_if_cancellation_slow(
                                    task,
                                    run_pk=run_pk,
                                    execution_id=execution_id,
                                    cancel_reason=CANCEL_REASON_HEARTBEAT_FAILURE,
                                )
                            )
                    await asyncio.sleep(interval)
                    continue
                if should_cancel and cancel_handle.reason is None:
                    cancel_handle.reason = CANCEL_REASON_CLOSE
                    cancel_handle.event.set()
                    if not task.done():
                        task.cancel()
                        warning_task = asyncio.create_task(
                            self._warn_if_cancellation_slow(
                                task,
                                run_pk=run_pk,
                                execution_id=execution_id,
                                cancel_reason=CANCEL_REASON_CLOSE,
                            )
                        )
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        finally:
            if warning_task is not None:
                warning_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await warning_task

    async def _close_execution_scope(self, execution_id: uuid.UUID) -> CloseResult:
        async with get_session() as session:
            execution = await self._load_execution(session, execution_id)
            if execution is None:
                return CloseResult(
                    status="closed",
                    already_closed=True,
                    discarded_holds=0,
                    runtime_deleted=False,
                    workspace_deleted=False,
                    workspace_deleted_count=0,
                )
            if execution.closed_at is not None:
                await self._reconcile_stale_runs(session, execution_id=execution_id)
                return await self._reconcile_already_closed_execution(
                    session,
                    execution,
                    discarded_holds=0,
                    already_closed=True,
                )
            await self._lifecycle.mark_execution_close_requested(session, execution_id)
            await self._lifecycle.mark_active_runs_closing_for_execution(
                session, execution_id
            )
            discarded = await execution_state_service.abandon_active_holds_for_execution(
                session, execution_id
            )

        await self._registry.request_cancel_for_execution(execution_id)
        return await self._wait_and_finalize(
            execution_id=execution_id,
            discarded_holds=discarded,
            already_closed=False,
        )

    async def _close_thread_scope(self, thread_id: str) -> CloseResult:
        thread_key = thread_key_for(thread_id)
        async with get_session() as session:
            row = await self._lifecycle.get_thread_session(session, thread_key)
            if row is not None and row.closed_at is not None:
                await self._reconcile_stale_runs(session, thread_key=thread_key)
                return await self._reconcile_already_closed_thread(
                    session,
                    thread_id=thread_id,
                    thread_key=thread_key,
                    discarded_holds=0,
                    already_closed=True,
                )
            await self._lifecycle.mark_thread_close_requested(session, thread_id=thread_id)
            await self._lifecycle.mark_active_runs_closing_for_thread(
                session, thread_id=thread_id
            )
            discarded = await execution_state_service.abandon_active_holds_for_thread(
                session, thread_id
            )

        await self._registry.request_cancel_for_thread(thread_key)
        return await self._wait_and_finalize(
            thread_id=thread_id,
            thread_key=thread_key,
            discarded_holds=discarded,
            already_closed=False,
        )

    async def _wait_and_finalize(
        self,
        *,
        execution_id: Optional[uuid.UUID] = None,
        thread_id: Optional[str] = None,
        thread_key: Optional[str] = None,
        discarded_holds: int,
        already_closed: bool,
    ) -> CloseResult:
        deadline = asyncio.get_running_loop().time() + self._settings.close_wait_timeout_sec
        while asyncio.get_running_loop().time() < deadline:
            async with get_session() as session:
                await self._reconcile_stale_runs(
                    session,
                    execution_id=execution_id,
                    thread_key=thread_key,
                )
                blocking = await self._lifecycle.list_non_stale_active_runs(
                    session,
                    stale_sec=self._settings.heartbeat_stale_sec,
                    execution_id=execution_id,
                    thread_key=thread_key,
                )
            if not blocking:
                break
            await asyncio.sleep(_POLL_INTERVAL_SEC)

        async with get_session() as session:
            await self._reconcile_stale_runs(
                session,
                execution_id=execution_id,
                thread_key=thread_key,
            )
            blocking = await self._lifecycle.list_non_stale_active_runs(
                session,
                stale_sec=self._settings.heartbeat_stale_sec,
                execution_id=execution_id,
                thread_key=thread_key,
            )
            if blocking:
                return CloseResult(
                    status="closing",
                    already_closed=already_closed,
                    discarded_holds=discarded_holds,
                    runtime_deleted=False,
                    workspace_deleted=False,
                    workspace_deleted_count=0,
                )
            return await self._finalize_scope(
                session,
                execution_id=execution_id,
                thread_id=thread_id,
                thread_key=thread_key,
                discarded_holds=discarded_holds,
                already_closed=already_closed,
            )

    async def _finalize_if_ready(
        self,
        *,
        execution_id: Optional[uuid.UUID] = None,
        thread_id: Optional[str] = None,
    ) -> CloseResult:
        thread_key = thread_key_for(thread_id) if thread_id else None
        async with get_session() as session:
            if execution_id is not None:
                execution = await self._load_execution(session, execution_id)
                if execution is None or execution.close_requested_at is None:
                    return CloseResult(
                        status="closed",
                        already_closed=True,
                        discarded_holds=0,
                        runtime_deleted=False,
                        workspace_deleted=False,
                        workspace_deleted_count=0,
                    )
                if execution.closed_at is not None:
                    await self._reconcile_stale_runs(session, execution_id=execution_id)
                    return await self._reconcile_already_closed_execution(
                        session,
                        execution,
                        discarded_holds=0,
                        already_closed=True,
                    )
            elif thread_id is not None:
                row = await self._lifecycle.get_thread_session(session, thread_key or "")
                if row is None or row.close_requested_at is None:
                    return CloseResult(
                        status="closed",
                        already_closed=True,
                        discarded_holds=0,
                        runtime_deleted=False,
                        workspace_deleted=False,
                        workspace_deleted_count=0,
                    )
                if row.closed_at is not None:
                    await self._reconcile_stale_runs(session, thread_key=thread_key)
                    return await self._reconcile_already_closed_thread(
                        session,
                        thread_id=thread_id,
                        thread_key=thread_key or "",
                        discarded_holds=0,
                        already_closed=True,
                    )
            else:
                raise ValueError("execution_id or thread_id required")

            await self._reconcile_stale_runs(
                session,
                execution_id=execution_id,
                thread_key=thread_key,
            )
            blocking = await self._lifecycle.list_non_stale_active_runs(
                session,
                stale_sec=self._settings.heartbeat_stale_sec,
                execution_id=execution_id,
                thread_key=thread_key,
            )
            if blocking:
                return CloseResult(
                    status="closing",
                    already_closed=False,
                    discarded_holds=0,
                    runtime_deleted=False,
                    workspace_deleted=False,
                    workspace_deleted_count=0,
                )
            return await self._finalize_scope(
                session,
                execution_id=execution_id,
                thread_id=thread_id,
                thread_key=thread_key,
                discarded_holds=0,
                already_closed=False,
            )

    async def _finalize_scope(
        self,
        session: AsyncSession,
        *,
        execution_id: Optional[uuid.UUID],
        thread_id: Optional[str],
        thread_key: Optional[str],
        discarded_holds: int,
        already_closed: bool,
    ) -> CloseResult:
        if execution_id is not None:
            execution = await self._load_execution(session, execution_id)
            if execution is None:
                return CloseResult(
                    status="closed",
                    already_closed=True,
                    discarded_holds=discarded_holds,
                    runtime_deleted=False,
                    workspace_deleted=False,
                    workspace_deleted_count=0,
                )
            await self._terminalize_execution_status_for_close(session, execution)
            runtime_result, ws_result = self._cleanup_execution_paths(execution)
            if not self._required_cleanup_complete(runtime_result, ws_result):
                return self._closing_result(
                    discarded_holds=discarded_holds,
                    already_closed=already_closed,
                    runtime_result=runtime_result,
                    ws_result=ws_result,
                )
            await self._lifecycle.mark_execution_closed(session, execution.id)
            execution.closed_at = _utcnow()
            return self._closed_result(
                discarded_holds=discarded_holds,
                already_closed=already_closed,
                runtime_result=runtime_result,
                ws_result=ws_result,
            )

        key = thread_key or (thread_key_for(thread_id) if thread_id else None)
        if not thread_id or not key:
            return CloseResult(
                status="closed",
                already_closed=already_closed,
                discarded_holds=discarded_holds,
                runtime_deleted=False,
                workspace_deleted=False,
                workspace_deleted_count=0,
            )

        executions = await self._lifecycle.list_executions_for_thread(
            session, thread_id=thread_id
        )
        workspace_deleted_count = 0
        cleanup_complete = True
        for execution in executions:
            if execution.origin != "agui":
                continue
            await self._terminalize_execution_status_for_close(session, execution)
            ws_result = cleanup_execution_sandbox(
                execution.id,
                config=execution.config,
                workspace_path=execution.workspace_path,
                origin=execution.origin,
                allow_agui_origin=True,
            )
            workspace_deleted_count += ws_result.count
            if not ws_result.complete:
                cleanup_complete = False
                continue
            if execution.closed_at is None:
                await self._lifecycle.mark_execution_closed(session, execution.id)
                execution.closed_at = _utcnow()

        if not cleanup_complete:
            return CloseResult(
                status="closing",
                already_closed=already_closed,
                discarded_holds=discarded_holds,
                runtime_deleted=False,
                workspace_deleted=workspace_deleted_count > 0,
                workspace_deleted_count=workspace_deleted_count,
            )

        runtime_result = delete_thread_runtime(thread_id)
        if not runtime_result.complete:
            return CloseResult(
                status="closing",
                already_closed=already_closed,
                discarded_holds=discarded_holds,
                runtime_deleted=False,
                workspace_deleted=workspace_deleted_count > 0,
                workspace_deleted_count=workspace_deleted_count,
            )

        await self._lifecycle.mark_thread_closed(session, key)
        return CloseResult(
            status="closed",
            already_closed=already_closed,
            discarded_holds=discarded_holds,
            runtime_deleted=runtime_result.outcome
            in (CleanupOutcome.SUCCESS, CleanupOutcome.ABSENT),
            workspace_deleted=workspace_deleted_count > 0,
            workspace_deleted_count=workspace_deleted_count,
        )

    async def _reconcile_already_closed_execution(
        self,
        session: AsyncSession,
        execution: Execution,
        *,
        discarded_holds: int,
        already_closed: bool,
    ) -> CloseResult:
        runtime_result, ws_result = self._cleanup_execution_paths(execution)
        if not self._required_cleanup_complete(runtime_result, ws_result):
            return self._closing_result(
                discarded_holds=discarded_holds,
                already_closed=already_closed,
                runtime_result=runtime_result,
                ws_result=ws_result,
            )
        return self._closed_result(
            discarded_holds=discarded_holds,
            already_closed=already_closed,
            runtime_result=runtime_result,
            ws_result=ws_result,
        )

    async def _reconcile_already_closed_thread(
        self,
        session: AsyncSession,
        *,
        thread_id: str,
        thread_key: str,
        discarded_holds: int,
        already_closed: bool,
    ) -> CloseResult:
        executions = await self._lifecycle.list_executions_for_thread(
            session, thread_id=thread_id
        )
        workspace_deleted_count = 0
        cleanup_complete = True
        for execution in executions:
            if execution.origin != "agui":
                continue
            ws_result = cleanup_execution_sandbox(
                execution.id,
                config=execution.config,
                workspace_path=execution.workspace_path,
                origin=execution.origin,
                allow_agui_origin=True,
            )
            workspace_deleted_count += ws_result.count
            if not ws_result.complete:
                cleanup_complete = False

        runtime_result = delete_thread_runtime(thread_id)
        if not runtime_result.complete:
            cleanup_complete = False

        status = "closed" if cleanup_complete else "closing"
        return CloseResult(
            status=status,
            already_closed=already_closed,
            discarded_holds=discarded_holds,
            runtime_deleted=runtime_result.outcome
            in (CleanupOutcome.SUCCESS, CleanupOutcome.ABSENT),
            workspace_deleted=workspace_deleted_count > 0,
            workspace_deleted_count=workspace_deleted_count,
        )

    @staticmethod
    def _cleanup_execution_paths(
        execution: Execution,
    ) -> tuple[RuntimeDeletionResult, WorkspaceCleanupResult]:
        ws_result = cleanup_execution_sandbox(
            execution.id,
            config=execution.config,
            workspace_path=execution.workspace_path,
            origin=execution.origin,
        )
        if ws_result.outcome is CleanupOutcome.FAILURE:
            runtime_path = runtime_root_for_execution(execution.id)
            existed = runtime_path.exists()
            return (
                RuntimeDeletionResult(
                    outcome=(
                        CleanupOutcome.ABSENT
                        if not existed
                        else CleanupOutcome.FAILURE
                    ),
                    runtime_path=str(runtime_path),
                    existed=existed,
                ),
                ws_result,
            )
        runtime_result = delete_execution_runtime(execution.id)
        return runtime_result, ws_result

    @staticmethod
    def _required_cleanup_complete(
        runtime_result: RuntimeDeletionResult,
        ws_result: WorkspaceCleanupResult,
    ) -> bool:
        return runtime_result.complete and ws_result.complete

    @staticmethod
    def _closed_result(
        *,
        discarded_holds: int,
        already_closed: bool,
        runtime_result: RuntimeDeletionResult,
        ws_result: WorkspaceCleanupResult,
    ) -> CloseResult:
        return CloseResult(
            status="closed",
            already_closed=already_closed,
            discarded_holds=discarded_holds,
            runtime_deleted=runtime_result.outcome
            in (CleanupOutcome.SUCCESS, CleanupOutcome.ABSENT),
            workspace_deleted=ws_result.outcome is CleanupOutcome.SUCCESS,
            workspace_deleted_count=ws_result.count,
        )

    @staticmethod
    def _closing_result(
        *,
        discarded_holds: int,
        already_closed: bool,
        runtime_result: RuntimeDeletionResult,
        ws_result: WorkspaceCleanupResult,
    ) -> CloseResult:
        return CloseResult(
            status="closing",
            already_closed=already_closed,
            discarded_holds=discarded_holds,
            runtime_deleted=runtime_result.outcome is CleanupOutcome.SUCCESS,
            workspace_deleted=ws_result.outcome is CleanupOutcome.SUCCESS,
            workspace_deleted_count=ws_result.count,
        )

    async def _reconcile_stale_runs(
        self,
        session: AsyncSession,
        *,
        execution_id: Optional[uuid.UUID] = None,
        thread_key: Optional[str] = None,
    ) -> int:
        cutoff = _utcnow() - timedelta(
            seconds=self._settings.heartbeat_stale_sec
        )
        stale_runs = await self._lifecycle.list_stale_runs(
            session,
            stale_sec=self._settings.heartbeat_stale_sec,
            execution_id=execution_id,
            thread_key=thread_key,
        )
        if not stale_runs:
            return 0
        failed_exec_ids: set[uuid.UUID] = set()
        count = 0
        for run in stale_runs:
            if await self._lifecycle.finish_run(
                session,
                run.id,
                status=RUN_STATUS_FAILED,
                preserve_completed_execution=True,
                heartbeat_before=cutoff,
            ):
                failed_exec_ids.add(run.execution_id)
                count += 1
        if failed_exec_ids:
            await session.execute(
                update(Execution)
                .where(Execution.id.in_(failed_exec_ids))
                .where(Execution.status != ExecutionStatus.COMPLETED)
                .values(error_message=SESSION_CLOSED_ERROR)
            )
        return count

    async def _terminalize_execution_status_for_close(
        self,
        session: AsyncSession,
        execution: Execution,
    ) -> None:
        """Parent execution row status terminalization without setting closed_at."""
        if execution.closed_at is not None:
            return
        if execution.status == ExecutionStatus.COMPLETED:
            return
        now = _utcnow()
        await session.execute(
            update(Execution)
            .where(Execution.id == execution.id)
            .where(Execution.closed_at.is_(None))
            .where(Execution.status != ExecutionStatus.COMPLETED)
            .values(
                status=ExecutionStatus.FAILED,
                error_message=SESSION_CLOSED_ERROR,
                completed_at=now,
                updated_at=now,
            )
        )
        execution.status = ExecutionStatus.FAILED
        execution.error_message = SESSION_CLOSED_ERROR
        execution.completed_at = now
        execution.updated_at = now

    async def _load_execution(
        self,
        session: AsyncSession,
        execution_id: uuid.UUID,
    ) -> Optional[Execution]:
        result = await session.execute(
            select(Execution).where(Execution.id == execution_id)
        )
        return result.scalar_one_or_none()


session_close_service = SessionCloseService()
