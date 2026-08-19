# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Phase 3 lifecycle persistence: thread sessions, execution runs, thread keys.

Revision ID: 20250816_phase3_lifecycle
Revises: 20250802_add_orchestration_fields
Create Date: 2025-08-16 00:00:00

"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy import text


revision = "20250816_phase3_lifecycle"
down_revision = "20250802_add_orchestration_fields"
branch_labels = None
depends_on = None

# --- Local migration helpers (must not import mutable app code) ---

_RUNTIME_THREAD_KEY_RE = re.compile(r"/threads/([a-f0-9]{64})/runtime/?$")
_EXECUTION_RUNTIME_RE = re.compile(r"/([0-9a-f-]{36})/runtime/?$")

_ACTIVE_RUN_STATUSES = ("active", "closing")


def _thread_key_for(thread_id: str) -> str:
    return hashlib.sha256((thread_id or "thread").encode("utf-8")).hexdigest()


def _thread_key_from_runtime_path(runtime_path: str) -> str | None:
    if not runtime_path:
        return None
    normalized = str(runtime_path).replace("\\", "/").rstrip("/")
    match = _RUNTIME_THREAD_KEY_RE.search(normalized)
    return match.group(1) if match else None


def _origin_from_runtime_path(runtime_path: str | None) -> str | None:
    if not runtime_path:
        return None
    if _thread_key_from_runtime_path(runtime_path):
        return "agui"
    normalized = str(runtime_path).replace("\\", "/").rstrip("/")
    if _EXECUTION_RUNTIME_RE.search(normalized):
        return "orchestrator"
    return None


def _recovered_thread_id(thread_key: str) -> str:
    return f"recovered:{thread_key}"


def _synthetic_historical_run_id(execution_id: uuid.UUID) -> str:
    return f"backfill:{execution_id}"


def _llm_state_run_status(status: str) -> tuple[str, bool]:
    """Map LLMState status to (execution_run status, is_finished).

    Only in-progress resume claims (pending) remain active. Awaiting holds are
    paused after their request segment and must not block migrated close/resume.
    """
    if status == "pending":
        return "active", False
    if status == "discarded":
        return "failed", True
    return "completed", True


def _execution_historical_run_status(execution_status: str) -> tuple[str, bool]:
    """Map Execution status to synthetic historical run status."""
    if execution_status in ("running", "pending"):
        return "active", False
    if execution_status == "failed":
        return "failed", True
    return "completed", True


def _parse_config(raw) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return None


def _runtime_path_from_config(raw) -> str | None:
    config = _parse_config(raw)
    if not config:
        return None
    path = config.get("runtimePath")
    return str(path) if path else None


def _ensure_thread_session(connection, thread_key: str, thread_id: str, now: datetime) -> None:
    existing = connection.execute(
        text("SELECT thread_key FROM thread_sessions WHERE thread_key = :key"),
        {"key": thread_key},
    ).fetchone()
    if existing:
        return
    connection.execute(
        text(
            """
            INSERT INTO thread_sessions (thread_key, thread_id, created_at, updated_at)
            VALUES (:key, :thread_id, :now, :now)
            """
        ),
        {"key": thread_key, "thread_id": thread_id, "now": now},
    )


def _backfill_llm_state_thread_keys(connection) -> None:
    rows = connection.execute(
        text(
            """
            SELECT id, thread_id
            FROM llm_states
            WHERE thread_id IS NOT NULL AND thread_key IS NULL
            """
        )
    ).fetchall()
    for row in rows:
        connection.execute(
            text("UPDATE llm_states SET thread_key = :key WHERE id = :id"),
            {"key": _thread_key_for(row.thread_id), "id": row.id},
        )

    rows = connection.execute(
        text(
            """
            SELECT ls.id, e.config
            FROM llm_states ls
            JOIN executions e ON e.id = ls.execution_id
            WHERE ls.thread_key IS NULL
            """
        )
    ).fetchall()
    for row in rows:
        digest = _thread_key_from_runtime_path(_runtime_path_from_config(row.config) or "")
        if digest:
            connection.execute(
                text("UPDATE llm_states SET thread_key = :key WHERE id = :id"),
                {"key": digest, "id": row.id},
            )


