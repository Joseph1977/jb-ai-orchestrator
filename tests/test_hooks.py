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
from app.services.hooks import loader as hook_loader
from app.services.hooks.loader import MAX_HOOK_CONFIG_CHARS, tool_matcher_name
from app.services.harness.registry import collect_manifest
from app.services.local_tool_provider import LocalToolContext
from app.services.tool_hub import ToolExecutionHub
from app.services.workspace_io import WorkspacePath, WorkspaceReader


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


def test_hook_config_precedence_remains_cursor_then_claude(tmp_path):
    cursor = tmp_path / ".cursor"
    claude = tmp_path / ".claude"
    cursor.mkdir()
    claude.mkdir()
    (cursor / "hooks.json").write_text(
        json.dumps({"hooks": {"cursorEvent": [{"command": "true"}]}})
    )
    (claude / "hooks.json").write_text(
        json.dumps({"hooks": {"ClaudeEvent": [{"command": "false"}]}})
    )

    cfg = load_hooks_for_workspace(str(tmp_path))

    assert cfg.source == "cursor"
    assert cfg.enabled_events == ["cursorEvent"]


def test_missing_hook_configs_have_no_diagnostics(tmp_path):
    cfg = load_hooks_for_workspace(str(tmp_path))
    assert cfg.source == "none"
    assert cfg.diagnostics == []


def test_claude_settings_without_hooks_do_not_emit_hook_notes(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"model": "example-model"}))

    cfg = load_hooks_for_workspace(str(tmp_path))
    manifest = collect_manifest(str(tmp_path))

    assert cfg.source == "none"
    assert not any("hook configuration" in note.lower() for note in manifest.notes)


def test_cursor_adapter_does_not_note_hookless_claude_settings(tmp_path):
    (tmp_path / ".cursor").mkdir()
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"model": "example-model"}))

    manifest = collect_manifest(str(tmp_path))

    assert manifest.orchestration_type == "cursor"
    assert not any("hook configuration" in note.lower() for note in manifest.notes)


def test_unsafe_hook_config_is_not_loaded_or_executed(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.HOOKS_ENABLED", True)
    outside = tmp_path.parent / "outside-hooks.json"
    marker = tmp_path / "executed"
    outside.write_text(
        json.dumps(
            {
                "hooks": {
                    "preToolUse": [
                        {"command": f"touch {marker}"}
                    ]
                }
            }
        )
    )
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    os.symlink(outside, cursor / "hooks.json")

    cfg = load_hooks_for_workspace(str(tmp_path))
    decision = run(run_hooks(str(tmp_path), "preToolUse", {}))

    assert cfg.events == {}
    assert any("unreadable or unsafe" in item for item in cfg.diagnostics)
    assert decision.allowed is True
    assert not marker.exists()


def test_unsafe_hook_config_is_reported_by_adapter(tmp_path, caplog):
    outside = tmp_path.parent / "outside-hooks.json"
    outside.write_text(json.dumps({"hooks": {"event": [{"command": "true"}]}}))
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    os.symlink(outside, cursor / "hooks.json")

    with caplog.at_level("WARNING"):
        manifest = collect_manifest(str(tmp_path))

    assert manifest.orchestration_type == "cursor"
    assert any("unreadable or unsafe" in note for note in manifest.notes)
    assert caplog.text.count("Skipping symlink .cursor/hooks.json") == 1
    assert "Hook configuration" not in caplog.text


def test_symlinked_claude_hook_file_keeps_security_diagnostic(tmp_path):
    outside = tmp_path.parent / "outside-claude-hooks.json"
    outside.write_text(json.dumps({"hooks": {"PreToolUse": []}}))
    claude = tmp_path / ".claude"
    claude.mkdir()
    os.symlink(outside, claude / "hooks.json")

    cfg = load_hooks_for_workspace(str(tmp_path))
    manifest = collect_manifest(str(tmp_path))

    assert any("unreadable or unsafe" in item for item in cfg.diagnostics)
    assert any("unreadable or unsafe" in note for note in manifest.notes)


def test_symlinked_claude_root_has_one_warning_and_no_hook_cascade(
    tmp_path, caplog
):
    outside = tmp_path.parent / "outside-claude-root"
    outside.mkdir()
    (outside / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"command": "false"}]}})
    )
    os.symlink(outside, tmp_path / ".claude")

    with caplog.at_level("WARNING"):
        manifest = collect_manifest(str(tmp_path))

    assert caplog.text.count("Skipping symlink .claude") == 1
    assert "Hook configuration" not in caplog.text
    assert not any("Hook configuration" in note for note in manifest.notes)
    assert any(
        "1 unique symlinked workspace path encountered and skipped" in note
        for note in manifest.notes
    )


def test_fifo_hook_config_is_rejected_without_blocking(tmp_path):
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    os.mkfifo(cursor / "hooks.json")

    cfg = load_hooks_for_workspace(str(tmp_path))

    assert cfg.events == {}
    assert any("unreadable or unsafe" in item for item in cfg.diagnostics)


def test_oversized_hook_config_is_rejected_whole(tmp_path):
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    (cursor / "hooks.json").write_text(" " * (MAX_HOOK_CONFIG_CHARS + 1))

    cfg = load_hooks_for_workspace(str(tmp_path))

    assert cfg.events == {}
    assert any("exceeds" in item for item in cfg.diagnostics)


def test_deeply_nested_hook_json_is_rejected(tmp_path, monkeypatch):
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    (cursor / "hooks.json").write_text("{}")
    monkeypatch.setattr(
        hook_loader.json,
        "loads",
        lambda _text: (_ for _ in ()).throw(RecursionError("nested")),
    )

    cfg = load_hooks_for_workspace(str(tmp_path))

    assert cfg.events == {}
    assert any("malformed" in item for item in cfg.diagnostics)


def test_borrowed_workspace_reader_stays_open(tmp_path):
    with WorkspaceReader(tmp_path) as reader:
        load_hooks_for_workspace(str(tmp_path), reader=reader)
        assert reader.exists(WorkspacePath.parse("missing")) is False


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
