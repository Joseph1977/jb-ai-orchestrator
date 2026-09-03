# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Phase 3 session close coordinator and run registry tests."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.execution_models import (
    Execution,
    ExecutionRun,
    ExecutionStatus,
    LLMState,
    LLMStateStatus,
    ThreadSession,
)
from app.services.run_lifecycle import (
    RUN_STATUS_ACTIVE,
    RUN_STATUS_CLOSING,
    RUN_STATUS_FAILED,
    RunLifecycleService,
    thread_key_for,
)
from app.services.run_registry import RunRegistry
from app.services.runtime_paths import ensure_runtime, runtime_root_for_thread
from app.services.session_close_service import (
    SESSION_CLOSED_ERROR,
    SessionCloseService,
    SessionCloseSettings,
)
from app.services.workspace_manager import cleanup_execution_sandbox


THREAD_ID = "thread-close-" + ("y" * 120)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryLifecycleStore:
    def __init__(self):
        self.thread_sessions: dict[str, ThreadSession] = {}
        self.execution_runs: dict[uuid.UUID, ExecutionRun] = {}
        self.executions: dict[uuid.UUID, Execution] = {}
        self.llm_states: dict[uuid.UUID, LLMState] = {}
        self._exec_locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self.claim_lock = asyncio.Lock()

    def add_execution(self, execution: Execution) -> None:
        self.executions[execution.id] = execution


class _NullNested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0]

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def one_or_none(self):
        return self._rows[0] if self._rows else None

    def distinct(self):
        return self


class _RowResult:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _UpdateResult:
    def __init__(self, count):
        self.rowcount = count


