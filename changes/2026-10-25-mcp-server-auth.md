# MCP server connection authentication

- **Date:** 2026-10-25
- **Branch:** `sync/generic-engine`

## What changed

An MCP server entry can name one connection identity.

- `authType` `service`, or `serviceToken` with no `authType`, sends the bearer from that environment variable on list and on every tool call. `serviceToken` is the variable name, not the secret. Startup fails if that variable is missing or empty.
- `authType` `user` sends `credentials.inputAccessToken` on list and on every tool call. `serviceToken` is omitted or null and is ignored.
- Omitting both fields leaves the connection unauthenticated, as before.

A request with no user token does not open an `authType` `user` server. Those tool lists stay out of the shared `MCP_TOOLS_CACHE_SEC` cache and are fetched on every request. Set `MCP_FETCH_TIMEOUT_SEC` when any server uses `authType` `user`. The direct agent routes have no credentials field, so user-auth servers stay unavailable there.

## Why

The MCP session carries one `Authorization` header. Tool-specific user authorization, after that connection is open, belongs to the MCP server.
