# Review — OpenCodeExecutionEngine implementation plan
Version: 5
Model: gpt-5.6-sol-medium
Verdict: CHANGES_REQUESTED

## Points
1. P011 [major] D20 narrows `heartbeat_run` to `active`, but closing workers must stay heartbeated until stopped (§12; `run_lifecycle.py:408`), or stale recovery can release a live claim → Keep heartbeat on active+closing; add a separate active-only fence query and close regression test.
