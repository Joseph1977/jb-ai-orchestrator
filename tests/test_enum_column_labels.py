# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""The Postgres enum labels the ORM emits must match what Alembic created.

SQLAlchemy stores a Python enum's member *name* unless told otherwise, so a
column declared as ``SqlEnum(ExecutionStatus)`` writes ``'PENDING'`` while the
migration created a type accepting only ``'pending'``. That mismatch is invisible
on a database whose types were created from the model metadata, and breaks every
insert on one created by Alembic.
"""

import ast
from pathlib import Path

import pytest

from app.models.execution_models import Execution, LLMState

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20250124_create_execution_state_tables.py"
)


def _labels_declared_in_migration(type_name: str) -> list[str]:
    """Read the enum labels the migration passes to postgresql.ENUM(...)."""
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        named = next(
            (kw.value for kw in node.keywords if kw.arg == "name"),
            None,
        )
        if isinstance(named, ast.Constant) and named.value == type_name:
            return [a.value for a in node.args if isinstance(a, ast.Constant)]
    raise AssertionError(f"{type_name} is not created in {MIGRATION.name}")


@pytest.mark.parametrize(
    ("model", "column", "type_name"),
    [
        (Execution, "status", "execution_status"),
        (LLMState, "status", "llm_state_status"),
    ],
)
def test_orm_enum_labels_match_the_migration(model, column, type_name):
    orm_labels = list(model.__table__.c[column].type.enums)
    assert orm_labels == _labels_declared_in_migration(type_name)


@pytest.mark.parametrize(
    ("model", "column"),
    [(Execution, "status"), (LLMState, "status")],
)
def test_orm_emits_values_not_member_names(model, column):
    """Guards the specific regression: member names would be uppercase."""
    for label in model.__table__.c[column].type.enums:
        assert label == label.lower(), f"{label} looks like a member name"
