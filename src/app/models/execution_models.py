# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Optional

from sqlalchemy import (
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    JSON,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp for DB defaults."""
    return datetime.now(timezone.utc)


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_RESPONSE = "awaiting_response"
    COMPLETED = "completed"
    FAILED = "failed"


class LLMStateStatus(str, Enum):
    PENDING = "pending"
    AWAITING_RESPONSE = "awaiting_response"
    COMPLETED = "completed"
    DISCARDED = "discarded"


class ThreadSession(Base):
    """Pod-agnostic thread lifecycle keyed by the SHA-256 digest of thread_id."""

    __tablename__ = "thread_sessions"

    thread_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    close_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )

    runs: Mapped[List["ExecutionRun"]] = relationship(
        "ExecutionRun",
        back_populates="thread_session",
    )


class ExecutionRun(Base):
    """Per-request run association with heartbeat for stateless multi-pod lifecycle."""

    __tablename__ = "execution_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    thread_key: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("thread_sessions.thread_key", ondelete="SET NULL"),
        nullable=True,
    )
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    channel: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    close_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    execution: Mapped["Execution"] = relationship("Execution", back_populates="runs")
    thread_session: Mapped[Optional[ThreadSession]] = relationship(
        "ThreadSession",
        back_populates="runs",
    )


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    status: Mapped[ExecutionStatus] = mapped_column(
        SqlEnum(ExecutionStatus, name="execution_status"),
        nullable=False,
        default=ExecutionStatus.PENDING,
    )
    origin: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Orchestration binding: the folder/source this execution runs against,
    # the provisioned sandbox path, the harness type, and per-session config.
    source: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    workspace_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    orchestration_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    close_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    states: Mapped[List["LLMState"]] = relationship(
        "LLMState",
        back_populates="execution",
        cascade="all, delete-orphan",
    )
    runs: Mapped[List[ExecutionRun]] = relationship(
        "ExecutionRun",
        back_populates="execution",
        cascade="all, delete-orphan",
    )


class LLMState(Base):
    __tablename__ = "llm_states"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    thread_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    thread_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    run_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tool_call_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[LLMStateStatus] = mapped_column(
        SqlEnum(LLMStateStatus, name="llm_state_status"),
        nullable=False,
        default=LLMStateStatus.PENDING,
    )
    state_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )

    execution: Mapped[Execution] = relationship("Execution", back_populates="states")