class _ReturningResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class ConcurrentLifecycleSession:
    """Minimal async session shim for close coordinator tests."""

    def __init__(self, store: InMemoryLifecycleStore):
        self.store = store
        self._held_locks: list[asyncio.Lock] = []

    def get_bind(self):
        return None

    def _table_name(self, stmt) -> str | None:
        if hasattr(stmt, "table") and stmt.table is not None:
            return stmt.table.name
        text = str(stmt)
        if "FROM executions" in text:
            return "executions"
        for name in ("thread_sessions", "execution_runs", "executions", "llm_states"):
            if name in text:
                return name
        return None

    async def execute(self, stmt):
        compiled = stmt.compile()
        params = compiled.params
        table = self._table_name(stmt)
        text = str(stmt)

        if "FOR UPDATE" in text.upper():
            if table == "executions":
                exec_id = params.get("id_1")
                lock = self.store._exec_locks.setdefault(exec_id, asyncio.Lock())
                await lock.acquire()
                self._held_locks.append(lock)
            elif table == "thread_sessions":
                key = params.get("thread_key_1")
                lock = self.store._thread_locks.setdefault(key, asyncio.Lock())
                await lock.acquire()
                self._held_locks.append(lock)

        try:
            if table == "thread_sessions" and stmt.is_select:
                key = params.get("thread_key_1")
                row = self.store.thread_sessions.get(key)
                return _ScalarResult([row] if row else [])
            if table == "execution_runs" and stmt.is_select:
                if "status IN" in text or "status in" in text:
                    return self._select_active_execution_runs(params, text)
                rows = list(self.store.execution_runs.values())
                thread_key = params.get("thread_key_1")
                if thread_key is not None:
                    rows = [r for r in rows if r.thread_key == thread_key]
                if "started_at" in text.lower() and "desc" in text.lower():
                    rows = sorted(rows, key=lambda r: r.started_at, reverse=True)
                return _ScalarResult(rows)
            if table == "executions" and stmt.is_select:
                if "FOR UPDATE" in text.upper():
                    exec_id = params.get("id_1")
                    row = self.store.executions.get(exec_id)
                    return _ScalarResult([row] if row else [])
                return self._select_executions(params, text)
            if table == "llm_states" and stmt.is_update:
                return self._update_llm_states(params)
            if table == "thread_sessions" and stmt.is_update:
                return self._update_thread_sessions(params)
            if table == "execution_runs" and stmt.is_update:
                result = self._update_execution_runs(params)
                if "RETURNING" in text.upper():
                    run_id = params.get("id_1")
                    row = self.store.execution_runs.get(run_id)
                    return _ReturningResult(row.execution_id if row else None)
                return result
            if table == "executions" and stmt.is_update:
                return self._update_executions(params)
            raise AssertionError(f"unsupported stmt: {stmt}")
        finally:
            self._release_held_locks()

    def begin_nested(self):
        return _NullNested()

    async def flush(self):
        self._release_held_locks()
        return None

    def _release_held_locks(self):
        while self._held_locks:
            lock = self._held_locks.pop()
            if lock.locked():
                lock.release()

    def add(self, obj):
        if isinstance(obj, ThreadSession):
            if obj.thread_key in self.store.thread_sessions:
                raise IntegrityError("", {}, Exception("duplicate thread"))
            self.store.thread_sessions[obj.thread_key] = obj
        elif isinstance(obj, ExecutionRun):
            if self._active_run_conflict(obj):
                raise IntegrityError("", {}, Exception("duplicate active run"))
            self.store.execution_runs[obj.id] = obj

    def _active_run_conflict(self, candidate: ExecutionRun) -> bool:
        for run in self.store.execution_runs.values():
            if run.finished_at is not None:
                continue
            if run.status not in ("active", "closing"):
                continue
            if run.execution_id == candidate.execution_id:
                return True
            if candidate.thread_key and run.thread_key == candidate.thread_key:
                return True
        return False

    def _select_active_execution_runs(self, params, text):
        rows = [
            r
            for r in self.store.execution_runs.values()
            if r.status in ("active", "closing") and r.finished_at is None
        ]
        exec_id = params.get("execution_id_1")
        thread_key = params.get("thread_key_1")
        if " OR " in text.upper():
            rows = [
                r
                for r in rows
                if (exec_id is not None and r.execution_id == exec_id)
                or (thread_key is not None and r.thread_key == thread_key)
            ]
        else:
            if exec_id is not None:
                rows = [r for r in rows if r.execution_id == exec_id]
            if thread_key is not None:
                rows = [r for r in rows if r.thread_key == thread_key]
        if "heartbeat_at_1" in params:
            cutoff = params["heartbeat_at_1"]
            if ">=" in text:
                rows = [r for r in rows if r.heartbeat_at >= cutoff]
            elif "<" in text:
                rows = [r for r in rows if r.heartbeat_at < cutoff]
        if "started_at" in text.lower() and "desc" in text.lower():
            rows = sorted(rows, key=lambda r: r.started_at, reverse=True)
        return _ScalarResult(rows)

    def _select_executions(self, params, text):
        exec_id = params.get("id_1")
        thread_key = params.get("thread_key_1")
        if (
            (
                "JOIN" in text.upper()
                or "IN (SELECT execution_runs.execution_id" in text
            )
            and thread_key is not None
        ):
            exec_ids = {
                r.execution_id
                for r in self.store.execution_runs.values()
                if r.thread_key == thread_key
            }
            rows = [
                self.store.executions[eid]
                for eid in exec_ids
                if eid in self.store.executions
            ]
            if "created_at" in text.lower() and "desc" in text.lower():
                rows = sorted(rows, key=lambda r: r.created_at, reverse=True)
            return _ScalarResult(rows)
        row = self.store.executions.get(exec_id)
        if row is None:
            return _RowResult(None)
        if "close_requested_at" in text and "closed_at" in text:
            return _RowResult((row.close_requested_at, row.closed_at))
        return _ScalarResult([row])

    def _update_thread_sessions(self, params):
        key = params.get("thread_key_1")
        row = self.store.thread_sessions.get(key)
        if row is None:
            return _UpdateResult(0)
        if row.closed_at is not None and "closed_at" in params:
            return _UpdateResult(0)
        if row.close_requested_at is not None and "close_requested_at" in params:
            return _UpdateResult(0)
        for field in ("close_requested_at", "closed_at", "updated_at"):
            if field in params:
                setattr(row, field, params[field])
        return _UpdateResult(1)

    def _update_execution_runs(self, params):
        run_id = params.get("id_1")
        exec_id = params.get("execution_id_1")
        thread_key = params.get("thread_key_1")
        required_status = params.get("status_1")
        if run_id:
            targets = [self.store.execution_runs.get(run_id)]
        else:
            targets = list(self.store.execution_runs.values())
        count = 0
        for row in targets:
            if row is None or row.finished_at is not None:
                continue
            if exec_id is not None and row.execution_id != exec_id:
                continue
            if thread_key is not None and row.thread_key != thread_key:
                continue
            if required_status is not None and row.status != required_status:
                continue
            for key, val in params.items():
                if key.endswith("_1"):
                    continue
                if hasattr(row, key):
                    setattr(row, key, val)
            count += 1
        return _UpdateResult(count)

    def _update_executions(self, params):
        exec_id = params.get("id_1")
        row = self.store.executions.get(exec_id)
        if row is None:
            return _UpdateResult(0)
        if row.closed_at is not None and "closed_at" in params:
            return _UpdateResult(0)
        if row.close_requested_at is not None and "close_requested_at" in params:
            return _UpdateResult(0)
        if (
            row.status == ExecutionStatus.COMPLETED
            and params.get("status") == ExecutionStatus.FAILED
        ):
            return _UpdateResult(0)
        for key, val in params.items():
            if key.endswith("_1"):
                continue
            if val is None:
                continue
            if hasattr(row, key):
                setattr(row, key, val)
        return _UpdateResult(1)

    def _update_llm_states(self, params):
        count = 0
        for state in self.store.llm_states.values():
            if params.get("execution_id_1") and state.execution_id != params["execution_id_1"]:
                continue
            if "status" in params:
                state.status = params["status"]
            count += 1
        return _UpdateResult(count)


