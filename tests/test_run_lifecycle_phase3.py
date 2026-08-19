# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Phase 3 lifecycle persistence: thread sessions, run associations, heartbeat."""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.models.bindings import SESSION_CLOSED, SESSION_CLOSING
from app.models.execution_models import (
    Execution,
    ExecutionRun,
    ExecutionStatus,
    LLMState,
    LLMStateStatus,
    ThreadSession,
)
from app.services.execution_state_service import ExecutionStateService
from app.services.run_lifecycle import (
    DEFAULT_HEARTBEAT_FRESH_SEC,
    DEFAULT_HEARTBEAT_STALE_SEC,
    RUN_STATUS_ACTIVE,
    RUN_STATUS_CLOSING,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RunLifecycleService,
    RunClaimError,
    origin_from_runtime_path,
    synthetic_historical_run_id,
    thread_key_for,
    thread_key_from_runtime_path,
)


LONG_THREAD_ID = "thread-" + ("x" * 200)
LONG_RUN_ID = "run-" + ("y" * 200)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/20250816_phase3_lifecycle_persistence.py"
)
_MIG_SPEC = importlib.util.spec_from_file_location(
    "phase3_lifecycle_migration",
    _MIGRATION_PATH,
)
assert _MIG_SPEC and _MIG_SPEC.loader
_migration = importlib.util.module_from_spec(_MIG_SPEC)
_MIG_SPEC.loader.exec_module(_migration)


def test_thread_key_matches_runtime_paths_digest():
    assert thread_key_for(LONG_THREAD_ID) == thread_key_for(LONG_THREAD_ID)
    assert len(thread_key_for(LONG_THREAD_ID)) == 64


def test_thread_key_from_runtime_path():
    digest = thread_key_for("my-thread")
    path = f"/tmp/workspaces/threads/{digest}/runtime"
    assert thread_key_from_runtime_path(path) == digest
    assert thread_key_from_runtime_path(path + "/") == digest
    assert thread_key_from_runtime_path("/other/path") is None


def test_origin_from_runtime_path():
    digest = thread_key_for("agui-thread")
    agui_path = f"/work/threads/{digest}/runtime"
    exec_id = uuid.uuid4()
    orch_path = f"/work/{exec_id}/runtime"
    assert origin_from_runtime_path(agui_path) == "agui"
    assert origin_from_runtime_path(orch_path) == "orchestrator"
    assert origin_from_runtime_path("/elsewhere") is None


def test_synthetic_historical_run_id_is_deterministic():
    exec_id = uuid.UUID("00000000-0000-0000-0000-000000000099")
    assert synthetic_historical_run_id(exec_id) == f"backfill:{exec_id}"


@pytest.mark.asyncio
async def test_list_executions_for_thread_does_not_distinct_json_columns():
    captured = {}

    class _Scalars:
        @staticmethod
        def all():
            return []

    class _Result:
        @staticmethod
        def scalars():
            return _Scalars()

    class _Session:
        @staticmethod
        async def execute(statement):
            captured["statement"] = statement
            return _Result()

    result = await RunLifecycleService().list_executions_for_thread(
        _Session(),
        thread_id="postgres-thread",
    )

    sql = str(captured["statement"].compile(dialect=postgresql.dialect()))
    assert result == []
    assert "SELECT DISTINCT" not in sql
    assert "IN (SELECT execution_runs.execution_id" in sql


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

    def exec_lock(self, execution_id: uuid.UUID) -> asyncio.Lock:
        if execution_id not in self._exec_locks:
            self._exec_locks[execution_id] = asyncio.Lock()
        return self._exec_locks[execution_id]

    def thread_lock(self, thread_key: str) -> asyncio.Lock:
        if thread_key not in self._thread_locks:
            self._thread_locks[thread_key] = asyncio.Lock()
        return self._thread_locks[thread_key]


