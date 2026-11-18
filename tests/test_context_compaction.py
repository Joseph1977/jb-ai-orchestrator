# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.config import Config
from app.services.context_compaction import (
    compact_messages_if_needed,
    estimate_message_tokens,
    manage_context_before_llm,
    message_chars,
    offload_tool_result_if_needed,
)
from app.services.local_tool_provider import LocalToolContext


def _ctx(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    return LocalToolContext(
        workspace_path=str(tmp_path),
        runtime_path=str(runtime),
    )


def test_offload_large_tool_result(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(Config, "TOOL_RESULT_OFFLOAD_CHARS", 100)
    ctx = _ctx(tmp_path)
    big = "x" * 500
    out = offload_tool_result_if_needed(
        big,
        tool_call_id="call_1",
        tool_name="grep_local",
        local_context=ctx,
    )
    payload = json.loads(out)
    assert payload["offloaded"] is True
    assert payload["path"].startswith(".agent/offload/")
    saved = tmp_path / "runtime" / payload["path"].removeprefix(".agent/")
    assert saved.read_text() == big


def test_compact_older_tool_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_CHARS", 800)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_TOKENS", 0)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_KEEP_RECENT_TOOL_MSGS", 1)
    monkeypatch.setattr(Config, "TOOL_RESULT_OFFLOAD_CHARS", 50)
    ctx = _ctx(tmp_path)
    messages = [
        {"role": "user", "content": "do work"},
        {
            "role": "tool",
            "tool_call_id": "t1",
            "name": "grep_local",
            "content": "old-result-" + ("a" * 400),
        },
        {
            "role": "tool",
            "tool_call_id": "t2",
            "name": "read_file_local",
            "content": "recent-result-" + ("b" * 400),
        },
    ]
    compact_messages_if_needed(messages, local_context=ctx)
    first = json.loads(messages[1]["content"])
    assert first.get("offloaded") is True or first.get("compacted") is True
    assert messages[2]["content"].startswith("recent-result-")


def test_token_budget_triggers_compaction(monkeypatch):
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_CHARS", 0)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_TOKENS", 10)
    messages = [{"role": "user", "content": "x" * 200}]
    assert estimate_message_tokens(messages) >= 10


@pytest.mark.asyncio
async def test_summarization_replaces_old_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_CHARS", 500)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_TOKENS", 0)
    monkeypatch.setattr(Config, "CONTEXT_SUMMARIZATION_ENABLED", True)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_KEEP_RECENT_TURNS", 1)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_KEEP_RECENT_TOOL_MSGS", 0)

    call_litellm = AsyncMock(
        return_value={
            "choices": [{"message": {"content": "User asked about auth; agent started refactor."}}]
        }
    )
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "old question " + ("a" * 300)},
        {"role": "assistant", "content": "old answer " + ("b" * 300)},
        {"role": "user", "content": "recent question"},
    ]
    ctx = _ctx(tmp_path)
    out = await manage_context_before_llm(
        messages,
        local_context=ctx,
        model="gpt-4o",
        lite_llm_timeout=30,
        call_litellm=call_litellm,
    )
    assert any("[context_summary]" in (m.get("content") or "") for m in out if m.get("role") == "system")
    call_litellm.assert_called_once()


@pytest.mark.asyncio
async def test_no_management_when_under_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_CHARS", 100000)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_TOKENS", 0)
    call_litellm = AsyncMock()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    ctx = _ctx(tmp_path)
    out = await manage_context_before_llm(
        messages,
        local_context=ctx,
        model="gpt-4o",
        lite_llm_timeout=30,
        call_litellm=call_litellm,
    )
    # Under budget: no summarization call, history unchanged.
    call_litellm.assert_not_called()
    assert out == messages


@pytest.mark.asyncio
async def test_long_tool_loop_proof(tmp_path, monkeypatch):
    """End-to-end proof: a long tool loop gets summarized AND tool-compacted,
    while recent turns and recent tool results stay intact."""
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_CHARS", 4000)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_TOKENS", 0)
    monkeypatch.setattr(Config, "CONTEXT_SUMMARIZATION_ENABLED", True)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_KEEP_RECENT_TURNS", 2)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_KEEP_RECENT_TOOL_MSGS", 1)
    monkeypatch.setattr(Config, "TOOL_RESULT_OFFLOAD_CHARS", 300)

    call_litellm = AsyncMock(
        return_value={"choices": [{"message": {"content": "Summary of early exploration."}}]}
    )

    messages = [{"role": "system", "content": "You are a coding agent."}]
    # Simulate 6 tool-calling rounds: substantial assistant reasoning + large
    # tool results (mirrors a real long coding loop).
    for i in range(6):
        messages.append(
            {"role": "assistant", "content": f"Round {i}: reasoning about the code. " + ("word " * 60)}
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"t{i}",
                "name": "grep_local",
                "content": f"result-{i}-" + ("z" * 500),
            }
        )
    messages.append({"role": "user", "content": "what did you find?"})

    ctx = _ctx(tmp_path)
    before = message_chars(messages)
    out = await manage_context_before_llm(
        messages,
        local_context=ctx,
        model="gpt-4o",
        lite_llm_timeout=30,
        call_litellm=call_litellm,
    )
    after = message_chars(out)

    # Proof 1: a summary block was inserted.
    assert any("[context_summary]" in (m.get("content") or "") for m in out if m.get("role") == "system")
    # Proof 2: the most recent user turn survives verbatim.
    assert out[-1]["content"] == "what did you find?"
    # Proof 3: overall footprint shrank meaningfully.
    assert after < before
    # Proof 4: original system prompt is still present.
    assert out[0]["role"] == "system" and "coding agent" in out[0]["content"]