@pytest.fixture
def ws_root(tmp_path, monkeypatch):
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setattr("app.config.Config.WORKSPACES_ROOT", str(root))
    monkeypatch.setattr("app.services.runtime_paths.Config.WORKSPACES_ROOT", str(root))
    monkeypatch.setattr("app.services.workspace_manager.Config.WORKSPACES_ROOT", str(root))
    return root


@pytest.fixture
def lifecycle_store():
    return InMemoryLifecycleStore()


@pytest.fixture
def close_settings():
    return SessionCloseSettings(
        heartbeat_interval_sec=1,
        heartbeat_stale_sec=60,
        close_wait_timeout_sec=0,
        segment_deadline_sec=270,
        cancellation_warn_sec=5,
    )


class ExtendedLifecycleSession(ConcurrentLifecycleSession):
    """Adds batch execution updates and llm_state selects for close tests."""

    async def execute(self, stmt):
        compiled = stmt.compile()
        params = compiled.params
        text = str(stmt)

        if "executions" in text.lower() and stmt.is_select and params.get("id_1") is not None:
            if (
                "JOIN" not in text.upper()
                and "IN (SELECT execution_runs.execution_id" not in text
                and (
                    "executions.id" in text
                    or "executions.status" in text
                )
            ):
                row = self.store.executions.get(params["id_1"])
                return _ScalarResult([row] if row else [])

        if "llm_states" in text.lower() and stmt.is_select:
            rows = list(self.store.llm_states.values())
            exec_id = params.get("execution_id_1")
            if exec_id is not None:
                rows = [r for r in rows if r.execution_id == exec_id]
            if "DISTINCT" in text.upper():
                seen = set()
                distinct_rows = []
                for row in rows:
                    if row.execution_id not in seen:
                        seen.add(row.execution_id)
                        distinct_rows.append(row.execution_id)
                return _ScalarResult(distinct_rows)
            return _ScalarResult(rows)

        if "executions" in text.lower() and stmt.is_update:
            id_param = params.get("id_1")
            if isinstance(id_param, list):
                count = 0
                for exec_id in id_param:
                    row = self.store.executions.get(exec_id)
                    if row is None:
                        continue
                    if row.status == ExecutionStatus.COMPLETED:
                        continue
                    for key, val in params.items():
                        if key.startswith("id_") or key.endswith("_1"):
                            continue
                        if val is None:
                            continue
                        if hasattr(row, key):
                            setattr(row, key, val)
                    count += 1
                return _UpdateResult(count)

        if "executions" in text.lower() and stmt.is_update and "id IN" in text.upper():
            count = 0
            target_ids = {
                val
                for key, val in params.items()
                if key.startswith("id_")
            }
            for exec_id in target_ids:
                row = self.store.executions.get(exec_id)
                if row is None:
                    continue
                if row.status == ExecutionStatus.COMPLETED and params.get("status") == ExecutionStatus.FAILED:
                    continue
                if "status_1" in params and row.status != params["status_1"]:
                    continue
                for key, val in params.items():
                    if key.startswith("id_") or key.endswith("_1"):
                        continue
                    if hasattr(row, key):
                        setattr(row, key, val)
                count += 1
            return _UpdateResult(count)

        return await super().execute(stmt)