class ConcurrentLifecycleSession:
    """Async session shim with row-lock serialization for concurrent claim tests."""

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
                lock = self.store.exec_lock(exec_id)
                await lock.acquire()
                self._held_locks.append(lock)
            elif table == "thread_sessions":
                key = params.get("thread_key_1")
                lock = self.store.thread_lock(key)
                await lock.acquire()
                self._held_locks.append(lock)

        try:
            if table == "thread_sessions" and stmt.is_select:
                return self._select_thread_sessions(params)
            if table == "execution_runs" and stmt.is_select:
                if "status IN" in text or "status in" in text:
                    return self._select_active_execution_runs(params, text)
                return self._select_all_execution_runs(params, text)
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

    def _select_thread_sessions(self, params):
        key = params.get("thread_key_1")
        row = self.store.thread_sessions.get(key)
        return _ScalarResult([row] if row else [])

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

    def _select_all_execution_runs(self, params, text):
        rows = list(self.store.execution_runs.values())
        thread_key = params.get("thread_key_1")
        if thread_key is not None:
            rows = [r for r in rows if r.thread_key == thread_key]
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
            rows = [self.store.executions[eid] for eid in exec_ids if eid in self.store.executions]
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


def test_ensure_thread_session_stores_full_thread_id():
    async def run():
        store = InMemoryLifecycleStore()
        session = ConcurrentLifecycleSession(store)
        svc = RunLifecycleService()

        row = await svc.ensure_thread_session(session, LONG_THREAD_ID)
        assert row.thread_id == LONG_THREAD_ID
        assert row.thread_key == thread_key_for(LONG_THREAD_ID)
        assert len(row.thread_id) > 64

    asyncio.run(run())


def test_closed_thread_and_execution_reject_requests():
    async def run():
        store = InMemoryLifecycleStore()
        exec_id = uuid.uuid4()
        execution = Execution(
            id=exec_id,
            status=ExecutionStatus.COMPLETED,
            closed_at=datetime.now(timezone.utc),
        )
        store.add_execution(execution)
        key = thread_key_for(LONG_THREAD_ID)
        store.thread_sessions[key] = ThreadSession(
            thread_key=key,
            thread_id=LONG_THREAD_ID,
            close_requested_at=datetime.now(timezone.utc),
            closed_at=datetime.now(timezone.utc),
        )
        session = ConcurrentLifecycleSession(store)
        svc = RunLifecycleService()

        assert await svc.is_thread_close_requested(session, thread_id=LONG_THREAD_ID)
        assert await svc.is_execution_close_requested(session, exec_id)
        assert await svc.reject_if_close_requested(
            session, execution_id=exec_id, thread_id=LONG_THREAD_ID
        )

    asyncio.run(run())


def test_mark_execution_close_requested_stamps_completed_execution():
    async def run():
        store = InMemoryLifecycleStore()
        exec_id = uuid.uuid4()
        execution = Execution(id=exec_id, status=ExecutionStatus.COMPLETED)
        store.add_execution(execution)
        session = ConcurrentLifecycleSession(store)
        svc = RunLifecycleService()

        assert await svc.mark_execution_close_requested(session, exec_id) is True
        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.close_requested_at is not None
        assert await svc.mark_execution_close_requested(session, exec_id) is False

    asyncio.run(run())


def test_concurrent_active_run_claims_serialized():
    async def run():
        store = InMemoryLifecycleStore()
        exec_id = uuid.uuid4()
        store.add_execution(Execution(id=exec_id, status=ExecutionStatus.RUNNING))
        svc = RunLifecycleService()

        async def attempt(run_suffix: str):
            async with store.claim_lock:
                session = ConcurrentLifecycleSession(store)
                return await svc.try_create_active_run(
                    session,
                    execution_id=exec_id,
                    run_id=f"run-{run_suffix}",
                    thread_id=LONG_THREAD_ID,
                )

        results = await asyncio.gather(
            attempt("a"),
            attempt("b"),
            attempt("c"),
        )
        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        assert winners[0].status == RUN_STATUS_ACTIVE
        assert len(store.execution_runs) == 1

    asyncio.run(run())


def test_close_committed_before_claim_rejects_without_inserting_run():
    async def run():
        store = InMemoryLifecycleStore()
        exec_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        store.add_execution(
            Execution(
                id=exec_id,
                status=ExecutionStatus.COMPLETED,
                close_requested_at=now,
                closed_at=now,
            )
        )
        key = thread_key_for(LONG_THREAD_ID)
        store.thread_sessions[key] = ThreadSession(
            thread_key=key,
            thread_id=LONG_THREAD_ID,
            close_requested_at=now,
            closed_at=now,
        )
        session = ConcurrentLifecycleSession(store)
        svc = RunLifecycleService()

        with pytest.raises(RunClaimError) as exc:
            await svc.try_create_active_run(
                session,
                execution_id=exec_id,
                run_id="blocked-run",
                thread_id=LONG_THREAD_ID,
            )
        assert exc.value.code == SESSION_CLOSED
        assert len(store.execution_runs) == 0

    asyncio.run(run())


