# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for AG-UI typed message conversion and authoritative system prompt."""

from ag_ui.core.types import (
    AssistantMessage,
    FunctionCall,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)

from app.services.agui_messages import (
    FRONTEND_INTERACTION_GUIDANCE,
    FRONTEND_TOOL_LIST_PREFIX,
    MANDATORY_UI_CONTRACT_BEGIN,
    MANDATORY_UI_CONTRACT_END,
    build_authoritative_system_content,
    build_initial_messages,
    compose_system_messages,
    messages_to_litellm,
)


def test_compose_system_messages_single_authoritative_block():
    incoming = [SystemMessage(id="s1", content="Use Ask-Text for user input.")]
    out = compose_system_messages(
        harness_prompt="Harness base",
        incoming_messages=incoming,
        contexts=[{"description": "thread", "value": "abc-123"}],
    )
    assert len(out) == 1
    assert out[0]["role"] == "system"
    content = out[0]["content"]
    harness_idx = content.index("Harness base")
    context_idx = content.index("Context:\nthread: abc-123")
    contract_idx = content.index(MANDATORY_UI_CONTRACT_BEGIN)
    contract_body_idx = content.index("Use Ask-Text for user input.")
    assert harness_idx < context_idx < contract_idx < contract_body_idx
    assert "MANDATORY UI CONTRACT" in content


def test_compose_system_messages_harness_only_when_no_incoming_system():
    out = compose_system_messages(harness_prompt="Standalone harness")
    assert len(out) == 1
    assert out[0]["content"] == "Standalone harness"
    assert MANDATORY_UI_CONTRACT_BEGIN not in out[0]["content"]


def test_compose_system_messages_empty_without_inputs():
    assert compose_system_messages() == []


def test_build_initial_messages_one_system_and_typed_history():
    msgs = [
        SystemMessage(id="s1", content="UI contract body"),
        UserMessage(id="u1", content="hello"),
        AssistantMessage(
            id="a1",
            content="",
            tool_calls=[
                ToolCall(
                    id="call_1",
                    function=FunctionCall(name="grep_local", arguments='{"pattern":"x"}'),
                )
            ],
        ),
        ToolMessage(id="t1", content='{"ok":true}', tool_call_id="call_1"),
    ]
    out = build_initial_messages(
        harness_prompt="Harness",
        agui_messages=msgs,
        contexts=[{"description": "env", "value": "dev"}],
    )
    roles = [m["role"] for m in out]
    assert roles.count("system") == 1
    assert roles[1:] == ["user", "assistant", "tool"]
    assert out[1]["content"] == "hello"
    assert out[2]["tool_calls"][0]["id"] == "call_1"
    assert out[3]["tool_call_id"] == "call_1"
    assert out[0]["content"].count("Harness") == 1
    assert out[0]["content"].count("UI contract body") == 1


def test_build_initial_messages_no_duplication_of_harness_or_contract():
    harness = "ROOT AGENTS INSTRUCTIONS\n" + ("line\n" * 20)
    contract = "Mandatory web contract\n" + ("rule\n" * 10)
    msgs = [SystemMessage(id="s1", content=contract), UserMessage(id="u1", content="go")]
    out = build_initial_messages(harness_prompt=harness, agui_messages=msgs)
    system = out[0]["content"]
    assert system.count("ROOT AGENTS INSTRUCTIONS") == 1
    assert system.count("Mandatory web contract") == 1
    assert system.index(harness.strip()) < system.index(MANDATORY_UI_CONTRACT_BEGIN)


