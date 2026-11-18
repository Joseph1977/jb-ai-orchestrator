# Copyright 2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""MCP tool listing asks every server on every execute unless the operator bounds it."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.config import Config
from app.services.mcp_agent_service import MCPAgentService, MCPTool

SERVER = {"name": "default", "url": "http://mcp.example/mcp"}


def _service(fetch):
    service = MCPAgentService(mcp_server_configs=[SERVER])
    service._fetch_mcp_tools_from_server = fetch
    return service


def _tool():
    return MCPTool(
        name="search",
        description="search",
        input_schema={},
        server_url=SERVER["url"],
        server_name="default",
        original_name="search",
    )


def test_both_bounds_are_off_by_default():
    assert Config.MCP_FETCH_TIMEOUT_SEC == 0
    assert Config.MCP_TOOLS_CACHE_SEC == 0


def test_without_a_cache_every_execute_asks_the_servers():
    calls = []

    async def fetch(config):
        calls.append(config["name"])
        return [_tool()]

    service = _service(fetch)

    async def go():
        with patch.object(Config, "MCP_TOOLS_CACHE_SEC", 0):
            await service.fetch_mcp_tools()
            await service.fetch_mcp_tools()

    asyncio.run(go())
    assert calls == ["default", "default"]


def test_a_cached_list_is_reused_until_it_expires():
    calls = []

    async def fetch(config):
        calls.append(config["name"])
        return [_tool()]

    service = _service(fetch)

    async def go():
        with patch.object(Config, "MCP_TOOLS_CACHE_SEC", 60):
            first = await service.fetch_mcp_tools()
            second = await service.fetch_mcp_tools()
            service._tools_cache = (service._tools_cache[0] - 61, service._tools_cache[1])
            await service.fetch_mcp_tools()
        return first, second

    first, second = asyncio.run(go())
    assert [t.name for t in first] == ["search"]
    assert [t.name for t in second] == ["search"]
    assert calls == ["default", "default"]


def test_a_server_that_does_not_answer_in_time_contributes_no_tools():
    async def fetch(config):
        await asyncio.sleep(5)
        return [_tool()]

    service = _service(fetch)

    async def go():
        with patch.object(Config, "MCP_FETCH_TIMEOUT_SEC", 0.05):
            return await service.fetch_mcp_tools_from_server(SERVER)

    assert asyncio.run(go()) == []
