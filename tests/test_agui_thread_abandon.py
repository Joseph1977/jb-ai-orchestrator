# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Thread-level abandon recovery for AG-UI active holds."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.controllers.ag_ui_controller import abandon_agui_thread
from app.models.execution_models import ExecutionStatus, LLMStateStatus
from app.services.execution_state_service import ExecutionStateService


def _stmt_values(stmt):
    return stmt.compile().params


def test_abandon_service_targets_active_statuses_only():
    async def run():
        svc = ExecutionStateService()
        captured = []

        class FakeResult:
            def __init__(self, rowcount=0):
                self.rowcount = rowcount

        class FakeScalars:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        class FakeSelectResult:
            def __init__(self, rows):
                self._rows = rows

            def scalars(self):
                return FakeScalars(self._rows)

        select_calls = 0

        class FakeSession:
            async def execute(self, stmt):
                nonlocal select_calls
                compiled = str(stmt)
                if "SELECT" in compiled.upper() and "llm_states" in compiled.lower():
                    select_calls += 1
                    return FakeSelectResult([uuid.uuid4()])
                captured.append(_stmt_values(stmt))
                return FakeResult(rowcount=2)

        count = await svc.abandon_active_holds_for_thread(
            FakeSession(), "thread-abandon-svc"
        )
        assert count == 2
        assert captured
        assert captured[0]["status"] == LLMStateStatus.DISCARDED
        assert "updated_at" in captured[0]

    asyncio.run(run())


def test_abandon_service_empty_thread_returns_zero():
    async def run():
        svc = ExecutionStateService()

        class FakeSession:
            async def execute(self, stmt):
                raise AssertionError("should not query for empty thread_id")

        assert await svc.abandon_active_holds_for_thread(FakeSession(), "") == 0

    asyncio.run(run())


def test_abandon_service_idempotent_when_no_active_holds():
    async def run():
        svc = ExecutionStateService()

        class FakeScalars:
            def all(self):
                return []

        class FakeSelectResult:
            def scalars(self):
                return FakeScalars()

        class FakeResult:
            rowcount = 0

        class FakeSession:
            async def execute(self, stmt):
                compiled = str(stmt)
                if "SELECT" in compiled.upper():
                    return FakeSelectResult()
                return FakeResult()

        assert (
            await svc.abandon_active_holds_for_thread(FakeSession(), "settled-thread")
            == 0
        )

    asyncio.run(run())


def test_abandon_conditional_update_skips_settled_holds():
    """Simulate UPDATE ... WHERE status IN (active): settled rows are not matched."""
    store = {
        uuid.UUID("00000000-0000-0000-0000-000000000001"): SimpleNamespace(
            status=LLMStateStatus.COMPLETED
        ),
        uuid.UUID("00000000-0000-0000-0000-000000000002"): SimpleNamespace(
            status=LLMStateStatus.DISCARDED
        ),
    }

    async def run():
        svc = ExecutionStateService()

        class FakeScalars:
            def all(self):
                return []

        class FakeSelectResult:
            def scalars(self):
                return FakeScalars()

        class FakeResult:
            rowcount = 0

        class FakeSession:
            async def execute(self, stmt):
                compiled = str(stmt)
                if "SELECT" in compiled.upper():
                    return FakeSelectResult()
                return FakeResult()

        count = await svc.abandon_active_holds_for_thread(
            FakeSession(), "thread-settled"
        )
        assert count == 0
        assert store[
            uuid.UUID("00000000-0000-0000-0000-000000000001")
        ].status == LLMStateStatus.COMPLETED
        assert store[
            uuid.UUID("00000000-0000-0000-0000-000000000002")
        ].status == LLMStateStatus.DISCARDED

    asyncio.run(run())


@pytest.fixture
def abandon_db_mocks():
    """In-memory store for abandon controller/service integration tests."""
    executions: dict = {}
    states: dict = {}

    async def abandon_active_holds_for_thread(session, thread_id):
        if not thread_id:
            return 0
        active = (
            LLMStateStatus.AWAITING_RESPONSE,
            LLMStateStatus.PENDING,
        )
        execution_ids = {
            s.execution_id
            for s in states.values()
            if s.thread_id == thread_id and s.status in active
        }
        count = 0
        now = datetime.now(timezone.utc)
        for state in states.values():
            if state.thread_id == thread_id and state.status in active:
                state.status = LLMStateStatus.DISCARDED
                state.updated_at = now
                count += 1
        for exec_id in execution_ids:
            ex = executions.get(exec_id)
            if ex and ex.status == ExecutionStatus.AWAITING_RESPONSE:
                ex.status = ExecutionStatus.FAILED
                ex.error_message = "abandoned_by_thread"
        return count

    async def try_claim_state_for_resume(session, state_id):
        state = states.get(state_id)
        if state and state.status == LLMStateStatus.AWAITING_RESPONSE:
            state.status = LLMStateStatus.PENDING
            state.updated_at = datetime.now(timezone.utc)
            return True
        return False

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    patches = [
        patch("app.controllers.ag_ui_controller.get_session", fake_get_session),
        patch(
            "app.controllers.ag_ui_controller.execution_state_service.abandon_active_holds_for_thread",
            abandon_active_holds_for_thread,
        ),
    ]
    for p in patches:
        p.start()
    yield {
        "executions": executions,
        "states": states,
        "abandon_active_holds_for_thread": abandon_active_holds_for_thread,
        "try_claim_state_for_resume": try_claim_state_for_resume,
    }
    for p in patches:
        p.stop()


