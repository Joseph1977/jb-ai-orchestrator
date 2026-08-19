# Execution status enums are stored by value

**Date:** 2026-08-19
**Branch:** `fix/execution-status-enum-case`
**Pull request:** [#1](https://github.com/Joseph1977/jb-ai-orchestrator/pull/1) → `on-going-dev`

## What changed

`Execution.status` and `LLMState.status` now pass `values_callable` to their
`SqlEnum` columns, so SQLAlchemy persists the enum members' values rather than
their names. Added `tests/test_enum_column_labels.py`, which parses the Alembic
revision that creates the two Postgres types and asserts the ORM's labels match
what the revision declares.

## Why

Starting any workflow against a freshly created database failed. The orchestrator
returned 500 from `POST /v1/orchestrator/initiate`, surfacing to the caller as
`502 ORCHESTRATOR_INITIATE_FAILED`, and the log showed:

```
invalid input value for enum execution_status: "PENDING"
```

Given a Python enum, SQLAlchemy stores the member *name* unless told otherwise,
so the insert sent `'PENDING'`. Revision `20250124_create_execution_state_tables`
creates the type with the lowercase *values*, so Postgres rejected it.

The defect survived this long because the schema has two authors: `init_db()`
calls `Base.metadata.create_all` alongside `alembic upgrade head`. A database
built by `create_all` gets its types from the member names and so happens to
match the ORM; one built by Alembic does not. Every long-lived local database had
been created down the first path, and a first run takes the second — so the bug
only appeared on a clean install.

## Migration note

No Alembic revision was added. The migration was already correct; the ORM was
brought into line with it.

Databases provisioned by Alembic, which is every deployed environment, already
carry the lowercase labels and need no action. Only a database whose enum types
were created by `create_all` carries uppercase labels; it will now reject writes
and must be recreated or have its types relabelled.

That `create_all` can still author a schema Alembic did not is the underlying
problem here. It is deliberately left in place and is worth addressing on its
own.

## Verification

- Full suite: 469 passed, including the 4 new assertions.
- The new test fails against the previous code with `assert 'PENDING' != 'pending'`.
- Against a wiped database, `initiate` returns `success: true` and the row
  persists with status `pending`.
