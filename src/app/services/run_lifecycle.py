# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.models.bindings import SESSION_CLOSED, SESSION_CLOSING
from app.models.execution_models import Execution, ExecutionRun, ExecutionStatus, ThreadSession

# Single authoritative stale threshold for close/cleanup decisions.
DEFAULT_HEARTBEAT_STALE_SEC = 300
# Retained alias: "fresh" means the same cutoff as non-stale (not a separate cleanup gate).
DEFAULT_HEARTBEAT_FRESH_SEC = DEFAULT_HEARTBEAT_STALE_SEC

RUN_STATUS_ACTIVE = "active"
RUN_STATUS_CLOSING = "closing"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"

_ACTIVE_RUN_STATUSES = (RUN_STATUS_ACTIVE, RUN_STATUS_CLOSING)

_RUNTIME_THREAD_KEY_RE = re.compile(r"/threads/([a-f0-9]{64})/runtime/?$")
_EXECUTION_RUNTIME_RE = re.compile(r"/([0-9a-f-]{36})/runtime/?$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def thread_key_for(thread_id: str) -> str:
    """SHA-256 digest of thread_id — same algorithm as runtime_root_for_thread."""
    return hashlib.sha256((thread_id or "thread").encode("utf-8")).hexdigest()


def thread_key_from_runtime_path(runtime_path: str) -> Optional[str]:
    """Derive a thread key from a sanitized runtimePath under threads/<sha256>/runtime."""
    if not runtime_path:
        return None
    normalized = str(runtime_path).replace("\\", "/").rstrip("/")
    match = _RUNTIME_THREAD_KEY_RE.search(normalized)
    return match.group(1) if match else None


def origin_from_runtime_path(runtime_path: Optional[str]) -> Optional[str]:
    """Infer execution origin from a sanitized runtimePath when safe."""
    if not runtime_path:
        return None
    if thread_key_from_runtime_path(runtime_path):
        return "agui"
    normalized = str(runtime_path).replace("\\", "/").rstrip("/")
    if _EXECUTION_RUNTIME_RE.search(normalized):
        return "orchestrator"
    return None


def synthetic_historical_run_id(execution_id: uuid.UUID) -> str:
    """Deterministic run_id for migration/backfill rows without llm_state run_id."""
    return f"backfill:{execution_id}"


def recovered_thread_id(thread_key: str) -> str:
    """Placeholder full thread_id when only the digest is known from runtimePath."""
    return f"recovered:{thread_key}"


def _session_has_close_lifecycle(
    close_requested_at: Optional[datetime],
    closed_at: Optional[datetime],
) -> bool:
    return close_requested_at is not None or closed_at is not None


class RunClaimError(Exception):
    """Active run claim rejected due to session lifecycle (not concurrency conflict)."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _reject_session_closure_or_raise(
    *,
    closed_at: Optional[datetime],
    close_requested_at: Optional[datetime],
) -> None:
    if closed_at is not None:
        raise RunClaimError(code=SESSION_CLOSED, message="Session is closed")
    if close_requested_at is not None:
        raise RunClaimError(
            code=SESSION_CLOSING,
            message="Session close is in progress",
        )


class RunLifecycleService:
    """Stateless multi-pod lifecycle persistence for threads and executable runs."""

    async def ensure_thread_session(
        self,
        session: AsyncSession,
        thread_id: str,
    ) -> ThreadSession:
        key = thread_key_for(thread_id)
        return await self._upsert_thread_session(session, thread_id, key)

    async def get_thread_session(
        self,
        session: AsyncSession,
        thread_key: str,
    ) -> Optional[ThreadSession]:
        if not thread_key:
            return None
        result = await session.execute(
            select(ThreadSession).where(ThreadSession.thread_key == thread_key)
        )
        return result.scalar_one_or_none()

    async def is_thread_close_requested(
        self,
        session: AsyncSession,
        *,
        thread_id: Optional[str] = None,
        thread_key: Optional[str] = None,
    ) -> bool:
        key = thread_key or (thread_key_for(thread_id) if thread_id else None)
        if not key:
            return False
        row = await self.get_thread_session(session, key)
        if row is None:
            return False
        return _session_has_close_lifecycle(row.close_requested_at, row.closed_at)

    async def is_execution_close_requested(
        self,
        session: AsyncSession,
        execution_id: uuid.UUID,
    ) -> bool:
        result = await session.execute(
            select(Execution.close_requested_at, Execution.closed_at).where(
                Execution.id == execution_id
            )
        )
        row = result.one_or_none()
        if row is None:
            return False
        close_requested_at, closed_at = row
        return _session_has_close_lifecycle(close_requested_at, closed_at)

    async def mark_thread_close_requested(
        self,
        session: AsyncSession,
        *,
        thread_id: Optional[str] = None,
        thread_key: Optional[str] = None,
    ) -> bool:
        """Idempotent: set close_requested_at once; never overwrite closed_at."""
        if thread_id:
            await self.ensure_thread_session(session, thread_id)
        key = thread_key or (thread_key_for(thread_id) if thread_id else None)
        if not key:
            return False
        now = _utcnow()
        result = await session.execute(
            update(ThreadSession)
            .where(ThreadSession.thread_key == key)
            .where(ThreadSession.closed_at.is_(None))
            .where(ThreadSession.close_requested_at.is_(None))
            .values(close_requested_at=now, updated_at=now)
        )
        return bool(result.rowcount)

    async def mark_execution_close_requested(
        self,
        session: AsyncSession,
        execution_id: uuid.UUID,
    ) -> bool:
        """Idempotent close request; preserve terminal COMPLETED status."""
        now = _utcnow()
        result = await session.execute(
            update(Execution)
            .where(Execution.id == execution_id)
            .where(Execution.closed_at.is_(None))
            .where(Execution.close_requested_at.is_(None))
            .values(close_requested_at=now, updated_at=now)
        )
        return bool(result.rowcount)

    async def mark_thread_closed(
        self,
        session: AsyncSession,
        thread_key: str,
    ) -> bool:
        now = _utcnow()
        result = await session.execute(
            update(ThreadSession)
            .where(ThreadSession.thread_key == thread_key)
            .where(ThreadSession.closed_at.is_(None))
            .values(closed_at=now, updated_at=now)
        )
        return bool(result.rowcount)

    async def mark_execution_closed(
        self,
        session: AsyncSession,
        execution_id: uuid.UUID,
    ) -> bool:
        now = _utcnow()
        result = await session.execute(
            update(Execution)
            .where(Execution.id == execution_id)
            .where(Execution.closed_at.is_(None))
            .values(closed_at=now, updated_at=now)
        )
        return bool(result.rowcount)

    async def associate_execution_origin(
        self,
        session: AsyncSession,
        execution_id: uuid.UUID,
        *,
        origin: str,
        thread_id: Optional[str] = None,
    ) -> None:
        """Record execution origin and ensure thread session exists when provided."""
        if thread_id:
            await self.ensure_thread_session(session, thread_id)
        await session.execute(
            update(Execution)
            .where(Execution.id == execution_id)
            .where(or_(Execution.origin.is_(None), Execution.origin == origin))
            .values(origin=origin, updated_at=_utcnow())
        )

    async def _upsert_thread_session(
        self,
        session: AsyncSession,
        thread_id: str,
        thread_key: str,
    ) -> ThreadSession:
        now = _utcnow()
        values = {
            "thread_key": thread_key,
            "thread_id": thread_id,
            "created_at": now,
            "updated_at": now,
        }
        bind = session.get_bind()
        dialect = bind.dialect.name if bind is not None else ""

        if dialect == "postgresql":
            stmt = pg_insert(ThreadSession).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["thread_key"],
                set_={"thread_id": thread_id, "updated_at": now},
            )
            await session.execute(stmt)
        elif dialect == "sqlite":
            stmt = sqlite_insert(ThreadSession).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["thread_key"],
                set_={"thread_id": thread_id, "updated_at": now},
            )
            await session.execute(stmt)
        else:
            existing = await self.get_thread_session(session, thread_key)
            if existing is not None:
                if existing.thread_id != thread_id:
                    existing.thread_id = thread_id
                return existing
            row = ThreadSession(**values)
            session.add(row)
            await session.flush()
            return row

        await session.flush()
        locked = await session.execute(
            select(ThreadSession)
            .where(ThreadSession.thread_key == thread_key)
            .with_for_update()
        )
        return locked.scalar_one()

    async def _lock_execution_row(
        self,
        session: AsyncSession,
        execution_id: uuid.UUID,
    ) -> Optional[Execution]:
        result = await session.execute(
            select(Execution)
            .where(Execution.id == execution_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def _has_conflicting_active_run(
        self,
        session: AsyncSession,
        *,
        execution_id: uuid.UUID,
        thread_key: Optional[str],
        exclude_run_id: Optional[uuid.UUID] = None,
    ) -> bool:
        filters = [
            ExecutionRun.status.in_(_ACTIVE_RUN_STATUSES),
            ExecutionRun.finished_at.is_(None),
        ]
        if exclude_run_id is not None:
            filters.append(ExecutionRun.id != exclude_run_id)
        scope = [ExecutionRun.execution_id == execution_id]
        if thread_key:
            scope.append(
                and_(
                    ExecutionRun.thread_key == thread_key,
                    ExecutionRun.thread_key.isnot(None),
                )
            )
        query = select(ExecutionRun.id).where(and_(*filters, or_(*scope))).limit(1)
        result = await session.execute(query)
        return result.scalar_one_or_none() is not None

    async def try_create_active_run(
        self,
        session: AsyncSession,
        *,
        execution_id: uuid.UUID,
        run_id: str,
        thread_id: Optional[str] = None,
        origin: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> Optional[ExecutionRun]:
        """Create an active run under row locks; reject concurrent execution/thread holds."""
        locked_execution = await self._lock_execution_row(session, execution_id)
        if locked_execution is None:
            return None

        _reject_session_closure_or_raise(
            closed_at=locked_execution.closed_at,
            close_requested_at=locked_execution.close_requested_at,
        )

        thread_key: Optional[str] = None
        if thread_id:
            thread_key = thread_key_for(thread_id)
            locked_thread = await self._upsert_thread_session(
                session, thread_id, thread_key
            )
            _reject_session_closure_or_raise(
                closed_at=locked_thread.closed_at,
                close_requested_at=locked_thread.close_requested_at,
            )

        stale_sec = getattr(
            Config, "RUN_HEARTBEAT_STALE_SEC", DEFAULT_HEARTBEAT_STALE_SEC
        )
        await self.mark_stale_runs_failed(
            session,
            stale_sec=stale_sec,
            execution_id=execution_id,
            preserve_completed_execution=True,
        )
        if thread_key:
            await self.mark_stale_runs_failed(
                session,
                stale_sec=stale_sec,
                thread_key=thread_key,
                preserve_completed_execution=True,
            )

        if await self._has_conflicting_active_run(
            session,
            execution_id=execution_id,
            thread_key=thread_key,
        ):
            return None

        now = _utcnow()
        run = ExecutionRun(
            execution_id=execution_id,
            thread_key=thread_key,
            run_id=run_id,
            origin=origin,
            channel=channel,
            status=RUN_STATUS_ACTIVE,
            heartbeat_at=now,
            started_at=now,
        )
        session.add(run)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            return None
        return run

    async def heartbeat_run(
        self,
        session: AsyncSession,
        run_pk: uuid.UUID,
    ) -> bool:
        now = _utcnow()
        result = await session.execute(
            update(ExecutionRun)
            .where(ExecutionRun.id == run_pk)
            .where(ExecutionRun.finished_at.is_(None))
            .where(ExecutionRun.status.in_(_ACTIVE_RUN_STATUSES))
            .values(heartbeat_at=now)
        )
        return bool(result.rowcount)

    async def finish_run(
        self,
        session: AsyncSession,
        run_pk: uuid.UUID,
        *,
        status: str = RUN_STATUS_COMPLETED,
        preserve_completed_execution: bool = True,
        update_execution_status: bool = True,
        heartbeat_before: Optional[datetime] = None,
    ) -> bool:
        """Finish a run; optionally update parent execution or record run-only."""
        now = _utcnow()
        finish_query = (
            update(ExecutionRun)
            .where(ExecutionRun.id == run_pk)
            .where(ExecutionRun.finished_at.is_(None))
            .values(status=status, finished_at=now)
            .returning(ExecutionRun.execution_id)
        )
        if heartbeat_before is not None:
            finish_query = finish_query.where(
                ExecutionRun.heartbeat_at < heartbeat_before
            )
        result = await session.execute(finish_query)
        execution_id = result.scalar_one_or_none()
        if execution_id is None:
            return False

        if not update_execution_status:
            return True

        if status == RUN_STATUS_COMPLETED:
            await session.execute(
                update(Execution)
                .where(Execution.id == execution_id)
                .values(
                    status=ExecutionStatus.COMPLETED,
                    completed_at=now,
                    updated_at=now,
                )
            )
        elif status == RUN_STATUS_FAILED:
            fail_query = (
                update(Execution)
                .where(Execution.id == execution_id)
                .values(
                    status=ExecutionStatus.FAILED,
                    completed_at=now,
                    updated_at=now,
                )
            )
            if preserve_completed_execution:
                fail_query = fail_query.where(
                    Execution.status != ExecutionStatus.COMPLETED
                )
            await session.execute(fail_query)
        return True

    async def mark_run_close_requested(
        self,
        session: AsyncSession,
        run_pk: uuid.UUID,
    ) -> bool:
        now = _utcnow()
        result = await session.execute(
            update(ExecutionRun)
            .where(ExecutionRun.id == run_pk)
            .where(ExecutionRun.finished_at.is_(None))
            .where(ExecutionRun.close_requested_at.is_(None))
            .values(close_requested_at=now, status=RUN_STATUS_CLOSING)
        )
        return bool(result.rowcount)

    async def mark_active_runs_closing_for_thread(
        self,
        session: AsyncSession,
        *,
        thread_id: Optional[str] = None,
        thread_key: Optional[str] = None,
    ) -> int:
        key = thread_key or (thread_key_for(thread_id) if thread_id else None)
        if not key:
            return 0
        now = _utcnow()
        result = await session.execute(
            update(ExecutionRun)
            .where(ExecutionRun.thread_key == key)
            .where(ExecutionRun.status == RUN_STATUS_ACTIVE)
            .where(ExecutionRun.finished_at.is_(None))
            .values(close_requested_at=now, status=RUN_STATUS_CLOSING)
        )
        return int(result.rowcount or 0)

    async def mark_active_runs_closing_for_execution(
        self,
        session: AsyncSession,
        execution_id: uuid.UUID,
    ) -> int:
        now = _utcnow()
        result = await session.execute(
            update(ExecutionRun)
            .where(ExecutionRun.execution_id == execution_id)
            .where(ExecutionRun.status == RUN_STATUS_ACTIVE)
            .where(ExecutionRun.finished_at.is_(None))
            .values(close_requested_at=now, status=RUN_STATUS_CLOSING)
        )
        return int(result.rowcount or 0)

    async def mark_stale_runs_failed(
        self,
        session: AsyncSession,
        *,
        stale_sec: int = DEFAULT_HEARTBEAT_STALE_SEC,
        execution_id: Optional[uuid.UUID] = None,
        thread_key: Optional[str] = None,
        preserve_completed_execution: bool = True,
    ) -> int:
        """Fail stale active/closing runs without downgrading completed executions."""
        cutoff = _utcnow() - timedelta(seconds=stale_sec)
        stale_runs = await self.list_stale_runs(
            session,
            stale_sec=stale_sec,
            execution_id=execution_id,
            thread_key=thread_key,
        )
        count = 0
        for run in stale_runs:
            if await self.finish_run(
                session,
                run.id,
                status=RUN_STATUS_FAILED,
                preserve_completed_execution=preserve_completed_execution,
                heartbeat_before=cutoff,
            ):
                count += 1
        return count

    async def list_active_runs(
        self,
        session: AsyncSession,
        *,
        execution_id: Optional[uuid.UUID] = None,
        thread_key: Optional[str] = None,
    ) -> Sequence[ExecutionRun]:
        query = select(ExecutionRun).where(
            ExecutionRun.status.in_(_ACTIVE_RUN_STATUSES),
            ExecutionRun.finished_at.is_(None),
        )
        if execution_id is not None:
            query = query.where(ExecutionRun.execution_id == execution_id)
        if thread_key is not None:
            query = query.where(ExecutionRun.thread_key == thread_key)
        query = query.order_by(ExecutionRun.started_at.desc())
        result = await session.execute(query)
        return result.scalars().all()

    async def list_non_stale_active_runs(
        self,
        session: AsyncSession,
        *,
        stale_sec: int = DEFAULT_HEARTBEAT_STALE_SEC,
        execution_id: Optional[uuid.UUID] = None,
        thread_key: Optional[str] = None,
    ) -> Sequence[ExecutionRun]:
        """Active runs whose heartbeat is within the stale threshold (block cleanup)."""
        cutoff = _utcnow() - timedelta(seconds=stale_sec)
        query = select(ExecutionRun).where(
            ExecutionRun.status.in_(_ACTIVE_RUN_STATUSES),
            ExecutionRun.finished_at.is_(None),
            ExecutionRun.heartbeat_at >= cutoff,
        )
        if execution_id is not None:
            query = query.where(ExecutionRun.execution_id == execution_id)
        if thread_key is not None:
            query = query.where(ExecutionRun.thread_key == thread_key)
        query = query.order_by(ExecutionRun.heartbeat_at.desc())
        result = await session.execute(query)
        return result.scalars().all()

    async def list_fresh_runs(
        self,
        session: AsyncSession,
        *,
        fresh_sec: int = DEFAULT_HEARTBEAT_FRESH_SEC,
        execution_id: Optional[uuid.UUID] = None,
        thread_key: Optional[str] = None,
    ) -> Sequence[ExecutionRun]:
        """Alias for non-stale active runs (same threshold as close authority)."""
        return await self.list_non_stale_active_runs(
            session,
            stale_sec=fresh_sec,
            execution_id=execution_id,
            thread_key=thread_key,
        )

    async def list_stale_runs(
        self,
        session: AsyncSession,
        *,
        stale_sec: int = DEFAULT_HEARTBEAT_STALE_SEC,
        execution_id: Optional[uuid.UUID] = None,
        thread_key: Optional[str] = None,
    ) -> Sequence[ExecutionRun]:
        cutoff = _utcnow() - timedelta(seconds=stale_sec)
        query = select(ExecutionRun).where(
            ExecutionRun.status.in_(_ACTIVE_RUN_STATUSES),
            ExecutionRun.finished_at.is_(None),
            ExecutionRun.heartbeat_at < cutoff,
        )
        if execution_id is not None:
            query = query.where(ExecutionRun.execution_id == execution_id)
        if thread_key is not None:
            query = query.where(ExecutionRun.thread_key == thread_key)
        query = query.order_by(ExecutionRun.heartbeat_at.asc())
        result = await session.execute(query)
        return result.scalars().all()

    async def list_runs_for_thread(
        self,
        session: AsyncSession,
        *,
        thread_id: Optional[str] = None,
        thread_key: Optional[str] = None,
    ) -> Sequence[ExecutionRun]:
        key = thread_key or (thread_key_for(thread_id) if thread_id else None)
        if not key:
            return []
        result = await session.execute(
            select(ExecutionRun)
            .where(ExecutionRun.thread_key == key)
            .order_by(ExecutionRun.started_at.desc())
        )
        return result.scalars().all()

    async def list_executions_for_thread(
        self,
        session: AsyncSession,
        *,
        thread_id: Optional[str] = None,
        thread_key: Optional[str] = None,
    ) -> Sequence[Execution]:
        key = thread_key or (thread_key_for(thread_id) if thread_id else None)
        if not key:
            return []
        result = await session.execute(
            select(Execution)
            .where(
                Execution.id.in_(
                    select(ExecutionRun.execution_id).where(
                        ExecutionRun.thread_key == key
                    )
                )
            )
            .order_by(Execution.created_at.desc())
        )
        return result.scalars().all()

    async def reject_if_close_requested(
        self,
        session: AsyncSession,
        *,
        execution_id: Optional[uuid.UUID] = None,
        thread_id: Optional[str] = None,
        thread_key: Optional[str] = None,
    ) -> bool:
        """Return True when the request should be rejected due to close lifecycle."""
        if execution_id is not None and await self.is_execution_close_requested(
            session, execution_id
        ):
            return True
        return await self.is_thread_close_requested(
            session,
            thread_id=thread_id,
            thread_key=thread_key,
        )


run_lifecycle_service = RunLifecycleService()