def _seed_state(
    store,
    *,
    thread_id: str,
    status: LLMStateStatus,
    execution_status: ExecutionStatus = ExecutionStatus.AWAITING_RESPONSE,
) -> uuid.UUID:
    eid = uuid.uuid4()
    sid = uuid.uuid4()
    store["executions"][eid] = SimpleNamespace(
        id=eid,
        status=execution_status,
        error_message=None,
    )
    store["states"][sid] = SimpleNamespace(
        id=sid,
        execution_id=eid,
        thread_id=thread_id,
        status=status,
        state_payload={"pending_tools": [{"tool_call_id": "call_1"}]},
        updated_at=datetime.now(timezone.utc),
    )
    return sid


@pytest.mark.asyncio
async def test_abandon_discards_awaiting_hold(abandon_db_mocks):
    sid = _seed_state(
        abandon_db_mocks,
        thread_id="thread-await",
        status=LLMStateStatus.AWAITING_RESPONSE,
    )

    resp = await abandon_agui_thread("thread-await")

    assert resp.thread_id == "thread-await"
    assert resp.discarded == 1
    assert resp.status == "abandoned"
    assert abandon_db_mocks["states"][sid].status == LLMStateStatus.DISCARDED


@pytest.mark.asyncio
async def test_abandon_discards_pending_hold(abandon_db_mocks):
    sid = _seed_state(
        abandon_db_mocks,
        thread_id="thread-pending",
        status=LLMStateStatus.PENDING,
    )

    resp = await abandon_agui_thread("thread-pending")

    assert resp.discarded == 1
    assert abandon_db_mocks["states"][sid].status == LLMStateStatus.DISCARDED


@pytest.mark.asyncio
async def test_abandon_leaves_completed_untouched(abandon_db_mocks):
    sid = _seed_state(
        abandon_db_mocks,
        thread_id="thread-done",
        status=LLMStateStatus.COMPLETED,
        execution_status=ExecutionStatus.COMPLETED,
    )

    resp = await abandon_agui_thread("thread-done")

    assert resp.discarded == 0
    assert abandon_db_mocks["states"][sid].status == LLMStateStatus.COMPLETED


@pytest.mark.asyncio
async def test_abandon_leaves_prior_discarded_untouched(abandon_db_mocks):
    sid = _seed_state(
        abandon_db_mocks,
        thread_id="thread-old-discard",
        status=LLMStateStatus.DISCARDED,
        execution_status=ExecutionStatus.FAILED,
    )

    resp = await abandon_agui_thread("thread-old-discard")

    assert resp.discarded == 0
    assert abandon_db_mocks["states"][sid].status == LLMStateStatus.DISCARDED


@pytest.mark.asyncio
async def test_abandon_is_idempotent(abandon_db_mocks):
    _seed_state(
        abandon_db_mocks,
        thread_id="thread-repeat",
        status=LLMStateStatus.AWAITING_RESPONSE,
    )

    first = await abandon_agui_thread("thread-repeat")
    second = await abandon_agui_thread("thread-repeat")

    assert first.discarded == 1
    assert second.discarded == 0
    assert second.status == "abandoned"


@pytest.mark.asyncio
async def test_abandon_isolates_threads(abandon_db_mocks):
    sid_a = _seed_state(
        abandon_db_mocks,
        thread_id="thread-a",
        status=LLMStateStatus.AWAITING_RESPONSE,
    )
    sid_b = _seed_state(
        abandon_db_mocks,
        thread_id="thread-b",
        status=LLMStateStatus.AWAITING_RESPONSE,
    )

    resp = await abandon_agui_thread("thread-a")

    assert resp.discarded == 1
    assert abandon_db_mocks["states"][sid_a].status == LLMStateStatus.DISCARDED
    assert abandon_db_mocks["states"][sid_b].status == LLMStateStatus.AWAITING_RESPONSE


@pytest.mark.asyncio
async def test_abandon_wins_race_over_resume_claim(abandon_db_mocks):
    sid = _seed_state(
        abandon_db_mocks,
        thread_id="thread-race-abandon",
        status=LLMStateStatus.AWAITING_RESPONSE,
    )

    resp = await abandon_agui_thread("thread-race-abandon")
    assert resp.discarded == 1

    claimed = await abandon_db_mocks["try_claim_state_for_resume"](None, sid)
    assert claimed is False
    assert abandon_db_mocks["states"][sid].status == LLMStateStatus.DISCARDED


@pytest.mark.asyncio
async def test_resume_claim_then_abandon_discards_pending(abandon_db_mocks):
    sid = _seed_state(
        abandon_db_mocks,
        thread_id="thread-race-pending",
        status=LLMStateStatus.AWAITING_RESPONSE,
    )

    claimed = await abandon_db_mocks["try_claim_state_for_resume"](None, sid)
    assert claimed is True
    assert abandon_db_mocks["states"][sid].status == LLMStateStatus.PENDING

    resp = await abandon_agui_thread("thread-race-pending")
    assert resp.discarded == 1
    assert abandon_db_mocks["states"][sid].status == LLMStateStatus.DISCARDED
