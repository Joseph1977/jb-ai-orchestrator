# Review — OpenCodeExecutionEngine implementation plan
Version: 4
Model: gpt-5.6-sol-medium
Verdict: CHANGES_REQUESTED

## Points
1. P007 [major] D12 never defines MCP dispatch; `execute_mcp_tool` needs both request-tool and user-token ContextVars, which preflight has reset → Bind/reset both per bridge task and test concurrent user-auth calls cannot cross-contaminate.
2. P008 [major] D11/C-1 require one loopback sidecar per process, but D18 accepts any host, allowing a shared remote sidecar that breaks stop, sweep, and isolation guarantees → Reject non-loopback `OPENCODE_BASE_URL` values and test IPv4/IPv6/hostname cases.
3. P009 [major] D21's fallback needs a secret “set at sidecar start,” but no slice/file owns sidecar launch or secret injection, so A3 failure has no executable fallback → Make A3 failure stopping, or plan sidecar process ownership, launch/config, teardown, and isolation tests.
4. P010 [major] D9 says durable resume is unsupported, yet D12 advertises `ask_user_local`, a tool guaranteed to fail after possible prior side effects → Exclude it from the OpenCode tool catalog and test absence, or implement durable await before advertising it.