@pytest.fixture
def session_factory(lifecycle_store):
    @asynccontextmanager
    async def _get_session():
        session = ExtendedLifecycleSession(lifecycle_store)
        try:
            yield session
        finally:
            pass

    return _get_session


@pytest.fixture
def close_service(session_factory, close_settings):
    registry = RunRegistry()
    svc = SessionCloseService(
        lifecycle=RunLifecycleService(),
        registry=registry,
        settings=close_settings,
    )
    with patch("app.services.session_close_service.get_session", session_factory):
        yield svc, registry, session_factory


def _orch_execution(
    ws_root: Path,
    *,
    execution_id: uuid.UUID | None = None,
    in_place: bool = False,
    with_output: bool = False,
) -> Execution:
    execution_id = execution_id or uuid.uuid4()
    sandbox = ws_root / str(execution_id) / "workspace"
    sandbox.mkdir(parents=True)
    (sandbox / "main.py").write_text("print('hi')", encoding="utf-8")
    config = {"inPlace": in_place, "legacyWritableInPlace": in_place}
    if with_output:
        config["output"] = {"type": "azure_blob", "uri": "https://example.blob/output"}
    return Execution(
        id=execution_id,
        status=ExecutionStatus.COMPLETED,
        origin="orchestrator",
        workspace_path=str(sandbox),
        config=config,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )


def _agui_execution(ws_root: Path, *, execution_id: uuid.UUID | None = None) -> Execution:
    execution_id = execution_id or uuid.uuid4()
    sandbox = ws_root / str(execution_id) / "workspace"
    sandbox.mkdir(parents=True)
    (sandbox / "app.py").write_text("agui", encoding="utf-8")
    return Execution(
        id=execution_id,
        status=ExecutionStatus.COMPLETED,
        origin="agui",
        workspace_path=str(sandbox),
        config={"inPlace": False},
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )


def _active_run(
    execution_id: uuid.UUID,
    *,
    stale: bool = False,
    thread_id: str | None = None,
) -> ExecutionRun:
    now = _utcnow()
    heartbeat = now - timedelta(seconds=120 if stale else 5)
    return ExecutionRun(
        id=uuid.uuid4(),
        execution_id=execution_id,
        thread_key=thread_key_for(thread_id) if thread_id else None,
        run_id=f"run-{uuid.uuid4().hex[:8]}",
        status=RUN_STATUS_ACTIVE,
        heartbeat_at=heartbeat,
        started_at=now - timedelta(minutes=1),
    )


def test_idempotent_execution_close(close_service, lifecycle_store, ws_root):
    async def run():
        svc, _, _ = close_service
        execution = _orch_execution(ws_root)
        lifecycle_store.add_execution(execution)
        execution.closed_at = _utcnow()
        execution.close_requested_at = _utcnow()

        first = await svc.close_execution(execution.id)
        second = await svc.close_execution(execution.id)
        assert first.status == "closed"
        assert first.already_closed is True
        assert second.already_closed is True
        assert not (ws_root / str(execution.id)).exists()

    asyncio.run(run())


def test_fresh_run_returns_closing_without_fs_deletion(close_service, lifecycle_store, ws_root):
    async def run():
        svc, _, _ = close_service
        execution = _orch_execution(ws_root)
        lifecycle_store.add_execution(execution)
        run_row = _active_run(execution.id, stale=False)
        lifecycle_store.execution_runs[run_row.id] = run_row
        runtime = Path(ensure_runtime(execution_id=str(execution.id)))
        (runtime / "todos.json").write_text("{}", encoding="utf-8")

        result = await svc.close_execution(execution.id)
        assert result.status == "closing"
        assert result.runtime_deleted is False
        assert result.workspace_deleted is False
        assert (ws_root / str(execution.id)).exists()
        assert runtime.exists()
        assert run_row.status == RUN_STATUS_CLOSING

    asyncio.run(run())


