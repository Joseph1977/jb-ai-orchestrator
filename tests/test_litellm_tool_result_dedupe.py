# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""LiteLLM boundary dedupe for duplicate tool_call_id tool results."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from ag_ui.core.types import AssistantMessage, FunctionCall, ToolCall, ToolMessage, UserMessage

from app.services.agui_messages import (
    build_initial_messages,
    dedupe_litellm_tool_results,
)
from app.services.mcp_agent_service import MCPAgentService


def test_dedupe_keeps_earliest_tool_result_per_id():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"first": true}'},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"second": true}'},
    ]
    normalized, dup_ids = dedupe_litellm_tool_results(messages)
    tool_msgs = [m for m in normalized if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert json.loads(tool_msgs[0]["content"]) == {"first": True}
    assert dup_ids == ["call_1"]
    assert messages[1]["content"] == '{"first": true}'


def test_dedupe_fills_earliest_empty_content_from_later_duplicate():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": ""},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"answer": "yes"}'},
    ]
    normalized, dup_ids = dedupe_litellm_tool_results(messages)
    tool_msgs = [m for m in normalized if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert normalized.index(tool_msgs[0]) == 1
    assert json.loads(tool_msgs[0]["content"]) == {"answer": "yes"}
    assert messages[1]["content"] == ""
    assert dup_ids == ["call_1"]


def test_dedupe_leaves_non_duplicates_unchanged():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            {"id": "call_b", "type": "function", "function": {"name": "b", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_a", "content": '{"a": 1}'},
        {"role": "tool", "tool_call_id": "call_b", "content": '{"b": 2}'},
    ]
    normalized, dup_ids = dedupe_litellm_tool_results(messages)
    assert dup_ids == []
    assert normalized == messages
    assert normalized is messages


def test_dedupe_skips_missing_tool_call_id():
    messages = [
        {"role": "tool", "content": '{"a": 1}'},
        {"role": "tool", "content": '{"a": 2}'},
        {"role": "tool", "tool_call_id": "", "content": '{"empty": true}'},
    ]
    normalized, dup_ids = dedupe_litellm_tool_results(messages)
    assert dup_ids == []
    assert len([m for m in normalized if m.get("role") == "tool"]) == 3


def test_dedupe_anthropic_one_tool_use_one_tool_result():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_x", "type": "function", "function": {"name": "AskUser", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_x", "content": '{"answer":"a"}'},
        {"role": "tool", "tool_call_id": "call_x", "content": '{"answer":"duplicate"}'},
    ]
    normalized, dup_ids = dedupe_litellm_tool_results(messages)
    tool_msgs = [m for m in normalized if m.get("role") == "tool" and m.get("tool_call_id") == "call_x"]
    assert len(tool_msgs) == 1
    assert dup_ids == ["call_x"]


def test_fresh_client_history_duplicate_tool_results_normalized():
    msgs = [
        UserMessage(id="u1", content="go"),
        AssistantMessage(
            id="a1",
            content="",
            tool_calls=[
                ToolCall(
                    id="call_1",
                    function=FunctionCall(name="AskUser", arguments="{}"),
                )
            ],
        ),
        ToolMessage(id="t1", content='{"answer":"first"}', tool_call_id="call_1"),
        ToolMessage(id="t2", content='{"answer":"dup"}', tool_call_id="call_1"),
    ]
    initial = build_initial_messages(agui_messages=msgs)
    normalized, dup_ids = dedupe_litellm_tool_results(initial)
    tool_msgs = [m for m in normalized if m.get("role") == "tool" and m.get("tool_call_id") == "call_1"]
    assert len(tool_msgs) == 1
    assert dup_ids == ["call_1"]


@pytest.mark.asyncio
async def test_mcp_agent_call_litellm_applies_boundary_dedupe():
    service = MCPAgentService(
        mcp_server_configs=[{"name": "test", "url": "http://localhost"}],
        litellm_base_url="http://litellm.test",
        litellm_api_key="test-key",
    )
    captured: dict = {}

    async def fake_post(*args, **kwargs):
        captured["json"] = kwargs.get("json") or {}
        response = AsyncMock()
        response.status_code = 200
        response.json = lambda: {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {},
        }
        return response

    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"keep": true}'},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"drop": true}'},
    ]

    with patch("httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client.post = fake_post
        client_cls.return_value = client

        await service.call_litellm(messages=messages, model="gpt-4o")

    sent = captured["json"]["messages"]
    tool_msgs = [m for m in sent if m.get("role") == "tool" and m.get("tool_call_id") == "call_1"]
    assert len(tool_msgs) == 1
    assert json.loads(tool_msgs[0]["content"]) == {"keep": True}
