# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Config
from app.services.local_tool_provider import LocalToolContext
from app.services.mcp_agent_service import MCPAgentService
from app.services.tool_hub import ToolExecutionHub


@pytest.mark.asyncio
async def test_consume_chat_stream_assembles_message():
    chunks = [
        'data: {"choices":[{"delta":{"role":"assistant","content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"grep","arguments":"{\\"p\\":"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]

    class FakeResponse:
        async def aiter_lines(self):
            for line in chunks:
                yield line

    svc = MCPAgentService.__new__(MCPAgentService)
    deltas = []

    async def on_delta(piece):
        deltas.append(piece)

    result = await svc._consume_chat_stream(FakeResponse(), on_delta)
    message = result["choices"][0]["message"]
    assert message["content"] == "Hello"
    assert message["tool_calls"][0]["function"]["name"] == "grep"
    assert message["tool_calls"][0]["function"]["arguments"] == '{"p":1}'
    assert deltas == ["Hel", "lo"]


async def _consume(chunks):
    class FakeResponse:
        async def aiter_lines(self):
            for line in chunks:
                yield line

    svc = MCPAgentService.__new__(MCPAgentService)
    result = await svc._consume_chat_stream(FakeResponse())
    return result["choices"][0]["message"]


@pytest.mark.asyncio
async def test_consume_chat_stream_openai_multi_tool_calls():
    """OpenAI shape: incrementing index, id+name only on the first fragment of a
    call, arguments streamed across later fragments that carry neither."""
    chunks = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a","type":"function","function":{"name":"foo","arguments":""}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"a\\":"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_b","type":"function","function":{"name":"bar","arguments":"{}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    message = await _consume(chunks)
    calls = message["tool_calls"]
    assert len(calls) == 2
    assert calls[0]["id"] == "call_a"
    assert calls[0]["function"]["name"] == "foo"
    assert calls[0]["function"]["arguments"] == '{"a":1}'
    assert calls[1]["id"] == "call_b"
    assert calls[1]["function"]["name"] == "bar"
    assert calls[1]["function"]["arguments"] == "{}"


@pytest.mark.asyncio
async def test_consume_chat_stream_ollama_multi_tool_calls_same_index():
    """Ollama (LiteLLM ollama_chat) shape: every fragment stamped index=0, but
    each call has a distinct id and its full name+arguments in one fragment.
    Previously these merged into one garbage call (concatenated names/args)."""
    chunks = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"uuid-A","type":"function","function":{"name":"AGUI-Step-Progress","arguments":"{\\"currentStep\\": \\"start\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"uuid-B","type":"function","function":{"name":"AGUI-Show-Message","arguments":"{\\"type\\": \\"info\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    message = await _consume(chunks)
    calls = message["tool_calls"]
    assert len(calls) == 2
    assert calls[0]["id"] == "uuid-A"
    assert calls[0]["function"]["name"] == "AGUI-Step-Progress"
    assert calls[0]["function"]["arguments"] == '{"currentStep": "start"}'
    assert calls[1]["id"] == "uuid-B"
    assert calls[1]["function"]["name"] == "AGUI-Show-Message"
    assert calls[1]["function"]["arguments"] == '{"type": "info"}'


@pytest.mark.asyncio
async def test_subagent_respects_max_depth(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "SUBAGENT_ENABLED", True)
    monkeypatch.setattr(Config, "SUBAGENT_MAX_DEPTH", 1)
    hub = ToolExecutionHub(
        mcp_service=MagicMock(),
        agui_service=MagicMock(),
        agui_event_service=MagicMock(),
    )
    ctx = LocalToolContext(workspace_path=str(tmp_path), subagent_depth=1)
    out = await hub._run_subagent(
        {"prompt": "review this"},
        local_context=ctx,
        model="gpt-4o",
        lite_llm_timeout=30,
        parent_agui=None,
    )
    assert "error" in out
    assert "max depth" in out["error"].lower()


@pytest.mark.asyncio
async def test_subagent_loads_agent_md_and_returns_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "SUBAGENT_ENABLED", True)
    monkeypatch.setattr(Config, "SUBAGENT_MAX_DEPTH", 2)
    monkeypatch.setattr(Config, "SUBAGENT_MAX_TOOL_CALLS", 3)
    agents = tmp_path / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_text("# Reviewer\nBe strict.\n", encoding="utf-8")

    hub = ToolExecutionHub(
        mcp_service=MagicMock(),
        agui_service=MagicMock(),
        agui_event_service=MagicMock(),
    )
    hub.process_request = AsyncMock(
        return_value={
            "success": True,
            "response": "Looks good with one nit.",
            "tool_calls_made": 2,
            "total_tokens": 10,
        }
    )
    output_backend = object()
    ctx = LocalToolContext(
        workspace_path=str(tmp_path),
        subagent_depth=0,
        mode="workflow",
        output_backend=output_backend,
    )
    out = await hub._run_subagent(
        {"prompt": "Review src/", "agent": "reviewer", "description": "code review"},
        local_context=ctx,
        model="gpt-4o",
        lite_llm_timeout=30,
        parent_agui=None,
    )
    assert out["success"] is True
    assert "Looks good" in out["summary"]
    call_kwargs = hub.process_request.await_args.kwargs
    assert "Be strict" in call_kwargs["system_prompt"]
    assert call_kwargs["local_context"].subagent_depth == 1
    assert call_kwargs["local_context"].mode == "workflow"
    assert call_kwargs["local_context"].output_backend is output_backend
    assert call_kwargs["include_agui_tools"] is False


def test_load_agent_system_prompt_by_path(tmp_path):
    hub = ToolExecutionHub(MagicMock(), MagicMock(), MagicMock())
    path = tmp_path / "agents" / "sec.md"
    path.parent.mkdir(parents=True)
    path.write_text("Security focus", encoding="utf-8")
    ctx = LocalToolContext(workspace_path=str(tmp_path))
    text = hub._load_agent_system_prompt("agents/sec.md", ctx)
    assert text == "Security focus"
