# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Provider-aware tool registry tests."""

from app.services.agui_service import AGUIToolRecord
from app.services.mcp_agent_service import MCPTool
from app.services.tool_registry import ToolProvider, build_tool_registry


def test_unique_names_stay_canonical():
    mcp = [
        MCPTool(
            name="grep",
            description="grep files",
            input_schema={"type": "object", "properties": {}},
            server_url="http://x",
            server_name="default",
            original_name="grep",
        )
    ]
    reg = build_tool_registry(mcp_tools=mcp, agui_records=[], local_litellm_tools=[])
    assert reg.resolve("grep").canonical_name == "grep"


def test_collision_gets_deterministic_alias():
    mcp = [
        MCPTool(
            name="grep_srv1",
            description="mcp grep",
            input_schema={},
            server_url="http://a",
            server_name="srv1",
            original_name="grep",
        )
    ]
    agui = [
        AGUIToolRecord(
            name="grep",
            original_name="grep",
            description="ui grep",
            parameters={},
            prefixed_name="AGUI-grep",
        )
    ]
    reg = build_tool_registry(mcp_tools=mcp, agui_records=agui, local_litellm_tools=[])
    mcp_tool = reg.resolve("grep_srv1")
    agui_tool = reg.resolve("AGUI-grep")
    assert mcp_tool.provider == ToolProvider.MCP
    assert agui_tool.provider == ToolProvider.AGUI
    assert mcp_tool.canonical_name != agui_tool.canonical_name
