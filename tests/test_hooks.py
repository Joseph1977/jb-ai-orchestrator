# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import os

from app.services.hooks import (
    HookDecision,
    is_hook_ask_approved,
    load_hooks_for_workspace,
    run_hooks,
)
from app.services.hooks.loader import tool_matcher_name
from app.services.local_tool_provider import LocalToolContext
from app.services.tool_hub import ToolExecutionHub


def run(coro):
    return asyncio.run(coro)


class _Dummy:
    pass


def _hub() -> ToolExecutionHub:
    return ToolExecutionHub(
        mcp_service=_Dummy(),
        agui_service=_Dummy(),
        agui_event_service=_Dummy(),
    )


def test_tool_matcher_name():
    assert tool_matcher_name("execute_local") == "Shell"
    assert tool_matcher_name("read_file_local") == "Read"
    assert tool_matcher_name("edit_file_local") == "Write"
    assert tool_matcher_name("task_local") == "Task"


def test_load_cursor_hooks(tmp_path):
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    script = cursor / "deny.sh"
    script.write_text("#!/bin/sh\necho '{\"permission\":\"deny\",\"userMessage\":\"nope\"}'\n")
    os.chmod(script, 0o755)
    (cursor / "hooks.json").write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "beforeShellExecution": [
                        {"command": "./deny.sh", "failClosed": True}
                    ]
                },
            }
        )
    )
    cfg = load_hooks_for_workspace(str(tmp_path))
    assert cfg.source == "cursor"
    assert "beforeShellExecution" in cfg.events


def test_before_shell_hook_blocks_via_hub(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.HOOKS_ENABLED", True)
    monkeypatch.setattr("app.config.Config.LOCAL_SHELL_ENABLED", True)
    monkeypatch.setattr("app.config.Config.PATH_POLICY_ENABLED", False)
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    script = cursor / "deny.sh"
    script.write_text("#!/bin/sh\necho '{\"permission\":\"deny\",\"userMessage\":\"blocked\"}'\n")
    os.chmod(script, 0o755)
    (cursor / "hooks.json").write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "beforeShellExecution": [
                        {"command": str(script), "failClosed": True}
                    ]
                },
            }
        )
    )
    ctx = LocalToolContext(workspace_path=str(tmp_path), in_place=True)
    result = run(
        _hub()._execute_local_with_hooks(
            function_name="execute_local",
            function_args={"command": "echo hi"},
            tool_call_id="c1",
            local_context=ctx,
            model="gpt-4o",
            lite_llm_timeout=30,
            parent_agui=None,
        )
    )
    assert result.get("hookBlocked") is True
    assert "blocked" in (result.get("error") or "").lower()


def test_hook_ask_returns_sentinel(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.HOOKS_ENABLED", True)
    monkeypatch.setattr("app.config.Config.LOCAL_SHELL_ENABLED", True)
    monkeypatch.setattr("app.config.Config.PATH_POLICY_ENABLED", False)
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    script = cursor / "ask.sh"
    script.write_text(
        "#!/bin/sh\necho '{\"permission\":\"ask\",\"userMessage\":\"confirm?\"}'\n"
    )
    os.chmod(script, 0o755)
    (cursor / "hooks.json").write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "beforeShellExecution": [{"command": str(script)}]
                },
            }
        )
    )
    ctx = LocalToolContext(workspace_path=str(tmp_path), in_place=True)
    result = run(
        _hub()._execute_local_with_hooks(
            function_name="execute_local",
            function_args={"command": "echo hi"},
            tool_call_id="c1",
            local_context=ctx,
            model="gpt-4o",
            lite_llm_timeout=30,
            parent_agui=None,
        )
    )
    assert result.get("_hookAsk") is True
    assert result.get("permission") == "ask"


def test_is_hook_ask_approved():
    assert is_hook_ask_approved({"result": {"decision": "approve"}}) is True
    assert is_hook_ask_approved({"result": {"decision": "deny", "reason": "no"}}) is False
    assert is_hook_ask_approved({"status": "cancelled", "cancelled": True}) is False
    # Legacy approved:boolean alias
    assert is_hook_ask_approved({"approved": True}) is True
    assert is_hook_ask_approved({"approved": False}) is False
    assert is_hook_ask_approved('{"approved": true}') is True
    assert is_hook_ask_approved({"permission": "allow"}) is True
    assert is_hook_ask_approved("deny") is False
    assert HookDecision.ask("x").needs_confirmation is True


def test_parse_hook_permission_decision():
    from app.services.hooks import parse_hook_permission_decision

    assert parse_hook_permission_decision({"result": {"decision": "approve"}}) == "approve"
    assert parse_hook_permission_decision({"result": {"decision": "deny"}}) == "deny"
    assert parse_hook_permission_decision({"status": "cancelled", "cancelled": True}) == "deny"
    assert parse_hook_permission_decision({"approved": True}) == "approve"


def test_run_hooks_allow_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.HOOKS_ENABLED", False)
    decision = run(run_hooks(str(tmp_path), "preToolUse", {"tool_name": "Read"}))
    assert decision.allowed is True


def test_before_submit_prompt_can_deny(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.HOOKS_ENABLED", True)
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    script = cursor / "deny.sh"
    script.write_text("#!/bin/sh\necho '{\"permission\":\"deny\",\"userMessage\":\"no prompts\"}'\n")
    os.chmod(script, 0o755)
    (cursor / "hooks.json").write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {"beforeSubmitPrompt": [{"command": str(script)}]},
            }
        )
    )
    decision = run(
        run_hooks(str(tmp_path), "beforeSubmitPrompt", {"prompt": "hello"})
    )
    assert decision.allowed is False


def test_load_claude_nested_hooks(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {"type": "command", "command": "echo '{}'"}
                            ],
                        }
                    ]
                }
            }
        )
    )
    cfg = load_hooks_for_workspace(str(tmp_path))
    assert cfg.source == "claude"
    assert "preToolUse" in cfg.events
    assert cfg.events["preToolUse"][0].matcher == "Bash"


def test_pre_compact_hook_runs(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.HOOKS_ENABLED", True)
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    script = cursor / "compact.sh"
    script.write_text("#!/bin/sh\necho '{\"permission\":\"deny\",\"userMessage\":\"skip compact\"}'\n")
    os.chmod(script, 0o755)
    (cursor / "hooks.json").write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {"preCompact": [{"command": str(script)}]},
            }
        )
    )
    decision = run(
        run_hooks(
            str(tmp_path),
            "preCompact",
            {"message_count": 10, "estimated_chars": 99999},
        )
    )
    assert decision.allowed is False
