# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Convert AG-UI typed messages to LiteLLM-native chat dicts."""

from __future__ import annotations

import json
import uuid
from typing import Any, List, Optional, Sequence, Tuple

from ag_ui.core.types import (
    AssistantMessage,
    FunctionCall,
    Message,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)

MANDATORY_UI_CONTRACT_BEGIN = (
    "<!-- mandatory-ui-contract:start -->\n"
    "---\n"
    "MANDATORY UI CONTRACT (follow exactly):\n"
)
MANDATORY_UI_CONTRACT_END = "\n---\n<!-- mandatory-ui-contract:end -->"

FRONTEND_INTERACTION_GUIDANCE = (
    "Client-provided frontend interaction tools are available for this run. "
    "Inspect each tool's description and parameter schema in the tool list. "
    "Treat workflow wording such as 'reply yes', 'choose a number', or 'confirm' "
    "as answer semantics, not as a requirement to collect the answer in plain text. "
    "When a provided frontend tool matches the intended user-facing interaction "
    "or presentation, invoke it structurally. This includes asking questions, "
    "offering choices or confirmations, reporting progress, and presenting "
    "generated output when a suitable frontend tool exists. Do not substitute a "
    "matching tool invocation by printing its tool name or arguments as chat text. "
    "Plain assistant text remains the normal channel for explanations, reasoning, "
    "narrative, summaries, and other content that no frontend tool is designed to "
    "handle. This guidance does not change how MCP or local tools are used. Do not "
    "assume a specific frontend tool name."
)

_FRONTEND_GUIDANCE_MARKER = "frontend interaction tools are available"
FRONTEND_TOOL_LIST_PREFIX = "Available frontend UI tools for this run: "


