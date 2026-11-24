# Review — OpenCodeExecutionEngine implementation plan
Version: 1
Model: gpt-5.6-sol-medium
Verdict: CHANGES_REQUESTED

## Points
1. P001 [blocker] D14 confirms only sidecar idleness; bridge handlers may still execute local/MCP tools after abort, allowing claim release during side effects → Track, cancel, and await all bridge operations; test cancellation during each tool class.
2. P002 [major] D12/D15 keep bearer validity only in memory; stale recovery elsewhere can release the claim while the old process and token remain live → Fence each bridge call against active run ownership/status; test stale reclaim with the old worker live.
3. P003 [major] A3 permits per-sidecar MCP configuration, which cannot isolate per-run bearer tokens across concurrent sessions → Require isolated dynamic headers in the spike and test concurrent cross-token denial; otherwise serialize runs or isolate sidecars.
4. P004 [major] D9 advertises MCP support while D12 exposes every registry MCP tool; no compatibility map rejects unsupported schemas/tools as the design requires → Define the tested subset and pre-claim validator, reject unmapped requested tools, and test it.
5. P005 [major] D10 treats AG-UI without `workspacePath` as `workspace_free`, rejecting supported new-contract `input` bindings → Gate on the resolved `input` or legacy `workspacePath`; test both and a truly workspace-free run.
6. P006 [major] In-5 requires `OPENCODE_PROVIDER=native`, but the matrix tests only model precedence, not native routing/configuration → Add adapter and fixture tests for `litellm` and `native`, including A9 provider/model translation.