def test_execution_closed_claim_raises_session_closed():
    async def run():
        store = InMemoryLifecycleStore()
        exec_id = uuid.uuid4()
        store.add_execution(
            Execution(
                id=exec_id,
                status=ExecutionStatus.COMPLETED,
                closed_at=datetime.now(timezone.utc),
            )
        )
        session = ConcurrentLifecycleSession(store)
        svc = RunLifecycleService()

        with pytest.raises(RunClaimError) as exc:
            await svc.try_create_active_run(
                session,
                execution_id=exec_id,
                run_id="run-closed",
            )
        assert exc.value.code == SESSION_CLOSED
        assert len(store.execution_runs) == 0

    asyncio.run(run())


def test_execution_closing_claim_raises_session_closing():
    async def run():
        store = InMemoryLifecycleStore()
        exec_id = uuid.uuid4()
        store.add_execution(
            Execution(
                id=exec_id,
                status=ExecutionStatus.RUNNING,
                close_requested_at=datetime.now(timezone.utc),
            )
        )
        session = ConcurrentLifecycleSession(store)
        svc = RunLifecycleService()

        with pytest.raises(RunClaimError) as exc:
            await svc.try_create_active_run(
                session,
                execution_id=exec_id,
                run_id="run-closing",
            )
        assert exc.value.code == SESSION_CLOSING
        assert len(store.execution_runs) == 0

    asyncio.run(run())


def test_thread_closed_claim_raises_session_closed():
    async def run():
        store = InMemoryLifecycleStore()
        exec_id = uuid.uuid4()
        store.add_execution(Execution(id=exec_id, status=ExecutionStatus.RUNNING))
        key = thread_key_for(LONG_THREAD_ID)
        store.thread_sessions[key] = ThreadSession(
            thread_key=key,
            thread_id=LONG_THREAD_ID,
            closed_at=datetime.now(timezone.utc),
        )
        session = ConcurrentLifecycleSession(store)
        svc = RunLifecycleService()

        with pytest.raises(RunClaimError) as exc:
            await svc.try_create_active_run(
                session,
                execution_id=exec_id,
                run_id="run-thread-closed",
                thread_id=LONG_THREAD_ID,
            )
        assert exc.value.code == SESSION_CLOSED
        assert len(store.execution_runs) == 0

    asyncio.run(run())


def test_thread_closing_claim_raises_session_closing():
    async def run():
        store = InMemoryLifecycleStore()
        exec_id = uuid.uuid4()
        store.add_execution(Execution(id=exec_id, status=ExecutionStatus.RUNNING))
        key = thread_key_for(LONG_THREAD_ID)
        store.thread_sessions[key] = ThreadSession(
            thread_key=key,
            thread_id=LONG_THREAD_ID,
            close_requested_at=datetime.now(timezone.utc),
        )
        session = ConcurrentLifecycleSession(store)
        svc = RunLifecycleService()

        with pytest.raises(RunClaimError) as exc:
            await svc.try_create_active_run(
                session,
                execution_id=exec_id,
                run_id="run-thread-closing",
                thread_id=LONG_THREAD_ID,
            )
        assert exc.value.code == SESSION_CLOSING
        assert len(store.execution_runs) == 0

    asyncio.run(run())


def test_active_run_conflict_still_returns_none():
    async def run():
        store = InMemoryLifecycleStore()
        exec_id = uuid.uuid4()
        store.add_execution(Execution(id=exec_id, status=ExecutionStatus.RUNNING))
        now = datetime.now(timezone.utc)
        existing = ExecutionRun(
            id=uuid.uuid4(),
            execution_id=exec_id,
            thread_key=thread_key_for(LONG_THREAD_ID),
            run_id="existing",
            status=RUN_STATUS_ACTIVE,
            heartbeat_at=now,
            started_at=now,
        )
        store.execution_runs[existing.id] = existing
        session = ConcurrentLifecycleSession(store)
        svc = RunLifecycleService()

        result = await svc.try_create_active_run(
            session,
            execution_id=exec_id,
            run_id="new-run",
            thread_id=LONG_THREAD_ID,
        )
        assert result is None
        assert len(store.execution_runs) == 1

    asyncio.run(run())


