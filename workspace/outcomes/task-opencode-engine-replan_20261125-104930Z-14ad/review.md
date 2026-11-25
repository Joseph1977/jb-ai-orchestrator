# Review — OpenCodeExecutionEngine implementation plan
Version: 3
Model: gpt-5.6-sol-medium
Verdict: CHANGES_REQUESTED

## Points
1. P005 [major] D10 allows OpenCode when `HOOKS_ENABLED=true` but no hooks exist initially; D12 bypasses hub hook wrapping, so a hook added mid-run is ignored → Reject all OpenCode local-tool runs whenever hooks are enabled, or enforce hooks per bridge call; test mutation.
2. P006 [major] D5 persists identity only on `ExecutionRun`/`LLMState`, but legacy completed runs have neither and only persist `Execution`, violating In-2's identity-on-new-runs requirement → Persist engine/version on legacy `Execution` records with migration, backfill, and tests.
