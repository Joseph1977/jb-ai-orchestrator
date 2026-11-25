# Review — OpenCodeExecutionEngine implementation plan
Version: 7
Model: gpt-5.6-sol-medium
Verdict: CHANGES_REQUESTED

## Points
1. P013 [major] D11/A1 share a loopback sidecar while D12 can expose execute_local; local_tool_provider.py:514 and ORCHESTRATOR §6 show shell is unjailed, so one run can call sidecar APIs for another → authenticate/isolate the sidecar or gate shell; add cross-run attack tests.
2. P014 [major] D10/D18 require global feature flags false for OpenCode, but D7 resumes internal state in that process; resumes silently lose compaction, subagents and multimodal behavior → keep globals unchanged with engine-local gates; test internal resume under an OpenCode default.
3. P015 [major] D15 relies on startup sweep for orphan recovery, but D25 skips cleanup and boots on version/health failure; stale claims can be reclaimed while sidecar work is unconfirmed stopped → block OpenCode reclaim until cleanup is verified; test this path.