def test_long_run_id_accepted_for_active_run_claim():
    async def run():
        store = InMemoryLifecycleStore()
        exec_id = uuid.uuid4()
        store.add_execution(Execution(id=exec_id, status=ExecutionStatus.RUNNING))
        session = ConcurrentLifecycleSession(store)
        svc = RunLifecycleService()

        run_row = await svc.try_create_active_run(
            session,
            execution_id=exec_id,
            run_id=LONG_RUN_ID,
            thread_id=LONG_THREAD_ID,
        )
        assert run_row is not None
        assert run_row.run_id == LONG_RUN_ID
        assert len(run_row.run_id) > 64

    asyncio.run(run())


def test_migration_widens_llm_state_run_id_to_text():
    upgrade_source = _MIGRATION_PATH.read_text()
    assert 'op.alter_column(\n        "llm_states",\n        "run_id"' in upgrade_source
    assert "type_=sa.Text()" in upgrade_source
    downgrade_source = upgrade_source.split("def downgrade():")[1]
    assert '"run_id"' in downgrade_source
    assert "type_=sa.String(length=64)" in downgrade_source


def test_non_stale_active_run_blocks_cleanup():
    async def run():
        store = InMemoryLifecycleStore()
        exec_id = uuid.uuid4()
        store.add_execution(Execution(id=exec_id, status=ExecutionStatus.RUNNING))
        session = ConcurrentLifecycleSession(store)
        svc = RunLifecycleService()
        now = datetime.now(timezone.utc)

        active = ExecutionRun(
            id=uuid.uuid4(),
            execution_id=exec_id,
            thread_key=thread_key_for("t1"),
            run_id="live",
            status=RUN_STATUS_ACTIVE,
            heartbeat_at=now - timedelta(seconds=30),
            started_at=now - timedelta(minutes=1),
        )
        stale = ExecutionRun(
            id=uuid.uuid4(),
            execution_id=exec_id,
            thread_key=thread_key_for("t2"),
            run_id="dead",
            status=RUN_STATUS_ACTIVE,
            heartbeat_at=now - timedelta(seconds=DEFAULT_HEARTBEAT_STALE_SEC + 60),
            started_at=now - timedelta(minutes=20),
        )
        store.execution_runs[active.id] = active
        store.execution_runs[stale.id] = stale

        blocking = await svc.list_non_stale_active_runs(session)
        stale_runs = await svc.list_stale_runs(session)
        assert {r.run_id for r in blocking} == {"live"}
        assert {r.run_id for r in stale_runs} == {"dead"}
        assert DEFAULT_HEARTBEAT_FRESH_SEC == DEFAULT_HEARTBEAT_STALE_SEC

    asyncio.run(run())


def test_mark_stale_runs_failed_preserves_completed_execution():
    async def run():
        store = InMemoryLifecycleStore()
        exec_id = uuid.uuid4()
        execution = Execution(id=exec_id, status=ExecutionStatus.COMPLETED)
        store.add_execution(execution)
        session = ConcurrentLifecycleSession(store)
        svc = RunLifecycleService()
        now = datetime.now(timezone.utc)
        run_row = ExecutionRun(
            id=uuid.uuid4(),
            execution_id=exec_id,
            run_id="stale-run",
            status=RUN_STATUS_ACTIVE,
            heartbeat_at=now - timedelta(seconds=DEFAULT_HEARTBEAT_STALE_SEC + 5),
            started_at=now - timedelta(minutes=5),
        )
        store.execution_runs[run_row.id] = run_row

        count = await svc.mark_stale_runs_failed(session, stale_sec=DEFAULT_HEARTBEAT_STALE_SEC)
        assert count == 1
        assert run_row.status == RUN_STATUS_FAILED
        assert execution.status == ExecutionStatus.COMPLETED

    asyncio.run(run())