def _text_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for fragment in content:
            if isinstance(fragment, dict):
                if fragment.get("type") == "text":
                    parts.append(str(fragment.get("text") or ""))
            elif getattr(fragment, "type", None) == "text":
                parts.append(str(getattr(fragment, "text", "") or ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def message_to_litellm(message: Message) -> dict[str, Any]:
    """Map one AG-UI Message to a LiteLLM/OpenAI chat message dict."""
    if isinstance(message, SystemMessage) or getattr(message, "role", None) == "system":
        return {
            "role": "system",
            "content": _text_content(getattr(message, "content", "")),
        }
    if isinstance(message, UserMessage) or getattr(message, "role", None) == "user":
        return {
            "role": "user",
            "content": _text_content(getattr(message, "content", "")),
        }
    if isinstance(message, ToolMessage) or getattr(message, "role", None) == "tool":
        out: dict[str, Any] = {
            "role": "tool",
            "tool_call_id": getattr(message, "tool_call_id", "") or "",
            "content": _text_content(getattr(message, "content", "")),
        }
        name = getattr(message, "name", None)
        if name:
            out["name"] = name
        return out
    if isinstance(message, AssistantMessage) or getattr(message, "role", None) == "assistant":
        out = {
            "role": "assistant",
            "content": _text_content(getattr(message, "content", None)) or None,
        }
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            litellm_calls: list[dict] = []
            for tc in tool_calls:
                fn = getattr(tc, "function", None) or (tc.get("function") if isinstance(tc, dict) else None)
                if fn is None:
                    continue
                if hasattr(fn, "model_dump"):
                    fn_dict = fn.model_dump(by_alias=True)
                elif isinstance(fn, dict):
                    fn_dict = fn
                else:
                    fn_dict = {
                        "name": getattr(fn, "name", ""),
                        "arguments": getattr(fn, "arguments", "{}"),
                    }
                args = fn_dict.get("arguments", "{}")
                if not isinstance(args, str):
                    args = json.dumps(args)
                litellm_calls.append(
                    {
                        "id": getattr(tc, "id", None) or (tc.get("id") if isinstance(tc, dict) else str(uuid.uuid4())),
                        "type": "function",
                        "function": {
                            "name": fn_dict.get("name", ""),
                            "arguments": args,
                        },
                    }
                )
            if litellm_calls:
                out["tool_calls"] = litellm_calls
        return out

    # Fallback for developer/activity/reasoning — treat as system context.
    role = getattr(message, "role", "user")
    return {
        "role": role if role in ("system", "user", "assistant", "tool") else "user",
        "content": _text_content(getattr(message, "content", "")),
    }


def messages_to_litellm(messages: Sequence[Message]) -> List[dict[str, Any]]:
    """Convert AG-UI messages preserving roles, tool calls, and tool result IDs."""
    return [message_to_litellm(m) for m in messages]


def _tool_result_has_content(content: Any) -> bool:
    if content is None:
        return False
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return bool(content)
    return True


def dedupe_litellm_tool_results(
    messages: Sequence[dict[str, Any]],
) -> Tuple[List[dict[str, Any]], List[str]]:
    """Keep at most one ``role: tool`` message per non-empty ``tool_call_id``.

    Preserves provider-valid ordering by retaining the earliest occurrence for
    each ID (closest to the matching tool_use). When the earliest copy has no
    content and a later duplicate does, the earliest message's content is
    updated in the returned copy without moving its position.

    Messages with missing or empty ``tool_call_id`` values are never deduped.
    The input list and its message dicts are not mutated.
    """
    if not messages:
        return [], []

    first_by_id: dict[str, int] = {}
    duplicate_indices: set[int] = set()
    duplicate_ids: list[str] = []

    for idx, msg in enumerate(messages):
        if msg.get("role") != "tool":
            continue
        tc_id = msg.get("tool_call_id")
        if tc_id is None or not str(tc_id).strip():
            continue
        tc_key = str(tc_id)
        if tc_key in first_by_id:
            duplicate_indices.add(idx)
            if tc_key not in duplicate_ids:
                duplicate_ids.append(tc_key)
        else:
            first_by_id[tc_key] = idx

    if not duplicate_indices:
        return messages, []

    out: list[dict[str, Any]] = list(messages)
    for tc_key, first_idx in first_by_id.items():
        first_msg = messages[first_idx]
        if _tool_result_has_content(first_msg.get("content")):
            continue
        for later_idx in sorted(i for i in duplicate_indices if str(messages[i].get("tool_call_id") or "") == tc_key):
            later_content = messages[later_idx].get("content")
            if _tool_result_has_content(later_content):
                merged = dict(first_msg)
                merged["content"] = later_content
                out[first_idx] = merged
                break

    normalized = [msg for idx, msg in enumerate(out) if idx not in duplicate_indices]
    return normalized, duplicate_ids


def _format_contexts(contexts: Optional[Sequence[Any]]) -> Optional[str]:
    if not contexts:
        return None
    context_lines: list[str] = []
    for ctx in contexts:
        desc = getattr(ctx, "description", None) or (ctx.get("description") if isinstance(ctx, dict) else None)
        val = getattr(ctx, "value", None) or (ctx.get("value") if isinstance(ctx, dict) else None)
        if desc and val:
            context_lines.append(f"{desc}: {val}")
    if not context_lines:
        return None
    return "Context:\n" + "\n".join(context_lines)


def _incoming_system_texts(incoming_messages: Optional[Sequence[Message]]) -> list[str]:
    texts: list[str] = []
    for msg in incoming_messages or []:
        litellm = message_to_litellm(msg)
        if litellm.get("role") != "system":
            continue
        content = (litellm.get("content") or "").strip()
        if content:
            texts.append(content)
    return texts


def _client_contract_covers_frontend_guidance(incoming_messages: Optional[Sequence[Message]]) -> bool:
    blob = "\n".join(_incoming_system_texts(incoming_messages)).lower()
    return _FRONTEND_GUIDANCE_MARKER in blob


def _frontend_tool_catalog(tool_names: Optional[Sequence[str]]) -> Optional[str]:
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in tool_names or []:
        name = str(raw_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    if not names:
        return None
    return f"{FRONTEND_TOOL_LIST_PREFIX}{json.dumps(names, ensure_ascii=False)}"


def build_authoritative_system_content(
    *,
    harness_prompt: Optional[str] = None,
    incoming_messages: Optional[Sequence[Message]] = None,
    contexts: Optional[Sequence[Any]] = None,
    has_frontend_tools: bool = False,
    frontend_tool_names: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Compose one system prompt: harness, context, optional guidance, UI contract."""
    sections: list[str] = []

    if harness_prompt and harness_prompt.strip():
        sections.append(harness_prompt.strip())

    context_block = _format_contexts(contexts)
    if context_block:
        sections.append(context_block)

    frontend_catalog = _frontend_tool_catalog(frontend_tool_names)
    if has_frontend_tools or frontend_catalog:
        frontend_section: list[str] = []
        if not _client_contract_covers_frontend_guidance(incoming_messages):
            frontend_section.append(FRONTEND_INTERACTION_GUIDANCE)
        if frontend_catalog:
            frontend_section.append(frontend_catalog)
        if frontend_section:
            sections.append("\n".join(frontend_section))

    incoming_system = _incoming_system_texts(incoming_messages)
    if incoming_system:
        contract_body = "\n\n".join(incoming_system)
        sections.append(
            f"{MANDATORY_UI_CONTRACT_BEGIN}{contract_body}{MANDATORY_UI_CONTRACT_END}"
        )

    if not sections:
        return None
    return "\n\n".join(sections)


def compose_system_messages(
    *,
    harness_prompt: Optional[str] = None,
    incoming_messages: Optional[Sequence[Message]] = None,
    contexts: Optional[Sequence[Any]] = None,
    has_frontend_tools: bool = False,
    frontend_tool_names: Optional[Sequence[str]] = None,
) -> List[dict[str, Any]]:
    """Return zero or one authoritative LiteLLM system message."""
    content = build_authoritative_system_content(
        harness_prompt=harness_prompt,
        incoming_messages=incoming_messages,
        contexts=contexts,
        has_frontend_tools=has_frontend_tools,
        frontend_tool_names=frontend_tool_names,
    )
    if not content:
        return []
    return [{"role": "system", "content": content}]


def build_initial_messages(
    *,
    harness_prompt: Optional[str] = None,
    agui_messages: Optional[Sequence[Message]] = None,
    legacy_request: Optional[str] = None,
    contexts: Optional[Sequence[Any]] = None,
    has_frontend_tools: bool = False,
    frontend_tool_names: Optional[Sequence[str]] = None,
) -> List[dict[str, Any]]:
    """Assemble the full initial message list for a fresh run."""
    messages: list[dict[str, Any]] = compose_system_messages(
        harness_prompt=harness_prompt,
        incoming_messages=agui_messages,
        contexts=contexts,
        has_frontend_tools=has_frontend_tools,
        frontend_tool_names=frontend_tool_names,
    )

    non_system_added = False
    for msg in agui_messages or []:
        litellm = message_to_litellm(msg)
        if litellm.get("role") != "system":
            messages.append(litellm)
            non_system_added = True

    if not non_system_added and legacy_request:
        messages.append({"role": "user", "content": legacy_request})

    return messages


def litellm_messages_to_agui_snapshot(messages: Sequence[dict]) -> List[Message]:
    """Best-effort conversion of LiteLLM message dicts for MESSAGES_SNAPSHOT events."""
    out: list[Message] = []
    for idx, msg in enumerate(messages):
        role = msg.get("role", "user")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = _text_content(content)
        mid = msg.get("id") or f"msg_{idx}"
        if role == "system":
            out.append(SystemMessage(id=mid, content=str(content)))
        elif role == "user":
            out.append(UserMessage(id=mid, content=str(content)))
        elif role == "assistant":
            tool_calls = None
            raw_tcs = msg.get("tool_calls") or []
            if raw_tcs:
                tool_calls = []
                for tc in raw_tcs:
                    fn = tc.get("function") or {}
                    tool_calls.append(
                        ToolCall(
                            id=tc.get("id") or f"call_{idx}",
                            function=FunctionCall(
                                name=fn.get("name", ""),
                                arguments=fn.get("arguments") or "{}",
                            ),
                        )
                    )
            out.append(AssistantMessage(id=mid, content=str(content or ""), tool_calls=tool_calls))
        elif role == "tool":
            out.append(
                ToolMessage(
                    id=mid,
                    content=str(content),
                    tool_call_id=msg.get("tool_call_id") or "",
                )
            )
    return out
