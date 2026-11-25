# Review — OpenCodeExecutionEngine implementation plan
Version: 2
Model: gpt-5.6-sol-medium
Verdict: CHANGES_REQUESTED

## Points
1. P002 [major] D12 calls `list_litellm_tools`/`execute` directly, but neither enforces `LocalToolProvider.enabled`, so `LOCAL_TOOLS_ENABLED=false` still exposes and runs local tools → Gate bridge listing and dispatch on `enabled`; test disabled listing and forged calls.
2. P003 [major] D12 returns `read_file_local` media markers directly, while only `ToolExecutionHub` turns them into model attachments; default-enabled multimodal reads silently break under OpenCode → Add a tested translation or an explicit multimodal capability gate before the claim.
3. P004 [major] D10/D12 silently remove always-available `ask_user` although In-4 and the design require unsupported flows to fail explicitly, so an engine switch can silently change workflows → Add a caller-visible capability gate/eligibility contract or support durable await; test it.