def test_stale_run_reconciles_to_closed(close_service, lifecycle_store, ws_root):
    async def run():
        svc, _, _ = close_service
        execution = _orch_execution(ws_root)
        lifecycle_store.add_execution(execution)
        run_row = _active_run(execution.id, stale=True)
        lifecycle_store.execution_runs[run_row.id] = run_row
        runtime = Path(ensure_runtime(execution_id=str(execution.id)))
        (runtime / "offload").mkdir()
        (runtime / "offload" / "x.txt").write_text("x", encoding="utf-8")

        result = await svc.close_execution(execution.id)
        assert result.status == "closed"
        assert result.runtime_deleted is True
        assert result.workspace_deleted is True
        assert not runtime.exists()
        assert not (ws_root / str(execution.id)).exists()
        assert run_row.status == RUN_STATUS_FAILED
        assert execution.closed_at is not None

    asyncio.run(run())


def test_completed_execution_status_preserved_on_stale_close(close_service, lifecycle_store, ws_root):
    async def run():
        svc, _, _ = close_service
        execution = _orch_execution(ws_root)
        execution.status = ExecutionStatus.COMPLETED
        lifecycle_store.add_execution(execution)
        run_row = _active_run(execution.id, stale=True)
        lifecycle_store.execution_runs[run_row.id] = run_row

        await svc.close_execution(execution.id)
        assert execution.status == ExecutionStatus.COMPLETED
        assert run_row.status == RUN_STATUS_FAILED

    asyncio.run(run())


def test_thread_close_deletes_all_agui_sandboxes(close_service, lifecycle_store, ws_root):
    async def run():
        svc, _, _ = close_service
        agui_a = _agui_execution(ws_root)
        agui_b = _agui_execution(ws_root)
        lifecycle_store.add_execution(agui_a)
        lifecycle_store.add_execution(agui_b)
        key = thread_key_for(THREAD_ID)
        now = _utcnow()
        for execution in (agui_a, agui_b):
            run_row = ExecutionRun(
                id=uuid.uuid4(),
                execution_id=execution.id,
                thread_key=key,
                run_id=f"done-{execution.id.hex[:6]}",
                status="completed",
                heartbeat_at=now,
                started_at=now,
                finished_at=now,
            )
            lifecycle_store.execution_runs[run_row.id] = run_row
        thread_runtime = runtime_root_for_thread(THREAD_ID)
        thread_runtime.mkdir(parents=True)
        (thread_runtime / "todos.json").write_text("{}", encoding="utf-8")

        result = await svc.close_thread(THREAD_ID)
        assert result.status == "closed"
        assert result.runtime_deleted is True
        assert result.workspace_deleted_count == 2
        assert not (ws_root / str(agui_a.id)).exists()
        assert not (ws_root / str(agui_b.id)).exists()
        assert not thread_runtime.exists()

    asyncio.run(run())


def test_thread_close_skips_orchestrator_sandbox(close_service, lifecycle_store, ws_root):
    async def run():
        svc, _, _ = close_service
        agui = _agui_execution(ws_root)
        orch = _orch_execution(ws_root)
        lifecycle_store.add_execution(agui)
        lifecycle_store.add_execution(orch)
        key = thread_key_for(THREAD_ID)
        now = _utcnow()
        for execution in (agui, orch):
            lifecycle_store.execution_runs[uuid.uuid4()] = ExecutionRun(
                id=uuid.uuid4(),
                execution_id=execution.id,
                thread_key=key,
                run_id=f"done-{execution.id.hex[:6]}",
                status="completed",
                heartbeat_at=now,
                started_at=now,
                finished_at=now,
            )
        # Fix duplicate key bug - use unique ids
        lifecycle_store.execution_runs.clear()
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

        result = await svc.close_thread(THREAD_ID)
        assert result.workspace_deleted_count == 1
        assert not (ws_root / str(agui.id)).exists()
        assert (ws_root / str(orch.id)).exists()

    asyncio.run(run())


def test_inplace_and_output_paths_untouched(ws_root):
    exec_id = uuid.uuid4()
    caller = ws_root / "caller-owned"
    caller.mkdir()
    (caller / "data.txt").write_text("keep", encoding="utf-8")
    config = {
        "inPlace": True,
        "legacyWritableInPlace": True,
        "output": {"type": "azure_blob", "uri": "https://example.blob/out"},
    }
    result = cleanup_execution_sandbox(
        exec_id,
        config=config,
        workspace_path=str(caller),
        origin="orchestrator",
    )
    assert result.outcome.value == "not_required"
    assert result.deleted is False
    assert caller.exists()