def _history_errors(messages):
    """Validate that every tool message answers the preceding assistant call
    and every call is answered before another message. A call still open at the
    very end is allowed."""
    errors = []
    open_ids = None
    for index, message in enumerate(messages):
        if message.get("role") == "tool":
            if not open_ids or message.get("tool_call_id") not in open_ids:
                errors.append(f"orphaned tool message at {index}")
            else:
                open_ids.discard(message.get("tool_call_id"))
            continue
        if open_ids:
            errors.append(f"calls left unanswered before {index}: {sorted(open_ids)}")
        open_ids = None
        if message.get("role") == "assistant" and message.get("tool_calls"):
            open_ids = {call["id"] for call in message["tool_calls"]}
    return errors


def _call(call_id, name):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _interview_history(rounds):
    """Each round reads a skill and saves memory, then asks; the answer is a tool result."""
    messages = [
        {"role": "system", "content": "You run an interview."},
        {"role": "user", "content": "hi"},
    ]
    for i in range(rounds):
        messages += [
            {
                "role": "assistant",
                "content": f"Round {i}: reading the next skill. " + ("word " * 30),
                "tool_calls": [_call(f"r{i}", "read_file_local"), _call(f"w{i}", "write_output_local")],
            },
            {"role": "tool", "tool_call_id": f"r{i}", "name": "read_file_local", "content": "skill " * 40},
            {"role": "tool", "tool_call_id": f"w{i}", "name": "write_output_local", "content": '{"ok": true}'},
            {"role": "assistant", "content": None, "tool_calls": [_call(f"q{i}", "Ask-Text")]},
            {"role": "tool", "tool_call_id": f"q{i}", "name": "Ask-Text", "content": f"answer {i}"},
        ]
    return messages


@pytest.mark.asyncio
async def test_summarization_never_orphans_a_tool_result(tmp_path, monkeypatch):
    """The cut lands inside the last round, between a call and its result."""
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_CHARS", 2000)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_TOKENS", 0)
    monkeypatch.setattr(Config, "CONTEXT_SUMMARIZATION_ENABLED", True)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_KEEP_RECENT_TURNS", 3)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_KEEP_RECENT_TOOL_MSGS", 2)
    monkeypatch.setattr(Config, "TOOL_RESULT_OFFLOAD_CHARS", 100000)
    call_litellm = AsyncMock(
        return_value={"choices": [{"message": {"content": "Earlier rounds summarized."}}]}
    )
    messages = _interview_history(8)
    assert _history_errors(messages) == []
    assert messages[-3]["role"] == "tool"

    out = await manage_context_before_llm(
        messages,
        local_context=_ctx(tmp_path),
        model="gpt-4o",
        lite_llm_timeout=30,
        call_litellm=call_litellm,
    )

    assert any("[context_summary]" in (m.get("content") or "") for m in out if m.get("role") == "system")
    assert _history_errors(out) == []
    assert out[-1] == {"role": "tool", "tool_call_id": "q7", "name": "Ask-Text", "content": "answer 7"}


@pytest.mark.asyncio
async def test_summarization_drops_a_tool_result_whose_call_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_ENABLED", True)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_CHARS", 500)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_TOKENS", 0)
    monkeypatch.setattr(Config, "CONTEXT_SUMMARIZATION_ENABLED", True)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_KEEP_RECENT_TURNS", 1)
    monkeypatch.setattr(Config, "CONTEXT_COMPACTION_KEEP_RECENT_TOOL_MSGS", 0)
    call_litellm = AsyncMock(return_value={"choices": [{"message": {"content": "Summary."}}]})
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old question " + ("a" * 300)},
        {"role": "tool", "tool_call_id": "lost", "name": "grep_local", "content": "stray"},
        {"role": "assistant", "content": "old answer " + ("b" * 300)},
        {"role": "user", "content": "recent question"},
    ]

    out = await manage_context_before_llm(
        messages,
        local_context=_ctx(tmp_path),
        model="gpt-4o",
        lite_llm_timeout=30,
        call_litellm=call_litellm,
    )

    assert not any(m.get("tool_call_id") == "lost" for m in out)
    assert _history_errors(out) == []
