# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import or_

from app.config import Config
from app.models.execution_models import (
    Execution,
    ExecutionStatus,
    LLMState,
    LLMStateStatus,
)
from app.services.run_lifecycle import thread_key_for


_UNSET = object()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _thread_scope(thread_id: Optional[str]):
    """Match llm_states by full thread_id or indexed thread_key."""
    if not thread_id:
        return None
    key = thread_key_for(thread_id)
    return or_(LLMState.thread_id == thread_id, LLMState.thread_key == key)


class ExecutionStateService:
    """Persistence layer for execution metadata and serialized LLM state."""

    async def create_execution(
        self,
        session: AsyncSession,
        *,
        status: ExecutionStatus = ExecutionStatus.PENDING,
        source: Optional[str] = None,
        workspace_path: Optional[str] = None,
        orchestration_type: Optional[str] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> Execution:
        execution = Execution(
            status=status,
            source=source,
            workspace_path=workspace_path,
            orchestration_type=orchestration_type,
            config=config,
        )
        session.add(execution)
        await session.flush()
        return execution

    async def update_execution(
        self,
        session: AsyncSession,
        execution_id: uuid.UUID,
        *,
        status: Optional[ExecutionStatus] = None,
        result: Optional[dict[str, Any]] | object = _UNSET,
        error_message: Optional[str] | object = _UNSET,
        source: Optional[str] = None,
        workspace_path: Optional[str] = None,
        orchestration_type: Optional[str] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> None:
        values: dict[str, Any] = {}
        if status is not None:
            values["status"] = status
            if status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED):
                values["completed_at"] = datetime.now(timezone.utc)
            else:
                values["completed_at"] = None
        if result is not _UNSET:
            values["result"] = result
        if error_message is not _UNSET:
            values["error_message"] = error_message
        if source is not None:
            values["source"] = source
        if workspace_path is not None:
            values["workspace_path"] = workspace_path
        if orchestration_type is not None:
            values["orchestration_type"] = orchestration_type
        if config is not None:
            values["config"] = config

        if not values:
            return

        await session.execute(
            update(Execution)
            .where(Execution.id == execution_id)
            .values(**values)
        )

    async def create_state(
        self,
        session: AsyncSession,
        *,
        execution_id: uuid.UUID,
        payload: dict[str, Any],
        status: LLMStateStatus = LLMStateStatus.PENDING,
        thread_id: Optional[str] = None,
        run_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> LLMState:
        state = LLMState(
            execution_id=execution_id,
            status=status,
            state_payload=payload,
            thread_id=thread_id,
            thread_key=thread_key_for(thread_id) if thread_id else None,
            run_id=run_id,
            tool_call_id=tool_call_id,
        )
        session.add(state)
        await session.flush()
        return state

    async def mark_state_status(
        self,
        session: AsyncSession,
        state_id: uuid.UUID,
        status: LLMStateStatus,
    ) -> None:
        await session.execute(
            update(LLMState)
            .where(LLMState.id == state_id)
            .values(status=status)
        )

    async def get_execution(
        self,
        session: AsyncSession,
        execution_id: uuid.UUID,
    ) -> Optional[Execution]:
        result = await session.execute(
            select(Execution).where(Execution.id == execution_id)
        )
        return result.scalar_one_or_none()

    async def get_latest_state_for_execution(
        self,
        session: AsyncSession,
        execution_id: uuid.UUID,
    ) -> Optional[LLMState]:
        result = await session.execute(
            select(LLMState)
            .where(LLMState.execution_id == execution_id)
            .order_by(LLMState.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_state(
        self,
        session: AsyncSession,
        state_id: uuid.UUID,
    ) -> Optional[LLMState]:
        result = await session.execute(
            select(LLMState).where(LLMState.id == state_id)
        )
        return result.scalar_one_or_none()

    async def get_resume_hold_by_tool_call_id(
        self,
        session: AsyncSession,
        tool_call_id: str,
        *,
        thread_id: Optional[str] = None,
    ) -> Optional[LLMState]:
        """Find an awaiting or in-progress (claimed) hold for resume by tool_call_id."""
        query = (
            select(LLMState)
            .where(
                LLMState.status.in_(
                    (LLMStateStatus.AWAITING_RESPONSE, LLMStateStatus.PENDING)
                )
            )
        )
        scope = _thread_scope(thread_id)
        if scope is not None:
            query = query.where(scope)
        query = query.order_by(LLMState.created_at.desc())
        result = await session.execute(query)
        states = result.scalars().all()
        for state in states:
            if state.tool_call_id == tool_call_id:
                return state
            payload = state.state_payload or {}
            pending = payload.get("pending_tools") or []
            if not pending and payload.get("pending_tool"):
                pending = [payload["pending_tool"]]
            for pt in pending:
                if pt.get("tool_call_id") == tool_call_id:
                    return state
                if pt.get("interrupt_id") == tool_call_id:
                    return state
        return None

    async def get_awaiting_state_by_tool_call_id(
        self,
        session: AsyncSession,
        tool_call_id: str,
        *,
        thread_id: Optional[str] = None,
    ) -> Optional[LLMState]:
        """Find awaiting state for a tool_call_id (primary column or batch payload)."""
        query = (
            select(LLMState)
            .where(LLMState.status == LLMStateStatus.AWAITING_RESPONSE)
        )
        scope = _thread_scope(thread_id)
        if scope is not None:
            query = query.where(scope)
        query = query.order_by(LLMState.created_at.desc())
        result = await session.execute(query)
        states = result.scalars().all()
        for state in states:
            if state.tool_call_id == tool_call_id:
                return state
            payload = state.state_payload or {}
            pending = payload.get("pending_tools") or []
            if not pending and payload.get("pending_tool"):
                pending = [payload["pending_tool"]]
            for pt in pending:
                if pt.get("tool_call_id") == tool_call_id:
                    return state
                if pt.get("interrupt_id") == tool_call_id:
                    return state
        return None

    async def get_latest_awaiting_state_for_thread(
        self,
        session: AsyncSession,
        thread_id: str,
    ) -> Optional[LLMState]:
        """Return the most recent awaiting state for a thread, if any."""
        if not thread_id:
            return None
        scope = _thread_scope(thread_id)
        if scope is None:
            return None
        result = await session.execute(
            select(LLMState)
            .where(scope)
            .where(LLMState.status == LLMStateStatus.AWAITING_RESPONSE)
            .order_by(LLMState.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_latest_pending_hold_for_thread(
        self,
        session: AsyncSession,
        thread_id: str,
    ) -> Optional[LLMState]:
        """Return the most recent in-progress (claimed) hold for a thread, if any."""
        if not thread_id:
            return None
        scope = _thread_scope(thread_id)
        if scope is None:
            return None
        result = await session.execute(
            select(LLMState)
            .where(scope)
            .where(LLMState.status == LLMStateStatus.PENDING)
            .order_by(LLMState.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def recover_stale_pending_claims(
        self,
        session: AsyncSession,
        *,
        thread_id: Optional[str] = None,
        execution_id: Optional[uuid.UUID] = None,
        timeout_sec: Optional[int] = None,
    ) -> int:
        """Atomically restore PENDING holds older than timeout to AWAITING_RESPONSE."""
        timeout = timeout_sec if timeout_sec is not None else Config.RESUME_CLAIM_TIMEOUT_SEC
        if timeout <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout)
        query = (
            update(LLMState)
            .where(LLMState.status == LLMStateStatus.PENDING)
            .where(LLMState.updated_at < cutoff)
        )
        scope = _thread_scope(thread_id) if thread_id is not None else None
        if scope is not None:
            query = query.where(scope)
        if execution_id is not None:
            query = query.where(LLMState.execution_id == execution_id)
        now = _utcnow()
        result = await session.execute(
            query.values(status=LLMStateStatus.AWAITING_RESPONSE, updated_at=now)
        )
        return int(result.rowcount or 0)

    async def try_claim_state_for_resume(
        self,
        session: AsyncSession,
        state_id: uuid.UUID,
    ) -> bool:
        """Atomically claim an awaiting hold for in-progress resume (AWAITING -> PENDING)."""
        now = _utcnow()
        result = await session.execute(
            update(LLMState)
            .where(LLMState.id == state_id)
            .where(LLMState.status == LLMStateStatus.AWAITING_RESPONSE)
            .values(status=LLMStateStatus.PENDING, updated_at=now)
        )
        return bool(result.rowcount)

    async def complete_claimed_state(
        self,
        session: AsyncSession,
        state_id: uuid.UUID,
    ) -> bool:
        """Mark a claimed (PENDING) hold completed after successful settlement."""
        now = _utcnow()
        result = await session.execute(
            update(LLMState)
            .where(LLMState.id == state_id)
            .where(LLMState.status == LLMStateStatus.PENDING)
            .values(status=LLMStateStatus.COMPLETED, updated_at=now)
        )
        return bool(result.rowcount)

    async def restore_claimed_state(
        self,
        session: AsyncSession,
        state_id: uuid.UUID,
    ) -> bool:
        """Return a claimed hold to awaiting when resume fails before settlement."""
        now = _utcnow()
        result = await session.execute(
            update(LLMState)
            .where(LLMState.id == state_id)
            .where(LLMState.status == LLMStateStatus.PENDING)
            .values(status=LLMStateStatus.AWAITING_RESPONSE, updated_at=now)
        )
        return bool(result.rowcount)

    async def rollback_partial_resume_settlement(
        self,
        session: AsyncSession,
        *,
        claimed_state_id: uuid.UUID,
        new_state_id: uuid.UUID,
    ) -> bool:
        """Discard a new awaiting hold and restore the claimed hold after failed completion."""
        now = _utcnow()
        await session.execute(
            update(LLMState)
            .where(LLMState.id == new_state_id)
            .values(status=LLMStateStatus.DISCARDED, updated_at=now)
        )
        result = await session.execute(
            update(LLMState)
            .where(LLMState.id == claimed_state_id)
            .where(LLMState.status == LLMStateStatus.PENDING)
            .values(status=LLMStateStatus.AWAITING_RESPONSE, updated_at=now)
        )
        return bool(result.rowcount)

    def pending_tool_call_ids(self, state_payload: dict) -> list[str]:
        pending = state_payload.get("pending_tools") or []
        if not pending and state_payload.get("pending_tool"):
            pending = [state_payload["pending_tool"]]
        return [str(pt["tool_call_id"]) for pt in pending if pt.get("tool_call_id")]

    async def discard_awaiting_states_for_thread(
        self,
        session: AsyncSession,
        thread_id: str,
    ) -> int:
        """Mark awaiting holds DISCARDED and terminalize parent executions."""
        if not thread_id:
            return 0
        scope = _thread_scope(thread_id)
        if scope is None:
            return 0
        result = await session.execute(
            select(LLMState)
            .where(scope)
            .where(LLMState.status == LLMStateStatus.AWAITING_RESPONSE)
        )
        states = result.scalars().all()
        execution_ids = {s.execution_id for s in states}
        if not states:
            return 0
        await session.execute(
            update(LLMState)
            .where(scope)
            .where(LLMState.status == LLMStateStatus.AWAITING_RESPONSE)
            .values(status=LLMStateStatus.DISCARDED)
        )
        for exec_id in execution_ids:
            await session.execute(
                update(Execution)
                .where(Execution.id == exec_id)
                .where(Execution.status == ExecutionStatus.AWAITING_RESPONSE)
                .values(
                    status=ExecutionStatus.FAILED,
                    error_message="discarded_by_fresh_run",
                    completed_at=datetime.now(timezone.utc),
                )
            )
        return len(states)

    async def abandon_active_holds_for_thread(
        self,
        session: AsyncSession,
        thread_id: str,
    ) -> int:
        """Explicit recovery: atomically discard active holds for a thread.

        Targets AWAITING_RESPONSE and in-progress (PENDING) resume claims only.
        Idempotent: repeated calls discard zero rows once holds are settled.
        """
        if not thread_id:
            return 0
        active_statuses = (
            LLMStateStatus.AWAITING_RESPONSE,
            LLMStateStatus.PENDING,
        )
        scope = _thread_scope(thread_id)
        if scope is None:
            return 0
        result = await session.execute(
            select(LLMState.execution_id)
            .where(scope)
            .where(LLMState.status.in_(active_statuses))
            .distinct()
        )
        execution_ids = set(result.scalars().all())
        now = _utcnow()
        update_result = await session.execute(
            update(LLMState)
            .where(scope)
            .where(LLMState.status.in_(active_statuses))
            .values(status=LLMStateStatus.DISCARDED, updated_at=now)
        )
        discarded = int(update_result.rowcount or 0)
        if not discarded:
            return 0
        for exec_id in execution_ids:
            await session.execute(
                update(Execution)
                .where(Execution.id == exec_id)
                .where(Execution.status == ExecutionStatus.AWAITING_RESPONSE)
                .values(
                    status=ExecutionStatus.FAILED,
                    error_message="abandoned_by_thread",
                    completed_at=now,
                )
            )
        return discarded

    async def abandon_active_holds_for_execution(
        self,
        session: AsyncSession,
        execution_id: uuid.UUID,
    ) -> int:
        """Explicit recovery: atomically discard active holds for one execution."""
        active_statuses = (
            LLMStateStatus.AWAITING_RESPONSE,
            LLMStateStatus.PENDING,
        )
        now = _utcnow()
        update_result = await session.execute(
            update(LLMState)
            .where(LLMState.execution_id == execution_id)
            .where(LLMState.status.in_(active_statuses))
            .values(status=LLMStateStatus.DISCARDED, updated_at=now)
        )
        discarded = int(update_result.rowcount or 0)
        if not discarded:
            return 0
        await session.execute(
            update(Execution)
            .where(Execution.id == execution_id)
            .where(Execution.status == ExecutionStatus.AWAITING_RESPONSE)
            .values(
                status=ExecutionStatus.FAILED,
                error_message="abandoned_by_execution",
                completed_at=now,
            )
        )
        return discarded


execution_state_service = ExecutionStateService()
