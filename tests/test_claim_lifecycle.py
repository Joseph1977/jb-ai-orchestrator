# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Safe claim lifecycle for resume holds (AWAITING -> PENDING -> COMPLETED/restore)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.models.execution_models import LLMStateStatus
from app.services.execution_state_service import ExecutionStateService


def _stmt_values(stmt):
    return stmt.compile().params


def test_try_claim_transitions_to_pending():
    async def run():
        svc = ExecutionStateService()
        updates = []

        class FakeResult:
            rowcount = 1

        class FakeSession:
            async def execute(self, stmt):
                updates.append(_stmt_values(stmt))
                return FakeResult()

        ok = await svc.try_claim_state_for_resume(
            FakeSession(), uuid.UUID("00000000-0000-0000-0000-000000000001")
        )
        assert ok is True
        assert updates[0]["status"] == LLMStateStatus.PENDING
        assert "updated_at" in updates[0]
        assert updates[0]["updated_at"] >= datetime.now(timezone.utc) - timedelta(seconds=2)

    asyncio.run(run())


def test_recover_stale_refreshes_updated_at():
    async def run():
        svc = ExecutionStateService()
        captured = []

        class FakeResult:
            rowcount = 1

        class FakeSession:
            async def execute(self, stmt):
                captured.append(_stmt_values(stmt))
                return FakeResult()

        await svc.recover_stale_pending_claims(
            FakeSession(), thread_id="thread-1", timeout_sec=300
        )
        assert "updated_at" in captured[0]
        assert captured[0]["updated_at"] >= datetime.now(timezone.utc) - timedelta(seconds=2)

    asyncio.run(run())


def test_complete_and_restore_claimed_state():
    async def run():
        svc = ExecutionStateService()
        statuses = []

        class FakeResult:
            rowcount = 1

        class FakeSession:
            async def execute(self, stmt):
                statuses.append(_stmt_values(stmt)["status"])
                return FakeResult()

        sid = uuid.UUID("00000000-0000-0000-0000-000000000001")
        assert await svc.complete_claimed_state(FakeSession(), sid) is True
        assert await svc.restore_claimed_state(FakeSession(), sid) is True
        assert statuses == [LLMStateStatus.COMPLETED, LLMStateStatus.AWAITING_RESPONSE]

    asyncio.run(run())


def test_rollback_partial_resume_settlement():
    async def run():
        svc = ExecutionStateService()
        statuses = []

        class FakeResult:
            rowcount = 1

        class FakeSession:
            async def execute(self, stmt):
                statuses.append(_stmt_values(stmt)["status"])
                return FakeResult()

        claimed = uuid.UUID("00000000-0000-0000-0000-000000000001")
        new = uuid.UUID("00000000-0000-0000-0000-000000000002")
        ok = await svc.rollback_partial_resume_settlement(
            FakeSession(), claimed_state_id=claimed, new_state_id=new
        )
        assert ok is True
        assert statuses == [LLMStateStatus.DISCARDED, LLMStateStatus.AWAITING_RESPONSE]

    asyncio.run(run())


def test_second_claim_on_pending_fails():
    store = {
        uuid.UUID("00000000-0000-0000-0000-000000000001"): SimpleNamespace(
            status=LLMStateStatus.PENDING
        )
    }

    async def run():
        svc = ExecutionStateService()

        class FakeResult:
            def __init__(self, count):
                self.rowcount = count

        class FakeSession:
            async def execute(self, stmt):
                # Simulate UPDATE ... WHERE status = AWAITING — no row matches PENDING.
                return FakeResult(0)

        ok = await svc.try_claim_state_for_resume(
            FakeSession(), uuid.UUID("00000000-0000-0000-0000-000000000001")
        )
        assert ok is False

    asyncio.run(run())