def test_finish_run_record_only_preserves_parent_execution_status():
    async def run():
        store = InMemoryLifecycleStore()
        session = ConcurrentLifecycleSession(store)
        svc = RunLifecycleService()
        now = datetime.now(timezone.utc)

        awaiting_exec_id = uuid.uuid4()
        awaiting_execution = Execution(
            id=awaiting_exec_id,
            status=ExecutionStatus.AWAITING_RESPONSE,
        )
        store.add_execution(awaiting_execution)
        awaiting_run = ExecutionRun(
            id=uuid.uuid4(),
            execution_id=awaiting_exec_id,
            run_id="awaiting-run",
            status=RUN_STATUS_ACTIVE,
            heartbeat_at=now,
            started_at=now,
        )
        store.execution_runs[awaiting_run.id] = awaiting_run

        completed_exec_id = uuid.uuid4()
        completed_execution = Execution(
            id=completed_exec_id,
            status=ExecutionStatus.COMPLETED,
        )
        store.add_execution(completed_execution)
        completed_run = ExecutionRun(
            id=uuid.uuid4(),
            execution_id=completed_exec_id,
            run_id="completed-run",
            status=RUN_STATUS_ACTIVE,
            heartbeat_at=now,
            started_at=now,
        )
        store.execution_runs[completed_run.id] = completed_run

        assert await svc.finish_run(
            session,
            awaiting_run.id,
            status=RUN_STATUS_COMPLETED,
            update_execution_status=False,
        )
        assert awaiting_run.status == RUN_STATUS_COMPLETED
        assert awaiting_run.finished_at is not None
        assert awaiting_execution.status == ExecutionStatus.AWAITING_RESPONSE

        assert await svc.finish_run(
            session,
            completed_run.id,
            status=RUN_STATUS_FAILED,
            update_execution_status=False,
        )
        assert completed_run.status == RUN_STATUS_FAILED
        assert completed_run.finished_at is not None
        assert completed_execution.status == ExecutionStatus.COMPLETED

    asyncio.run(run())


def test_mark_active_runs_closing_for_thread_and_execution():
    async def run():
        store = InMemoryLifecycleStore()
        exec_id = uuid.uuid4()
        store.add_execution(Execution(id=exec_id, status=ExecutionStatus.RUNNING))
        key = thread_key_for(LONG_THREAD_ID)
        now = datetime.now(timezone.utc)
        run_row = ExecutionRun(
            id=uuid.uuid4(),
            execution_id=exec_id,
            thread_key=key,
            run_id="r1",
            status=RUN_STATUS_ACTIVE,
            heartbeat_at=now,
            started_at=now,
        )
        store.execution_runs[run_row.id] = run_row
        session = ConcurrentLifecycleSession(store)
        svc = RunLifecycleService()

        assert await svc.mark_active_runs_closing_for_thread(session, thread_id=LONG_THREAD_ID) == 1
        assert run_row.status == RUN_STATUS_CLOSING
        assert run_row.close_requested_at is not None
        assert await svc.mark_active_runs_closing_for_execution(session, exec_id) == 0

    asyncio.run(run())


def test_list_executions_and_runs_for_thread():
    async def run():
        store = InMemoryLifecycleStore()
        exec_id = uuid.uuid4()
        execution = Execution(id=exec_id, status=ExecutionStatus.COMPLETED)
        store.add_execution(execution)
        key = thread_key_for(LONG_THREAD_ID)
        now = datetime.now(timezone.utc)
        run_row = ExecutionRun(
            id=uuid.uuid4(),
            execution_id=exec_id,
            thread_key=key,
            run_id="r1",
            status=RUN_STATUS_COMPLETED,
            heartbeat_at=now,
            started_at=now,
            finished_at=now,
        )
        store.execution_runs[run_row.id] = run_row
        session = ConcurrentLifecycleSession(store)
        svc = RunLifecycleService()

        runs = await svc.list_runs_for_thread(session, thread_id=LONG_THREAD_ID)
        execs = await svc.list_executions_for_thread(session, thread_id=LONG_THREAD_ID)
        assert len(runs) == 1
        assert len(execs) == 1
        assert execs[0].id == exec_id

    asyncio.run(run())


def test_abandon_active_holds_for_execution():
    async def run():
        store = InMemoryLifecycleStore()
        exec_id = uuid.uuid4()
        store.add_execution(
            Execution(id=exec_id, status=ExecutionStatus.AWAITING_RESPONSE)
        )
        state_id = uuid.uuid4()
        store.llm_states[state_id] = LLMState(
            id=state_id,
            execution_id=exec_id,
            status=LLMStateStatus.AWAITING_RESPONSE,
            state_payload={"messages": []},
        )
        session = ConcurrentLifecycleSession(store)
        svc = ExecutionStateService()

        count = await svc.abandon_active_holds_for_execution(session, exec_id)
        assert count == 1
        assert store.llm_states[state_id].status == LLMStateStatus.DISCARDED

    asyncio.run(run())


