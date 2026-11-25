# Review — OpenCodeExecutionEngine implementation plan
Version: 9
Model: gpt-5.6-sol-medium
Verdict: CHANGES_REQUESTED

## Points
1. P017 [major] D10 leaves subagents, context compaction, and multimodal ungated although all default on and OpenCode reports them false, silently degrading behavior contrary to design §Capability gating → Gate each enabled capability pre-claim or implement tested parity; cover shipped defaults.
2. P018 [major] D12 calls `execute_mcp_tool(original_name, args)`, but `find_tool_by_name` indexes request tools by attributed `MCPTool.name`; custom/multi-server MCP calls therefore fail → Dispatch with the canonical mapped name and test suffixed names from multiple servers.
