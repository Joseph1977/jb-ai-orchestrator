# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Execute Cursor/Claude command hooks over JSON stdin/stdout."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import Config
from app.services.hooks.loader import HookCommand, HookConfig, load_hooks_for_workspace
from app.utils.logger import logger


@dataclass
class HookDecision:
    allowed: bool = True
    permission: str = "allow"  # allow | deny | ask
    user_message: str = ""
    agent_message: str = ""
    continue_loop: bool = True
    needs_confirmation: bool = False

    @classmethod
    def allow(cls) -> "HookDecision":
        return cls()

    @classmethod
    def deny(cls, message: str = "Blocked by hook") -> "HookDecision":
        return cls(
            allowed=False,
            permission="deny",
            user_message=message,
            agent_message=message,
            continue_loop=False,
        )

    @classmethod
    def ask(cls, message: str = "Hook requested confirmation") -> "HookDecision":
        return cls(
            allowed=False,
            permission="ask",
            user_message=message,
            agent_message=message,
            continue_loop=False,
            needs_confirmation=True,
        )


def _matcher_hits(matcher: Optional[str], subject: str) -> bool:
    if not matcher:
        return True
    try:
        return re.search(str(matcher), subject or "") is not None
    except re.error:
        return str(matcher) in (subject or "")


def _resolve_command(command: str, config_dir: Optional[str], workspace: Path) -> str:
    """Resolve relative hook commands against the hooks config directory."""
    cmd = command.strip()
    if not cmd:
        return cmd
    # Absolute or looks like a shell pipeline — leave as-is.
    if cmd.startswith("/") or " " in cmd or "|" in cmd or "&&" in cmd:
        # Still rewrite a leading relative script if the first token is relative.
        first = cmd.split(None, 1)[0]
        if first.startswith("./") or (not first.startswith("/") and "/" in first):
            base = Path(config_dir) if config_dir else workspace
            script = (base / first).resolve()
            if script.exists():
                rest = cmd[len(first) :].lstrip()
                return f"{script} {rest}".rstrip()
        return cmd
    if cmd.startswith("./") or "/" in cmd:
        base = Path(config_dir) if config_dir else workspace
        script = (base / cmd).resolve()
        return str(script)
    return cmd


async def _run_one(
    hook: HookCommand,
    *,
    payload: Dict[str, Any],
    workspace: Path,
    config_dir: Optional[str],
) -> HookDecision:
    timeout = hook.timeout if hook.timeout is not None else Config.HOOKS_TIMEOUT_SEC
    timeout = max(1, int(timeout))
    fail_closed = (
        Config.HOOKS_FAIL_CLOSED if hook.fail_closed is None else bool(hook.fail_closed)
    )
    command = _resolve_command(hook.command, config_dir, workspace)
    stdin_data = json.dumps(payload).encode("utf-8")
    max_out = Config.HOOKS_MAX_OUTPUT_BYTES

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(workspace),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(stdin_data),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            msg = f"Hook timed out after {timeout}s: {hook.command}"
            logger.warning(msg)
            return HookDecision.deny(msg) if fail_closed else HookDecision.allow()
    except OSError as exc:
        msg = f"Hook failed to start ({hook.command}): {exc}"
        logger.warning(msg)
        return HookDecision.deny(msg) if fail_closed else HookDecision.allow()

    stdout = (stdout_b or b"")[:max_out].decode(errors="replace").strip()
    stderr = (stderr_b or b"")[:max_out].decode(errors="replace").strip()
    if stderr:
        logger.debug("Hook stderr (%s): %s", hook.command, stderr[:500])

    if proc.returncode not in (0, None):
        msg = f"Hook exited {proc.returncode}: {hook.command}"
        if stderr:
            msg = f"{msg} — {stderr[:300]}"
        logger.warning(msg)
        return HookDecision.deny(msg) if fail_closed else HookDecision.allow()

    if not stdout:
        return HookDecision.allow()

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        # Non-JSON stdout is treated as observational (allow).
        return HookDecision.allow()

    if not isinstance(data, dict):
        return HookDecision.allow()

    permission = str(data.get("permission") or "allow").lower()
    continue_loop = data.get("continue", True)
    if continue_loop is False:
        permission = "deny"
    user_message = str(data.get("userMessage") or data.get("user_message") or "")
    agent_message = str(data.get("agentMessage") or data.get("agent_message") or "")
    # Claude-style nested decision
    decision = data.get("hookSpecificOutput") or data.get("permissionDecision")
    if isinstance(decision, dict):
        nested = str(decision.get("permissionDecision") or decision.get("permission") or "").lower()
        if nested:
            permission = nested
        user_message = user_message or str(decision.get("permissionDecisionReason") or "")

    if permission in ("deny", "block"):
        return HookDecision(
            allowed=False,
            permission="deny",
            user_message=user_message or "Blocked by hook",
            agent_message=agent_message or user_message or "Blocked by hook",
            continue_loop=False,
        )
    if permission == "ask":
        return HookDecision.ask(
            user_message or agent_message or "Hook requested confirmation"
        )

    return HookDecision(
        allowed=True,
        permission="allow",
        user_message=user_message,
        agent_message=agent_message,
        continue_loop=True,
    )