def test_bind_only_thread_close_without_runs(close_service, lifecycle_store, ws_root):
    async def run():
        svc, _, session_factory = close_service
        lifecycle = RunLifecycleService()
        async with session_factory() as session:
            await lifecycle.ensure_thread_session(session, THREAD_ID)
        thread_runtime = runtime_root_for_thread(THREAD_ID)
        thread_runtime.mkdir(parents=True)
        (thread_runtime / "notes.txt").write_text("bind-only", encoding="utf-8")

        result = await svc.close_thread(THREAD_ID)
        assert result.status == "closed"
        assert result.runtime_deleted is True
        assert result.workspace_deleted_count == 0
        key = thread_key_for(THREAD_ID)
        assert lifecycle_store.thread_sessions[key].closed_at is not None

    asyncio.run(run())


def test_retry_after_closing_reaches_closed(close_service, lifecycle_store, ws_root):
    async def run():
        svc, _, _ = close_service
        execution = _orch_execution(ws_root)
        lifecycle_store.add_execution(execution)
        run_row = _active_run(execution.id, stale=False)
        lifecycle_store.execution_runs[run_row.id] = run_row

        first = await svc.close_execution(execution.id)
        assert first.status == "closing"

        run_row.status = RUN_STATUS_FAILED
        run_row.finished_at = _utcnow()
        second = await svc.reconcile_execution_close(execution.id)
        assert second.status == "closed"
        assert second.workspace_deleted is True
        assert execution.closed_at is not None

    asyncio.run(run())


def test_registry_cancellation_by_execution():
    async def run():
        registry = RunRegistry()
        cancel_seen = asyncio.Event()

        async def worker():
            try:
                while True:
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                cancel_seen.set()
                raise

        task = asyncio.create_task(worker())
        cancel_event = asyncio.Event()
        exec_id = uuid.uuid4()
        run_pk = uuid.uuid4()
        await registry.register(
            run_pk=run_pk,
            execution_id=exec_id,
            task=task,
            cancel_event=cancel_event,
        )
        count = await registry.request_cancel_for_execution(exec_id)
        assert count == 1
        assert cancel_event.is_set()
        await asyncio.sleep(0.1)
        assert task.cancelled() or cancel_seen.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await registry.unregister(run_pk)

    asyncio.run(run())


def test_discard_holds_on_execution_close(close_service, lifecycle_store, ws_root):
    async def run():
        svc, _, _ = close_service
        execution = _orch_execution(ws_root)
        execution.status = ExecutionStatus.AWAITING_RESPONSE
        lifecycle_store.add_execution(execution)
        state_id = uuid.uuid4()
        lifecycle_store.llm_states[state_id] = LLMState(
            id=state_id,
            execution_id=execution.id,
            status=LLMStateStatus.AWAITING_RESPONSE,
            state_payload={"messages": []},
        )

        result = await svc.close_execution(execution.id)
        assert result.discarded_holds == 1
        assert lifecycle_store.llm_states[state_id].status == LLMStateStatus.DISCARDED

    asyncio.run(run())


def test_stale_close_sets_session_closed_error(close_service, lifecycle_store, ws_root):
    async def run():
        svc, _, _ = close_service
        execution = _orch_execution(ws_root)
        execution.status = ExecutionStatus.RUNNING
        lifecycle_store.add_execution(execution)
        run_row = _active_run(execution.id, stale=True)
        lifecycle_store.execution_runs[run_row.id] = run_row

        await svc.close_execution(execution.id)
        assert execution.status == ExecutionStatus.FAILED
        assert execution.error_message == SESSION_CLOSED_ERROR
        assert execution.closed_at is not None
        assert execution.completed_at is not None

    asyncio.run(run())


