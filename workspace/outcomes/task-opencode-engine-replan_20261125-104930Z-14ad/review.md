# Review — OpenCodeExecutionEngine implementation plan
Version: 15
Model: gpt-5.6-sol-medium
Verdict: CHANGES_REQUESTED

## Points
1. P024 [major] D4 resolves the fresh AG-UI and legacy engines inside `_create_agui_execution` / `_create_execution_record`, but D10 requires OpenCode capability and tool gates before calling those helpers; the controller therefore cannot select the gates without another environment read, or rejection still creates an `Execution`, violating P004/In-4 → Resolve the fresh engine once before all gates, pass that immutable choice into execution creation and subsequent run/state stamping, and test one resolver call plus no durable rows on rejection.