def _backfill_thread_sessions(connection, now: datetime) -> None:
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT thread_key, thread_id
            FROM llm_states
            WHERE thread_key IS NOT NULL AND thread_id IS NOT NULL
            """
        )
    ).fetchall()
    for row in rows:
        _ensure_thread_session(connection, row.thread_key, row.thread_id, now)

    rows = connection.execute(
        text(
            """
            SELECT ls.thread_key, ls.thread_id
            FROM llm_states ls
            WHERE ls.thread_key IS NOT NULL AND ls.thread_id IS NULL
            """
        )
    ).fetchall()
    for row in rows:
        _ensure_thread_session(
            connection,
            row.thread_key,
            _recovered_thread_id(row.thread_key),
            now,
        )


def _backfill_execution_origins(connection, now: datetime) -> None:
    rows = connection.execute(
        text("SELECT id, config, origin FROM executions WHERE origin IS NULL")
    ).fetchall()
    for row in rows:
        origin = _origin_from_runtime_path(_runtime_path_from_config(row.config))
        if not origin:
            continue
        connection.execute(
            text(
                """
                UPDATE executions
                SET origin = :origin, updated_at = :now
                WHERE id = :id AND origin IS NULL
                """
            ),
            {"origin": origin, "id": row.id, "now": now},
        )


def _backfill_execution_runs_from_llm_states(connection, now: datetime) -> None:
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (ls.execution_id, ls.run_id)
                ls.execution_id,
                ls.thread_key,
                ls.thread_id,
                ls.run_id,
                ls.status::text AS status,
                ls.created_at,
                ls.updated_at,
                e.origin,
                e.config
            FROM llm_states ls
            JOIN executions e ON e.id = ls.execution_id
            WHERE ls.run_id IS NOT NULL
            ORDER BY ls.execution_id, ls.run_id, ls.created_at DESC
            """
        )
    ).fetchall()

    seen: set[tuple] = set()
    for row in rows:
        key = (row.execution_id, row.run_id)
        if key in seen:
            continue
        seen.add(key)

        thread_key = row.thread_key
        if thread_key:
            thread_id = row.thread_id or _recovered_thread_id(thread_key)
            _ensure_thread_session(connection, thread_key, thread_id, now)

        origin = row.origin
        if not origin:
            origin = _origin_from_runtime_path(
                _runtime_path_from_config(row.config) or ""
            )

        run_status, is_finished = _llm_state_run_status(row.status)
        finished_at = None if not is_finished else (row.updated_at or now)
        connection.execute(
            text(
                """
                INSERT INTO execution_runs (
                    id, execution_id, thread_key, run_id, origin, status,
                    heartbeat_at, started_at, finished_at
                ) VALUES (
                    :id, :execution_id, :thread_key, :run_id, :origin, :status,
                    :heartbeat_at, :started_at, :finished_at
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "execution_id": row.execution_id,
                "thread_key": thread_key,
                "run_id": row.run_id,
                "origin": origin,
                "status": run_status,
                "heartbeat_at": row.updated_at or now,
                "started_at": row.created_at or now,
                "finished_at": finished_at,
            },
        )


def _backfill_agui_thread_runtime_executions(connection, now: datetime) -> None:
    """Discover AG-UI sandboxes from thread runtime paths across execution statuses."""
    rows = connection.execute(
        text(
            """
            SELECT e.id, e.config, e.status::text AS status,
                   e.completed_at, e.updated_at, e.created_at
            FROM executions e
            """
        )
    ).fetchall()

    for row in rows:
        runtime_path = _runtime_path_from_config(row.config) or ""
        thread_key = _thread_key_from_runtime_path(runtime_path)
        if not thread_key:
            continue

        _ensure_thread_session(
            connection,
            thread_key,
            _recovered_thread_id(thread_key),
            now,
        )

        connection.execute(
            text(
                """
                UPDATE executions
                SET origin = 'agui', updated_at = :now
                WHERE id = :id AND (origin IS NULL OR origin = 'agui')
                """
            ),
            {"id": row.id, "now": now},
        )

        existing_run = connection.execute(
            text(
                """
                SELECT id FROM execution_runs
                WHERE execution_id = :execution_id
                LIMIT 1
                """
            ),
            {"execution_id": row.id},
        ).fetchone()
        if existing_run:
            continue

        run_id = _synthetic_historical_run_id(row.id)
        run_status, is_finished = _execution_historical_run_status(row.status)
        heartbeat = row.updated_at or row.created_at or now
        finished = row.completed_at or row.updated_at or row.created_at or now
        finished_at = finished if is_finished else None

        connection.execute(
            text(
                """
                INSERT INTO execution_runs (
                    id, execution_id, thread_key, run_id, origin, status,
                    heartbeat_at, started_at, finished_at
                ) VALUES (
                    :id, :execution_id, :thread_key, :run_id, 'agui', :status,
                    :heartbeat_at, :started_at, :finished_at
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "execution_id": row.id,
                "thread_key": thread_key,
                "run_id": run_id,
                "status": run_status,
                "heartbeat_at": heartbeat,
                "started_at": row.created_at or now,
                "finished_at": finished_at,
            },
        )


def _terminalize_execution_run(connection, run_id: uuid.UUID, now: datetime) -> None:
    connection.execute(
        text(
            """
            UPDATE execution_runs
            SET status = 'completed', finished_at = :now
            WHERE id = :id
              AND status IN ('active', 'closing')
              AND finished_at IS NULL
            """
        ),
        {"id": run_id, "now": now},
    )


def _dedupe_active_execution_runs(connection, now: datetime) -> None:
    """Retain newest active/closing row per execution and per thread_key."""
    for column in ("execution_id", "thread_key"):
        if column == "thread_key":
            groups = connection.execute(
                text(
                    """
                    SELECT thread_key
                    FROM execution_runs
                    WHERE status IN ('active', 'closing')
                      AND finished_at IS NULL
                      AND thread_key IS NOT NULL
                    GROUP BY thread_key
                    HAVING COUNT(*) > 1
                    """
                )
            ).fetchall()
        else:
            groups = connection.execute(
                text(
                    """
                    SELECT execution_id
                    FROM execution_runs
                    WHERE status IN ('active', 'closing')
                      AND finished_at IS NULL
                    GROUP BY execution_id
                    HAVING COUNT(*) > 1
                    """
                )
            ).fetchall()

        for group in groups:
            value = group[0]
            rows = connection.execute(
                text(
                    f"""
                    SELECT id
                    FROM execution_runs
                    WHERE {column} = :value
                      AND status IN ('active', 'closing')
                      AND finished_at IS NULL
                    ORDER BY heartbeat_at DESC, started_at DESC
                    """
                ),
                {"value": value},
            ).fetchall()
            for stale in rows[1:]:
                _terminalize_execution_run(connection, stale[0], now)


def upgrade():
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    execution_columns = {
        column["name"] for column in inspector.get_columns("executions")
    }
    if "origin" not in execution_columns:
        op.add_column(
            "executions",
            sa.Column("origin", sa.String(length=64), nullable=True),
        )
    if "close_requested_at" not in execution_columns:
        op.add_column(
            "executions",
            sa.Column("close_requested_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "closed_at" not in execution_columns:
        op.add_column(
            "executions",
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        )

    op.alter_column(
        "llm_states",
        "thread_id",
        existing_type=sa.String(length=64),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "llm_states",
        "run_id",
        existing_type=sa.String(length=64),
        type_=sa.Text(),
        existing_nullable=True,
    )
    llm_columns = {
        column["name"] for column in inspector.get_columns("llm_states")
    }
    if "thread_key" not in llm_columns:
        op.add_column(
            "llm_states",
            sa.Column("thread_key", sa.String(length=64), nullable=True),
        )
    llm_indexes = {
        index["name"] for index in inspector.get_indexes("llm_states")
    }
    if "ix_llm_states_thread_key" not in llm_indexes:
        op.create_index("ix_llm_states_thread_key", "llm_states", ["thread_key"])

    existing_tables = set(inspector.get_table_names())
    if "thread_sessions" not in existing_tables:
        op.create_table(
            "thread_sessions",
            sa.Column("thread_key", sa.String(length=64), primary_key=True, nullable=False),
            sa.Column("thread_id", sa.Text(), nullable=False),
            sa.Column("close_requested_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    if "execution_runs" not in existing_tables:
        op.create_table(
            "execution_runs",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "execution_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("executions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "thread_key",
                sa.String(length=64),
                sa.ForeignKey("thread_sessions.thread_key", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("run_id", sa.Text(), nullable=False),
            sa.Column("origin", sa.String(length=64), nullable=True),
            sa.Column("channel", sa.String(length=64), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("close_requested_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
        execution_run_indexes: set[str] = set()
    else:
        execution_run_indexes = {
            index["name"] for index in inspector.get_indexes("execution_runs")
        }
    for index_name, columns in (
        ("ix_execution_runs_execution_id", ["execution_id"]),
        ("ix_execution_runs_thread_key", ["thread_key"]),
        ("ix_execution_runs_status", ["status"]),
        ("ix_execution_runs_heartbeat_at", ["heartbeat_at"]),
    ):
        if index_name not in execution_run_indexes:
            op.create_index(index_name, "execution_runs", columns)

    now = datetime.now(timezone.utc)
    _backfill_llm_state_thread_keys(connection)
    _backfill_thread_sessions(connection, now)
    _backfill_execution_origins(connection, now)
    _backfill_execution_runs_from_llm_states(connection, now)
    _backfill_agui_thread_runtime_executions(connection, now)
    _dedupe_active_execution_runs(connection, now)

    if connection.dialect.name == "postgresql":
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_runs_active_per_execution
            ON execution_runs (execution_id)
            WHERE status IN ('active', 'closing') AND finished_at IS NULL
            """
        )
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_runs_active_per_thread
            ON execution_runs (thread_key)
            WHERE thread_key IS NOT NULL
              AND status IN ('active', 'closing')
              AND finished_at IS NULL
            """
        )


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS uq_execution_runs_active_per_thread")
        op.execute("DROP INDEX IF EXISTS uq_execution_runs_active_per_execution")

    op.drop_index("ix_execution_runs_heartbeat_at", table_name="execution_runs")
    op.drop_index("ix_execution_runs_status", table_name="execution_runs")
    op.drop_index("ix_execution_runs_thread_key", table_name="execution_runs")
    op.drop_index("ix_execution_runs_execution_id", table_name="execution_runs")
    op.drop_table("execution_runs")
    op.drop_table("thread_sessions")

    op.drop_index("ix_llm_states_thread_key", table_name="llm_states")
    op.drop_column("llm_states", "thread_key")
    op.alter_column(
        "llm_states",
        "run_id",
        existing_type=sa.Text(),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    op.alter_column(
        "llm_states",
        "thread_id",
        existing_type=sa.Text(),
        type_=sa.String(length=64),
        existing_nullable=True,
    )

    op.drop_column("executions", "closed_at")
    op.drop_column("executions", "close_requested_at")
    op.drop_column("executions", "origin")