def test_execution_close_running_with_cancelled_run_terminalizes(close_service, lifecycle_store, ws_root):
    """Run finished by controller without execution status update -> FAILED on close."""
    async def run():
        svc, _, _ = close_service
        execution = _orch_execution(ws_root)
        execution.status = ExecutionStatus.RUNNING
        lifecycle_store.add_execution(execution)
        run_row = _active_run(execution.id, stale=True)
        run_row.status = RUN_STATUS_FAILED
        run_row.finished_at = _utcnow()
        lifecycle_store.execution_runs[run_row.id] = run_row

        await svc.close_execution(execution.id)

        assert execution.status == ExecutionStatus.FAILED
        assert execution.error_message == SESSION_CLOSED_ERROR
        assert execution.closed_at is not None
        assert execution.completed_at is not None

    asyncio.run(run())


def test_execution_close_completed_preserves_status_and_sets_closed_at(
    close_service, lifecycle_store, ws_root
):
    async def run():
        svc, _, _ = close_service
        execution = _orch_execution(ws_root)
        execution.status = ExecutionStatus.COMPLETED
        lifecycle_store.add_execution(execution)

        await svc.close_execution(execution.id)

        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.error_message is None
        assert execution.closed_at is not None

    asyncio.run(run())


def test_thread_close_running_agui_execution_terminalizes(close_service, lifecycle_store, ws_root):
    async def run():
        svc, _, _ = close_service
        agui = _agui_execution(ws_root)
        agui.status = ExecutionStatus.RUNNING
        lifecycle_store.add_execution(agui)
        key = thread_key_for(THREAD_ID)
        now = _utcnow()
        run_id = uuid.uuid4()
        lifecycle_store.execution_runs[run_id] = ExecutionRun(
            id=run_id,
            execution_id=agui.id,
            thread_key=key,
            run_id="cancelled-run",
            status=RUN_STATUS_FAILED,
            heartbeat_at=now,
            started_at=now,
            finished_at=now,
        )

        await svc.close_thread(THREAD_ID)

        assert agui.status == ExecutionStatus.FAILED
        assert agui.error_message == SESSION_CLOSED_ERROR
        assert agui.closed_at is not None
        assert lifecycle_store.thread_sessions[key].closed_at is not None

    asyncio.run(run())


def test_thread_close_completed_agui_preserves_status_and_sets_closed_at(
    close_service, lifecycle_store, ws_root
):
    async def run():
        svc, _, _ = close_service
        agui = _agui_execution(ws_root)
        agui.status = ExecutionStatus.COMPLETED
        lifecycle_store.add_execution(agui)
        key = thread_key_for(THREAD_ID)
        now = _utcnow()
        run_id = uuid.uuid4()
        lifecycle_store.execution_runs[run_id] = ExecutionRun(
            id=run_id,
            execution_id=agui.id,
            thread_key=key,
            run_id="done-run",
            status="completed",
            heartbeat_at=now,
            started_at=now,
            finished_at=now,
        )

        await svc.close_thread(THREAD_ID)

        assert agui.status == ExecutionStatus.COMPLETED
        assert agui.closed_at is not None
        assert lifecycle_store.thread_sessions[key].closed_at is not None

    asyncio.run(run())


def test_thread_close_leaves_orchestrator_execution_unterminalized(
    close_service, lifecycle_store, ws_root
):
    async def run():
        svc, _, _ = close_service
        agui = _agui_execution(ws_root)
        agui.status = ExecutionStatus.RUNNING
        orch = _orch_execution(ws_root)
        orch.status = ExecutionStatus.RUNNING
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
                run_id=f"run-{execution.id.hex[:6]}",
                status=RUN_STATUS_FAILED,
                heartbeat_at=now,
                started_at=now,
                finished_at=now,
            )

        await svc.close_thread(THREAD_ID)

        assert agui.status == ExecutionStatus.FAILED
        assert agui.closed_at is not None
        assert orch.status == ExecutionStatus.RUNNING
        assert orch.closed_at is None

    asyncio.run(run())


def test_resolve_session_close_settings_clamps():
    from app.services.session_close_service import resolve_session_close_settings
    from app.config import Config

    Config.RUN_HEARTBEAT_INTERVAL_SEC = 0
    Config.RUN_HEARTBEAT_STALE_SEC = 1
    Config.CLOSE_WAIT_TIMEOUT_SEC = 0
    settings = resolve_session_close_settings()
    assert settings.heartbeat_interval_sec >= 1
    assert settings.heartbeat_stale_sec > settings.heartbeat_interval_sec
    assert settings.close_wait_timeout_sec >= 1