def test_create_state_sets_thread_key_for_long_thread_id():
    async def run():
        svc = ExecutionStateService()
        captured = []

        class FakeSession:
            async def flush(self):
                return None

            def add(self, state):
                captured.append(state)

        exec_id = uuid.uuid4()
        await svc.create_state(
            FakeSession(),
            execution_id=exec_id,
            payload={"messages": []},
            thread_id=LONG_THREAD_ID,
        )
        assert captured[0].thread_id == LONG_THREAD_ID
        assert captured[0].thread_key == thread_key_for(LONG_THREAD_ID)

    asyncio.run(run())


def test_migration_llm_state_run_status_classification():
    assert _migration._llm_state_run_status("pending") == ("active", False)
    assert _migration._llm_state_run_status("awaiting_response") == ("completed", True)
    assert _migration._llm_state_run_status("completed") == ("completed", True)
    assert _migration._llm_state_run_status("discarded") == ("failed", True)


def test_migration_execution_historical_run_status_classification():
    assert _migration._execution_historical_run_status("running") == ("active", False)
    assert _migration._execution_historical_run_status("pending") == ("active", False)
    assert _migration._execution_historical_run_status("completed") == ("completed", True)
    assert _migration._execution_historical_run_status("failed") == ("failed", True)
    assert _migration._execution_historical_run_status("awaiting_response") == (
        "completed",
        True,
    )


def test_migration_local_helpers_match_service_digest():
    digest = thread_key_for(LONG_THREAD_ID)
    path = f"/work/threads/{digest}/runtime"
    assert _migration._thread_key_for(LONG_THREAD_ID) == digest
    assert _migration._thread_key_from_runtime_path(path) == digest
    assert _migration._origin_from_runtime_path(path) == "agui"
    exec_id = uuid.uuid4()
    assert _migration._origin_from_runtime_path(f"/work/{exec_id}/runtime") == "orchestrator"
    assert _migration._recovered_thread_id(digest) == f"recovered:{digest}"
    assert _migration._synthetic_historical_run_id(exec_id) == f"backfill:{exec_id}"


def test_migration_dedupe_logic_terminalizes_older_active_rows():
    now = datetime.now(timezone.utc)

    class FakeConnection:
        def __init__(self):
            self.runs = {
                uuid.UUID("00000000-0000-0000-0000-000000000001"): {
                    "execution_id": uuid.UUID("00000000-0000-0000-0000-000000000010"),
                    "thread_key": "abc",
                    "status": "active",
                    "finished_at": None,
                    "heartbeat_at": now - timedelta(hours=2),
                    "started_at": now - timedelta(hours=2),
                },
                uuid.UUID("00000000-0000-0000-0000-000000000002"): {
                    "execution_id": uuid.UUID("00000000-0000-0000-0000-000000000010"),
                    "thread_key": "abc",
                    "status": "active",
                    "finished_at": None,
                    "heartbeat_at": now - timedelta(minutes=5),
                    "started_at": now - timedelta(minutes=5),
                },
            }
            self.updates = []

        def execute(self, stmt, params=None):
            sql = str(stmt)
            if "GROUP BY execution_id" in sql:
                return _FakeResult(
                    [(uuid.UUID("00000000-0000-0000-0000-000000000010"),)]
                )
            if "GROUP BY thread_key" in sql:
                return _FakeResult([])
            if "ORDER BY heartbeat_at DESC" in sql:
                rows = [
                    (rid,)
                    for rid, run in sorted(
                        self.runs.items(),
                        key=lambda item: item[1]["heartbeat_at"],
                        reverse=True,
                    )
                    if run["execution_id"] == params["value"]
                ]
                return _FakeResult(rows)
            if "SET status = 'completed'" in sql:
                rid = params["id"]
                self.runs[rid]["status"] = "completed"
                self.runs[rid]["finished_at"] = params["now"]
                self.updates.append(rid)
                return _FakeResult([])
            return _FakeResult([])

    conn = FakeConnection()
    _migration._dedupe_active_execution_runs(conn, now)
    assert conn.runs[uuid.UUID("00000000-0000-0000-0000-000000000002")]["status"] == "active"
    assert conn.runs[uuid.UUID("00000000-0000-0000-0000-000000000001")]["status"] == "completed"
    assert len(conn.updates) == 1


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None
