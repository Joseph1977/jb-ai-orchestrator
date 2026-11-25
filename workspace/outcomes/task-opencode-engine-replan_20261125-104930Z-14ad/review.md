# Review — OpenCodeExecutionEngine implementation plan
Version: 13
Model: gpt-5.6-sol-medium
Verdict: CHANGES_REQUESTED

## Points
1. P022 [major] D14 releases the worker after a bounded idle poll fails, and the A6 outcome explicitly drops idle confirmation, but C-1 requires the sidecar session to be confirmed stopped before its claim is released; closing bridge/relay access does not prove the shared sidecar stopped consuming capacity or interfering with later runs → Make abort-plus-confirmed termination a stopping prerequisite (or isolate each run in a process whose exit can be confirmed), keep the worker/heartbeat/claim live until confirmation, and test that idle-unconfirmed never finalizes the claim.
