# Generic engine sync

- **Date:** 2026-11-18
- **Branch:** `sync/generic-engine`

## What changed

- Added per-server MCP connection authentication using either a service bearer
  from a named environment variable or the caller's transient user token.
- Added optional MCP tool-list timeout and cache bounds, both disabled by
  default.
- Preserved assistant tool-call messages with their matching results during
  context summarization.
- Installed SQLAlchemy's `asyncio` extra.
- Added the proposed pluggable execution-engine architecture and linked it from
  the documentation indexes.

## Why

These changes improve generic MCP connectivity, bound optional tool discovery,
keep compacted model history valid, make fresh async database installations
complete, and document a future engine boundary without changing the current
execution engine.

## Migration / breaking

None by default. MCP authentication and listing bounds are opt-in. Negative or
non-finite MCP listing bounds are rejected at startup.