def test_large_harness_plus_ui_contract_ordering():
    harness = "# Harness catalog\n" + "\n".join(f"## Skill {i}\nDetails for skill {i}." for i in range(40))
    contract = "\n".join(f"- Rule {i}: obey UI tool {i % 3}" for i in range(25))
    content = build_authoritative_system_content(
        harness_prompt=harness,
        incoming_messages=[SystemMessage(id="s1", content=contract)],
        contexts=[{"description": "workspace", "value": "/tmp/ws"}],
    )
    assert content is not None
    assert content.index(harness.strip()) == 0
    assert content.index("Context:\nworkspace: /tmp/ws") > content.index("Skill 39")
    assert content.index(MANDATORY_UI_CONTRACT_BEGIN) > content.index("workspace: /tmp/ws")
    assert content.index("Rule 24:") > content.index(MANDATORY_UI_CONTRACT_BEGIN)
    assert len(compose_system_messages(
        harness_prompt=harness,
        incoming_messages=[SystemMessage(id="s1", content=contract)],
        contexts=[{"description": "workspace", "value": "/tmp/ws"}],
    )) == 1


def test_standalone_legacy_user_when_no_typed_non_system():
    out = build_initial_messages(legacy_request="plain prompt")
    assert out == [{"role": "user", "content": "plain prompt"}]


def test_frontend_guidance_inserted_before_mandatory_contract():
    content = build_authoritative_system_content(
        harness_prompt="Harness base",
        incoming_messages=[SystemMessage(id="s1", content="UI contract body")],
        has_frontend_tools=True,
    )
    assert content is not None
    assert content.index(FRONTEND_INTERACTION_GUIDANCE) < content.index(MANDATORY_UI_CONTRACT_BEGIN)


def test_frontend_guidance_is_scoped_to_matching_tools():
    content = build_authoritative_system_content(
        harness_prompt="Harness base",
        has_frontend_tools=True,
        frontend_tool_names=["Ask-Text", "Show-Message", "Ask-Text", ""],
    )
    assert content is not None
    assert "When a provided frontend tool matches" in content
    assert "Treat workflow wording" in content
    assert "not as a requirement to collect the answer in plain text" in content
    assert "Plain assistant text remains the normal channel" in content
    assert "does not change how MCP or local tools are used" in content
    assert "Do not assume a specific frontend tool name" in content
    assert "Ask-Text" not in FRONTEND_INTERACTION_GUIDANCE
    assert (
        f'{FRONTEND_TOOL_LIST_PREFIX}["Ask-Text", "Show-Message"]'
        in content
    )


def test_frontend_guidance_absent_without_frontend_tools():
    content = build_authoritative_system_content(
        harness_prompt="Harness base",
        has_frontend_tools=False,
    )
    assert content == "Harness base"
    assert FRONTEND_INTERACTION_GUIDANCE not in content


def test_frontend_tool_names_enable_guidance_without_separate_boolean():
    content = build_authoritative_system_content(
        harness_prompt="Harness base",
        frontend_tool_names=["Ask-Choice"],
    )
    assert content is not None
    assert FRONTEND_INTERACTION_GUIDANCE in content
    assert f'{FRONTEND_TOOL_LIST_PREFIX}["Ask-Choice"]' in content


def test_assistant_tool_calls_preserved():
    msg = AssistantMessage(
        id="a1",
        content="",
        tool_calls=[
            ToolCall(
                id="call_1",
                function=FunctionCall(name="grep_local", arguments='{"pattern":"x"}'),
            )
        ],
    )
    litellm = messages_to_litellm([msg])[0]
    assert litellm["role"] == "assistant"
    assert litellm["tool_calls"][0]["id"] == "call_1"
    assert litellm["tool_calls"][0]["function"]["name"] == "grep_local"


def test_mandatory_ui_contract_delimiters_wrap_incoming_system_only():
    contract = "Rule one\nRule two"
    content = build_authoritative_system_content(
        harness_prompt="Harness base",
        incoming_messages=[SystemMessage(id="s1", content=contract)],
    )
    assert content is not None
    assert content.endswith(MANDATORY_UI_CONTRACT_END)
    assert MANDATORY_UI_CONTRACT_BEGIN in content
    assert _extract_contract_body(content) == contract


def _extract_contract_body(content: str) -> str:
    begin = content.index(MANDATORY_UI_CONTRACT_BEGIN) + len(MANDATORY_UI_CONTRACT_BEGIN)
    end = content.rindex(MANDATORY_UI_CONTRACT_END)
    return content[begin:end]
