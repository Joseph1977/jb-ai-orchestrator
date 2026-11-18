# Install greenlet with the async database engine

- **Date:** 2026-09-30
- **Branch:** `sync/generic-engine-clean`

## What changed

`src/requirements.txt` asks for `SQLAlchemy[asyncio]` instead of `SQLAlchemy`.

## Why

SQLAlchemy 2.1 stopped installing `greenlet` by default and moved it to the
`asyncio` extra. The engine uses `sqlalchemy.ext.asyncio`, which refuses to
import without it. A fresh install therefore collected no tests, and an image
built from it could not open its database. Installs made before 2.1 still had
`greenlet`, which hid the gap.

## Migration / breaking

None. The extra adds `greenlet` and nothing else.
