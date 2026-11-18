# Copyright 2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""One MCP connection uses one bearer: the service env var, or the request token."""

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from app.config import Config
from app.services.mcp_agent_service import (
    MCPAgentService,
    bind_mcp_user_token,
    bind_request_mcp_tools,
    reset_mcp_user_token,
    reset_request_mcp_tools,
)


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = name
        self.inputSchema = {}


class _Listed:
    def __init__(self):
        self.tools = [_Tool("search")]
        self.nextCursor = None


class _CallResult:
    content = []
    isError = False


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def initialize(self):
        return None

    async def list_tools(self, **kwargs):
        return _Listed()

    async def call_tool(self, name, arguments):
        return _CallResult()


def _servers():
    return [
        {
            "name": "platform",
            "url": "http://platform/mcp",
            "authType": "service",
            "serviceToken": "MCP_TOKEN_PLATFORM",
        },
        {"name": "gmail", "url": "http://gmail/mcp", "authType": "user"},
    ]


@pytest.fixture
def opened():
    calls = []

    @asynccontextmanager
    async def fake_client(url, headers=None, **kwargs):
        calls.append((url, headers))
        yield (None, None, None)

    with patch("app.services.mcp_agent_service.streamablehttp_client", fake_client), patch(
        "app.services.mcp_agent_service.ClientSession", lambda *a, **k: _Session()
    ):
        yield calls


def test_service_and_user_connections_use_different_bearers(monkeypatch, opened):
    monkeypatch.setenv("MCP_TOKEN_PLATFORM", "service-secret")
    service = MCPAgentService(mcp_server_configs=_servers())

    async def go():
        with patch.object(Config, "MCP_TOOLS_CACHE_SEC", 60):
            first = await service.fetch_mcp_tools(user_access_token="user-secret")
            await service.fetch_mcp_tools(user_access_token="user-secret")
        return first

    import asyncio

    tools = asyncio.run(go())
    names = sorted(tool.name for tool in tools)
    assert names == ["search_gmail", "search_platform"]
    assert [headers["Authorization"] for _, headers in opened] == [
        "Bearer service-secret",
        "Bearer user-secret",
        "Bearer user-secret",
    ]
    cached_names = {tool.server_name for tool in service._tools_cache[1]}
    assert cached_names == {"platform"}
    assert "gmail" not in {tool.server_name for tool in service.all_mcp_tools}


def test_missing_user_token_does_not_connect(monkeypatch, opened):
    monkeypatch.setenv("MCP_TOKEN_PLATFORM", "service-secret")
    service = MCPAgentService(mcp_server_configs=_servers())

    import asyncio

    tools = asyncio.run(service.fetch_mcp_tools())
    assert [tool.server_name for tool in tools] == ["platform"]
    assert [headers["Authorization"] for _, headers in opened] == ["Bearer service-secret"]

    missing = asyncio.run(service.execute_mcp_tool("search_gmail", {}))
    assert missing["isError"] is True
    assert len(opened) == 1


def test_user_server_ignores_service_token_on_call(monkeypatch, opened):
    monkeypatch.setenv("MCP_TOKEN_PLATFORM", "service-secret")
    configs = [
        {
            "name": "gmail",
            "url": "http://gmail/mcp",
            "authType": "user",
            "serviceToken": "MCP_TOKEN_PLATFORM",
        }
    ]
    service = MCPAgentService(mcp_server_configs=configs)

    async def go():
        token_reset = bind_mcp_user_token("user-secret")
        try:
            tools = await service.fetch_mcp_tools()
            tools_reset = bind_request_mcp_tools(tools)
            try:
                return await service.execute_mcp_tool("search_gmail", {})
            finally:
                reset_request_mcp_tools(tools_reset)
        finally:
            reset_mcp_user_token(token_reset)

    import asyncio

    asyncio.run(go())
    assert [headers["Authorization"] for _, headers in opened] == [
        "Bearer user-secret",
        "Bearer user-secret",
    ]
    assert all("service-secret" not in (headers or {}).get("Authorization", "") for _, headers in opened)


def test_service_call_uses_only_the_env_bearer(monkeypatch, opened):
    monkeypatch.setenv("MCP_TOKEN_PLATFORM", "service-secret")
    service = MCPAgentService(
        mcp_server_configs=[
            {
                "name": "platform",
                "url": "http://platform/mcp",
                "authType": "service",
                "serviceToken": "MCP_TOKEN_PLATFORM",
            }
        ]
    )

    async def go():
        tools = await service.fetch_mcp_tools(user_access_token="user-secret")
        return await service.execute_mcp_tool(tools[0].name, {})

    import asyncio

    asyncio.run(go())
    assert [headers["Authorization"] for _, headers in opened] == [
        "Bearer service-secret",
        "Bearer service-secret",
    ]