async def run_hooks(
    workspace_path: str,
    event: str,
    payload: Dict[str, Any],
    *,
    matcher_subject: str = "",
    config: Optional[HookConfig] = None,
) -> HookDecision:
    """Run all hooks registered for ``event``. First deny wins."""
    if not Config.HOOKS_ENABLED:
        return HookDecision.allow()

    cfg = config or load_hooks_for_workspace(workspace_path)
    hooks: List[HookCommand] = list(cfg.events.get(event) or [])
    if not hooks:
        return HookDecision.allow()

    workspace = Path(workspace_path).resolve()
    body = {
        **payload,
        "hook_event_name": event,
        "workspace_roots": [str(workspace)],
    }

    for hook in hooks:
        if not _matcher_hits(hook.matcher, matcher_subject):
            continue
        decision = await _run_one(
            hook,
            payload=body,
            workspace=workspace,
            config_dir=cfg.config_dir,
        )
        if not decision.allowed:
            return decision
    return HookDecision.allow()


def is_hook_ask_approved(resume_tool_result: Any) -> bool:
    """Interpret a HITL resume payload for a hook ``permission: ask`` pause."""
    return parse_hook_permission_decision(resume_tool_result) == "approve"


def hook_permission_deny_reason(resume_tool_result: Any) -> Optional[str]:
    """Optional user-provided reason when decision is deny."""
    payload = _hook_permission_payload(resume_tool_result)
    if not payload:
        return None
    reason = payload.get("reason")
    if reason is None:
        return None
    text = str(reason).strip()
    return text or None


def parse_hook_permission_decision(resume_tool_result: Any) -> Optional[str]:
    """Return canonical ``approve``/``deny``, or None when cancelled/unparseable."""
    if isinstance(resume_tool_result, str):
        text = resume_tool_result.strip()
        try:
            resume_tool_result = json.loads(text)
        except json.JSONDecodeError:
            lowered = text.lower()
            if lowered in ("allow", "approve", "approved", "yes", "y"):
                return "approve"
            if lowered in ("deny", "denied", "decline", "declined", "no", "n"):
                return "deny"
            return None
    if not isinstance(resume_tool_result, dict):
        return None
    if resume_tool_result.get("status") == "cancelled" or resume_tool_result.get("cancelled"):
        return "deny"
    if resume_tool_result.get("error"):
        return "deny"

    payload = _hook_permission_payload(resume_tool_result)
    if payload:
        decision = payload.get("decision")
        if isinstance(decision, str):
            normalized = decision.strip().lower()
            if normalized in ("approve", "deny"):
                return normalized
        # Temporary compatibility alias for legacy clients.
        if "approved" in payload:
            return "approve" if bool(payload.get("approved")) else "deny"

    permission = str(resume_tool_result.get("permission") or "").lower()
    if permission in ("allow", "approve"):
        return "approve"
    if permission in ("deny", "block", "decline"):
        return "deny"

    nested = resume_tool_result.get("result")
    if nested is not None and nested is not resume_tool_result:
        if isinstance(nested, dict):
            return parse_hook_permission_decision(nested)
        if isinstance(nested, str):
            return parse_hook_permission_decision(nested)
    return None


def _hook_permission_payload(resume_tool_result: Any) -> Optional[dict]:
    """Extract the hook-permission decision object from a resume response."""
    if not isinstance(resume_tool_result, dict):
        return None
    inner = resume_tool_result.get("result")
    if isinstance(inner, dict):
        if "decision" in inner or "approved" in inner or "reason" in inner:
            return inner
        nested = inner.get("result")
        if isinstance(nested, dict):
            return nested
    if "decision" in resume_tool_result or "approved" in resume_tool_result:
        return resume_tool_result
    return None


def hook_ask_sentinel(
    *,
    event: str,
    decision: HookDecision,
) -> Dict[str, Any]:
    """Marker returned by local tools / hub when a hook needs human confirmation."""
    return {
        "_hookAsk": True,
        "event": event,
        "permission": "ask",
        "message": decision.user_message or decision.agent_message or "Confirmation required",
        "userMessage": decision.user_message,
        "agentMessage": decision.agent_message,
    }
