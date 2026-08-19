# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Retryable close cleanup: failure leaves closing state, retry succeeds."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.execution_models import ExecutionRun, ExecutionStatus
from app.services.run_lifecycle import thread_key_for
from app.services.runtime_paths import (
    CleanupOutcome,
    RuntimeDeletionResult,
    ensure_runtime,
    runtime_root_for_execution,
    runtime_root_for_thread,
)
from app.services.workspace_manager import CleanupOutcome, cleanup_execution_sandbox

pytest_plugins = ["test_session_close_phase3"]

from test_session_close_phase3 import (  # noqa: E402
    THREAD_ID,
    _active_run,
    _agui_execution,
    _orch_execution,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fail_execution_sandbox_once(ws_root: Path):
    """Fail the first rmtree of a top-level execution sandbox root."""

    real = shutil.rmtree
    state = {"failed": False}
    root = ws_root.resolve()

    def _rmtree(path, *args, **kwargs):
        resolved = Path(path).resolve()
        if state["failed"] or not str(resolved).startswith(str(root)):
            return real(path, *args, **kwargs)
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            return real(path, *args, **kwargs)
        if len(rel.parts) == 1 and rel.parts[0] != "threads":
            state["failed"] = True
            raise OSError("simulated sandbox rmtree failure")
        return real(path, *args, **kwargs)

    return _rmtree


def test_execution_close_cleanup_failure_then_retry(
    close_service, lifecycle_store, ws_root, monkeypatch
):
    async def run():
        svc, _, _ = close_service
        execution = _orch_execution(ws_root)
        lifecycle_store.add_execution(execution)
        runtime = Path(ensure_runtime(execution_id=str(execution.id)))
        (runtime / "todos.json").write_text("{}", encoding="utf-8")
        sandbox = ws_root / str(execution.id)
        fail_sandbox = _fail_execution_sandbox_once(ws_root)
        monkeypatch.setattr("app.services.workspace_manager.shutil.rmtree", fail_sandbox)
        monkeypatch.setattr("app.services.runtime_paths.shutil.rmtree", fail_sandbox)

        first = await svc.close_execution(execution.id)
        assert first.status == "closing"
        assert execution.close_requested_at is not None
        assert execution.closed_at is None
        assert sandbox.exists()
        assert runtime.exists()

        second = await svc.reconcile_execution_close(execution.id)
        assert second.status == "closed"
        assert execution.closed_at is not None
        assert not sandbox.exists()
        assert not runtime.exists()

    asyncio.run(run())


def test_execution_close_runtime_failure_then_retry(
    close_service, lifecycle_store, ws_root, monkeypatch
):
    async def run():
        from app.services import session_close_service as scs
        from app.services.runtime_paths import delete_execution_runtime as real_delete
        from app.services.workspace_manager import WorkspaceCleanupResult

        svc, _, _ = close_service
        execution = _orch_execution(ws_root)
        lifecycle_store.add_execution(execution)
        runtime = Path(ensure_runtime(execution_id=str(execution.id)))
        (runtime / "todos.json").write_text("{}", encoding="utf-8")
        sandbox = ws_root / str(execution.id)
        state = {"failed": False}

        def absent_workspace_cleanup(*args, **kwargs):
            return WorkspaceCleanupResult(outcome=CleanupOutcome.ABSENT, count=0)

        def flaky_delete_execution_runtime(execution_id):
            runtime_path = runtime_root_for_execution(execution_id)
            if not state["failed"]:
                state["failed"] = True
                return RuntimeDeletionResult(
                    outcome=CleanupOutcome.FAILURE,
                    runtime_path=str(runtime_path),
                    existed=True,
                )
            return real_delete(execution_id)

        monkeypatch.setattr(scs, "cleanup_execution_sandbox", absent_workspace_cleanup)
        monkeypatch.setattr(
            scs, "delete_execution_runtime", flaky_delete_execution_runtime
        )

        first = await svc.close_execution(execution.id)
        assert first.status == "closing"
        assert execution.closed_at is None
        assert sandbox.exists()
        assert runtime.exists()

        second = await svc.reconcile_execution_close(execution.id)
        assert second.status == "closed"
        assert execution.closed_at is not None
        assert sandbox.exists()
        assert not runtime.exists()

    asyncio.run(run())


def test_execution_close_failure_preserves_completed_status(
    close_service, lifecycle_store, ws_root, monkeypatch
):
    async def run():
        svc, _, _ = close_service
        execution = _orch_execution(ws_root)
        execution.status = ExecutionStatus.COMPLETED
        lifecycle_store.add_execution(execution)
        Path(ensure_runtime(execution_id=str(execution.id)))

        def always_fail(path, *args, **kwargs):
            raise OSError("simulated rmtree failure")

        monkeypatch.setattr(
            "app.services.workspace_manager.shutil.rmtree", always_fail
        )

        result = await svc.close_execution(execution.id)
        assert result.status == "closing"
        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.closed_at is None

    asyncio.run(run())


def test_thread_close_cleanup_failure_then_retry(
    close_service, lifecycle_store, ws_root, monkeypatch
):
    async def run():
        svc, _, _ = close_service
        agui = _agui_execution(ws_root)
        orch = _orch_execution(ws_root)
        lifecycle_store.add_execution(agui)
        lifecycle_store.add_execution(orch)
        key = thread_key_for(THREAD_ID)
        now = _utcnow()
        for execution in (agui, orch):
            run_id = uuid.uuid4()
            lifecycle_store.execution_runs[run_id] = ExecutionRun(
                id=run_id,
                execution_id=execution.id,
                thread_key=key,
                run_id=f"done-{execution.id.hex[:6]}",
                status="completed",
                heartbeat_at=now,
                started_at=now,
                finished_at=now,
            )
        thread_runtime = runtime_root_for_thread(THREAD_ID)
        thread_runtime.mkdir(parents=True)
        (thread_runtime / "todos.json").write_text("{}", encoding="utf-8")
        agui_sandbox = ws_root / str(agui.id)
        orch_sandbox = ws_root / str(orch.id)

        fail_workspace = _fail_execution_sandbox_once(ws_root)
        monkeypatch.setattr(
            "app.services.workspace_manager.shutil.rmtree", fail_workspace
        )
        monkeypatch.setattr(
            "app.services.runtime_paths.shutil.rmtree", fail_workspace
        )

        first = await svc.close_thread(THREAD_ID)
        assert first.status == "closing"
        assert lifecycle_store.thread_sessions[key].closed_at is None
        assert agui.closed_at is None
        assert agui_sandbox.exists()
        assert orch_sandbox.exists()
        assert thread_runtime.exists()

        second = await svc.reconcile_thread_close(THREAD_ID)
        assert second.status == "closed"
        assert lifecycle_store.thread_sessions[key].closed_at is not None
        assert agui.closed_at is not None
        assert not agui_sandbox.exists()
        assert orch_sandbox.exists()
        assert not thread_runtime.exists()
        assert orch.closed_at is None

    asyncio.run(run())


def test_inplace_and_orchestrator_untouched_on_cleanup_failure(ws_root, monkeypatch):
    exec_id = uuid.uuid4()
    caller = ws_root / "caller-owned"
    caller.mkdir()
    (caller / "data.txt").write_text("keep", encoding="utf-8")
    orch_sandbox = ws_root / str(exec_id) / "workspace"
    orch_sandbox.mkdir(parents=True)
    (orch_sandbox / "main.py").write_text("x", encoding="utf-8")

    def always_fail(path, *args, **kwargs):
        raise OSError("simulated rmtree failure")

    monkeypatch.setattr("app.services.workspace_manager.shutil.rmtree", always_fail)

    in_place = cleanup_execution_sandbox(
        exec_id,
        config={"inPlace": True},
        workspace_path=str(caller),
        origin="orchestrator",
    )
    assert in_place.outcome is CleanupOutcome.NOT_REQUIRED
    assert caller.exists()

    orch = cleanup_execution_sandbox(
        exec_id,
        config={"inPlace": False},
        workspace_path=str(orch_sandbox),
        origin="orchestrator",
    )
    assert orch.outcome is CleanupOutcome.FAILURE
    assert orch_sandbox.exists()


def test_stale_close_failure_leaves_closing_without_closed_at(
    close_service, lifecycle_store, ws_root, monkeypatch
):
    async def run():
        svc, _, _ = close_service
        execution = _orch_execution(ws_root)
        execution.status = ExecutionStatus.RUNNING
        lifecycle_store.add_execution(execution)
        run_row = _active_run(execution.id, stale=True)
        lifecycle_store.execution_runs[run_row.id] = run_row
        Path(ensure_runtime(execution_id=str(execution.id)))

        def always_fail(path, *args, **kwargs):
            raise OSError("simulated rmtree failure")

        monkeypatch.setattr(
            "app.services.workspace_manager.shutil.rmtree", always_fail
        )

        result = await svc.close_execution(execution.id)
        assert result.status == "closing"
        assert execution.status == ExecutionStatus.FAILED
        assert execution.error_message == "session_closed"
        assert execution.closed_at is None
        assert (ws_root / str(execution.id)).exists()

    asyncio.run(run())
