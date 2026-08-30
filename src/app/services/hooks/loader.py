# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Load Cursor/Claude hook definitions from a workspace."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.workspace_io import (
    ReaderUnavailableError,
    UnsafePathError,
    WorkspacePath,
    WorkspaceReader,
)
from app.utils.logger import logger

MAX_HOOK_CONFIG_CHARS = 64_000

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
    diagnostics: List[str] = field(default_factory=list)

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


def _config_dir(workspace_path: str, wp: WorkspacePath) -> Path:
    return Path(workspace_path).joinpath(*wp.parent.parts)


def _read_candidate(
    reader: WorkspaceReader,
    wp: WorkspacePath,
    diagnostics: List[str],
) -> Optional[str]:
    try:
        text, overflowed = reader.read_strict(wp, MAX_HOOK_CONFIG_CHARS)
    except FileNotFoundError:
        return None
    except (OSError, UnsafePathError) as exc:
        message = f"Hook configuration {wp.posix} is unreadable or unsafe; ignored"
        diagnostics.append(message)
        logger.warning("%s: %s", message, exc)
        return None
    if overflowed:
        message = (
            f"Hook configuration {wp.posix} exceeds {MAX_HOOK_CONFIG_CHARS} "
            "characters; ignored"
        )
        diagnostics.append(message)
        logger.warning(message)
        return None
    return text


def _parse_json(
    text: str,
    wp: WorkspacePath,
    diagnostics: List[str],
) -> Optional[Any]:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        message = f"Hook configuration {wp.posix} is malformed; ignored"
        diagnostics.append(message)
        logger.warning("%s: %s", message, exc)
        return None


def _load_hooks(
    workspace_path: str,
    reader: WorkspaceReader,
) -> HookConfig:
    diagnostics: List[str] = []
    cursor_wp = WorkspacePath.parse(".cursor/hooks.json")
    cursor_text = _read_candidate(reader, cursor_wp, diagnostics)
    if cursor_text is not None:
        data = _parse_json(cursor_text, cursor_wp, diagnostics)
        if data is not None:
            config_dir = _config_dir(workspace_path, cursor_wp)
            cfg = _parse_cursor_hooks(
                data if isinstance(data, dict) else {},
                config_dir,
            )
            cfg.diagnostics.extend(diagnostics)
            logger.info(
                "Loaded Cursor hooks from %s events=%s",
                cursor_wp,
                cfg.enabled_events,
            )
            return cfg

    claude_detected = False
    for wp in (
        WorkspacePath.parse(".claude/hooks.json"),
        WorkspacePath.parse(".claude/settings.json"),
    ):
        text = _read_candidate(reader, wp, diagnostics)
        if text is None:
            continue
        claude_detected = True
        data = _parse_json(text, wp, diagnostics)
        if data is None:
            continue
        if not isinstance(data, dict) or "hooks" not in data:
            continue
        cfg = _parse_claude_hooks(data, _config_dir(workspace_path, wp))
        if cfg.events:
            cfg.diagnostics.extend(diagnostics)
            logger.info(
                "Loaded Claude hooks from %s events=%s",
                wp,
                cfg.enabled_events,
            )
            return cfg

    return HookConfig(
        source="claude" if claude_detected else "none",
        events={},
        diagnostics=diagnostics,
    )


def load_hooks_for_workspace(
    workspace_path: str,
    *,
    reader: Optional[WorkspaceReader] = None,
) -> HookConfig:
    """Load bounded project hook configuration through workspace-safe I/O.

    Cursor and Claude locations are adapter conventions normalized here; the
    reader itself has no provider knowledge. A supplied reader is borrowed and
    remains open. Runtime callers without a discovery context get a temporary
    reader owned by this function.
    """
    owned_reader = reader is None
    if reader is None:
        try:
            reader = WorkspaceReader(Path(workspace_path))
        except (OSError, ReaderUnavailableError) as exc:
            message = "Hook configuration could not be read safely; hooks disabled"
            logger.warning("%s: %s", message, exc)
            return HookConfig(
                source="none",
                events={},
                diagnostics=[message],
            )
    try:
        return _load_hooks(workspace_path, reader)
    finally:
        if owned_reader:
            reader.close()


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
