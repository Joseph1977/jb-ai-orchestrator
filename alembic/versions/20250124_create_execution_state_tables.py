# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""create execution and llm_state tables

Revision ID: 20250124_create_execution_state
Revises: 
Create Date: 2025-01-24 00:00:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20250124_create_execution_state"
down_revision = None
branch_labels = None
depends_on = None


execution_status = postgresql.ENUM(
    "pending",
    "running",
    "awaiting_response",
    "completed",
    "failed",
    name="execution_status",
    create_type=False,
)

llm_state_status = postgresql.ENUM(
    "pending",
    "awaiting_response",
    "completed",
    "discarded",
    name="llm_state_status",
    create_type=False,
)


def upgrade():
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())
    execution_status.create(bind, checkfirst=True)
    llm_state_status.create(bind, checkfirst=True)

    if "executions" not in existing_tables:
        op.create_table(
            "executions",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "status",
                execution_status,
                nullable=False,
                server_default="pending",
            ),
            sa.Column("result", sa.JSON(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
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
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )

    if "llm_states" not in existing_tables:
        op.create_table(
            "llm_states",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column(
                "execution_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("executions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("thread_id", sa.String(length=64), nullable=True),
            sa.Column("run_id", sa.String(length=64), nullable=True),
            sa.Column("tool_call_id", sa.String(length=128), nullable=True),
            sa.Column(
                "status",
                llm_state_status,
                nullable=False,
                server_default="pending",
            ),
            sa.Column("state_payload", sa.JSON(), nullable=False),
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

    # Later revision identifiers exceed Alembic's historical VARCHAR(32)
    # default. Widen this before Alembic records the next revision.
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade():
    op.drop_table("llm_states")
    op.drop_table("executions")

    bind = op.get_bind()
    llm_state_status.drop(bind, checkfirst=True)
    execution_status.drop(bind, checkfirst=True)
