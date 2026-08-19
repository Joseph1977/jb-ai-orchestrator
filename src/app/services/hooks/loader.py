# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Load Cursor/Claude hook definitions from a workspace."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.utils.logger import logger

# Claude Code uses PascalCase event names; map to Cursor camelCase.
_CLAUDE_EVENT_MAP = {
    "PreToolUse": "preToolUse",
    "PostToolUse": "postToolUse",
    "PostToolUseFailure": "postToolUseFailure",
    "Notification": "notification",
    "Stop": "stop",
    "SubagentStop": "subagentStop",
    "PreCompact": "preCompact",
    "UserPromptSubmit": "beforeSubmitPrompt",
    "SessionStart": "sessionStart",
    "SessionEnd": "sessionEnd",
}


@dataclass
class HookCommand:
    command: str
    matcher: Optional[str] = None
    timeout: Optional[int] = None
    fail_closed: Optional[bool] = None


@dataclass
class HookConfig:
    """Normalized hook registry: event name → command list."""

    source: str  # cursor | claude | none
    events: Dict[str, List[HookCommand]] = field(default_factory=dict)
    config_dir: Optional[str] = None  # directory containing hooks.json (for rel paths)

    @property
    def enabled_events(self) -> List[str]:
        return sorted(self.events.keys())


def _parse_cursor_hooks(data: dict, config_dir: Path) -> HookConfig:
    hooks_raw = data.get("hooks") or {}
    events: Dict[str, List[HookCommand]] = {}
    if isinstance(hooks_raw, dict):
        for event, entries in hooks_raw.items():
            if not isinstance(entries, list):
                continue
            cmds: List[HookCommand] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                command = entry.get("command")
                if not command:
                    continue
                cmds.append(
                    HookCommand(
                        command=str(command),
                        matcher=entry.get("matcher"),
                        timeout=entry.get("timeout"),
                        fail_closed=entry.get("failClosed"),
                    )
                )
            if cmds:
                events[str(event)] = cmds
    return HookConfig(source="cursor", events=events, config_dir=str(config_dir))


def _parse_claude_hooks(data: dict, config_dir: Path) -> HookConfig:
    """Normalize Claude Code settings/hooks.json into Cursor-style events.

    Claude nests ``hooks: [{type, command}]`` under matcher groups; we flatten
    to a single command list per event.
    """
    hooks_raw = data.get("hooks") or {}
    events: Dict[str, List[HookCommand]] = {}
    if not isinstance(hooks_raw, dict):
        return HookConfig(source="claude", events={}, config_dir=str(config_dir))

    for event_key, groups in hooks_raw.items():
        event = _CLAUDE_EVENT_MAP.get(str(event_key), str(event_key))
        # Cursor-style flat list
        if isinstance(groups, list) and groups and isinstance(groups[0], dict) and "command" in groups[0]:
            cmds = [
                HookCommand(
                    command=str(g["command"]),
                    matcher=g.get("matcher"),
                    timeout=g.get("timeout"),
                    fail_closed=g.get("failClosed"),
                )
                for g in groups
                if isinstance(g, dict) and g.get("command")
            ]
            if cmds:
                events[event] = cmds
            continue
        # Claude nested matcher groups
        if not isinstance(groups, list):
            continue
        cmds = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher")
            nested = group.get("hooks") or []
            if not isinstance(nested, list):
                continue
            for hook in nested:
                if not isinstance(hook, dict):
                    continue
                if hook.get("type") and hook.get("type") != "command":
                    continue
                command = hook.get("command")
                if not command:
                    continue
                cmds.append(
                    HookCommand(
                        command=str(command),
                        matcher=matcher,
                        timeout=hook.get("timeout"),
                        fail_closed=hook.get("failClosed"),
                    )
                )
        if cmds:
            events[event] = cmds
    return HookConfig(source="claude", events=events, config_dir=str(config_dir))


def load_hooks_for_workspace(workspace_path: str) -> HookConfig:
    """Load project hooks from ``.cursor/hooks.json`` or Claude hook files."""
    root = Path(workspace_path)
    cursor_hooks = root / ".cursor" / "hooks.json"
    if cursor_hooks.is_file():
        try:
            data = json.loads(cursor_hooks.read_text(encoding="utf-8"))
            cfg = _parse_cursor_hooks(data if isinstance(data, dict) else {}, cursor_hooks.parent)
            logger.info(
                "Loaded Cursor hooks from %s events=%s",
                cursor_hooks,
                cfg.enabled_events,
            )
            return cfg
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to parse %s: %s", cursor_hooks, exc)

    claude_hooks = root / ".claude" / "hooks.json"
    claude_settings = root / ".claude" / "settings.json"
    for path in (claude_hooks, claude_settings):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "hooks" not in data:
                continue
            cfg = _parse_claude_hooks(data, path.parent)
            if cfg.events:
                logger.info(
                    "Loaded Claude hooks from %s events=%s",
                    path,
                    cfg.enabled_events,
                )
                return cfg
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to parse %s: %s", path, exc)

    return HookConfig(source="none", events={})


def tool_matcher_name(function_name: str) -> str:
    """Map local/MCP tool names to Cursor matcher categories."""
    base = function_name or ""
    if base.endswith("_local"):
        base = base[: -len("_local")]
    mapping = {
        "execute": "Shell",
        "read_file": "Read",
        "list_files": "Read",
        "glob": "Read",
        "grep": "Read",
        "write_file": "Write",
        "create_file": "Write",
        "create_folder": "Write",
        "edit_file": "Write",
        "write_todos": "Write",
        "task": "Task",
        "ask_user": "AskUser",
    }
    return mapping.get(base, base)
