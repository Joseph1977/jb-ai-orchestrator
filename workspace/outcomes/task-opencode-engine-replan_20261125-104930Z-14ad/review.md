# Review — OpenCodeExecutionEngine implementation plan
Version: 14
Model: gpt-5.6-sol-medium
Verdict: CHANGES_REQUESTED

## Points
1. P023 [major] D4 resolves every “fresh” segment from `ORCHESTRATOR_ENGINE`, while D5 explicitly makes `executions.engine` audit-only and overwrites it on each orchestrator execute; therefore an environment change can move later segments of the same persisted orchestrator session to another engine, contrary to C-5’s “persisted engine identity wins” rule → Resolve an existing session from its immutable `Execution.engine`/version, use the environment only when creating the session, keep each `ExecutionRun` consistent, and test env changes between initiate/execute and repeated executes.
