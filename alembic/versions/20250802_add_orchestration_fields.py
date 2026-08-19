# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""add orchestration fields to executions + indexes on llm_states

Revision ID: 20250802_add_orchestration_fields
Revises: 20250124_create_execution_state
Create Date: 2025-08-02 00:00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20250802_add_orchestration_fields"
down_revision = "20250124_create_execution_state"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    execution_columns = {
        column["name"] for column in inspector.get_columns("executions")
    }
    if "source" not in execution_columns:
        op.add_column("executions", sa.Column("source", sa.Text(), nullable=True))
    if "workspace_path" not in execution_columns:
        op.add_column(
            "executions",
            sa.Column("workspace_path", sa.Text(), nullable=True),
        )
    if "orchestration_type" not in execution_columns:
        op.add_column(
            "executions",
            sa.Column("orchestration_type", sa.String(length=64), nullable=True),
        )
    if "config" not in execution_columns:
        op.add_column("executions", sa.Column("config", sa.JSON(), nullable=True))

    # Support pod-agnostic resume: look up a pending state by the tool call the
    # client is answering, scoped by thread.
    llm_indexes = {
        index["name"] for index in inspector.get_indexes("llm_states")
    }
    if "ix_llm_states_tool_call_id" not in llm_indexes:
        op.create_index(
            "ix_llm_states_tool_call_id",
            "llm_states",
            ["tool_call_id"],
        )
    if "ix_llm_states_thread_id" not in llm_indexes:
        op.create_index(
            "ix_llm_states_thread_id",
            "llm_states",
            ["thread_id"],
        )


def downgrade():
    op.drop_index("ix_llm_states_thread_id", table_name="llm_states")
    op.drop_index("ix_llm_states_tool_call_id", table_name="llm_states")

    op.drop_column("executions", "config")
    op.drop_column("executions", "orchestration_type")
    op.drop_column("executions", "workspace_path")
    op.drop_column("executions", "source")
