# Optional bounds on MCP tool listing

- **Date:** 2026-09-30
- **Branch:** `sync/generic-engine-clean`

## What changed

Every execute lists the tools of every configured MCP server before the first
model call. Two operator settings, both `0` (off) by default, bound that:

- `MCP_FETCH_TIMEOUT_SEC` caps how long one server's listing may take. A server
  that misses it is logged, with its URL redacted, and contributes no tools.
- `MCP_TOOLS_CACHE_SEC` reuses a fetched tool list for that many seconds, so an
  execute inside the window does not reconnect to every server.

With both off, every execute asks every server and waits as long as the
transport does, as before.

## Why

A deployment that lists a server it cannot reach pays the connect failure on
every execute. On Windows a refused local connection takes about two seconds,
which a person answering an interview feels on every question.

## Migration / breaking

Negative and non-finite values are rejected at startup. A cached list does not
see tools a server adds or removes until the window ends.

Tests: `tests/test_mcp_tools_cache.py`.
