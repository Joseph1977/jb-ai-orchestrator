# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Context compaction helpers for long tool-calling loops.

Mechanisms (applied in order when the budget is exceeded):

1. **Summarize** — LLM compresses older user/assistant turns into one block.
2. **Compact** — shrink/offload older tool messages (recent ones kept intact).
3. **Offload** — persist a single huge tool result under ``.agent/offload/``.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.config import Config
from app.services.hooks.executor import run_hooks
from app.services.local_tool_provider import LocalToolContext
from app.utils.logger import logger


_OFFLOAD_DIR = ".agent/offload"
_SUMMARY_MARKER = "[context_summary]"


def _offload_pointer_note(rel_path: str) -> str:
    try:
        from app.services.prompt_loader import load_prompt

        return load_prompt(
            "offload_pointer_note",
            path=rel_path,
            read_file_tool="read_file_local",
        )["prompt"].strip()
    except Exception:
        return (
            f"Full tool result saved to '{rel_path}'. "
            "The preview may be sufficient; use read_file_local on that path "
            "only if you need omitted detail."
        )


def is_agent_offload_path(rel_path: str) -> bool:
    """True when *rel_path* points at a persisted offload artifact."""
    if not rel_path:
        return False
    from app.services.runtime_paths import normalize_rel

    normalized = normalize_rel(str(rel_path))
    return normalized == _OFFLOAD_DIR or normalized.startswith(f"{_OFFLOAD_DIR}/")


def normalize_offload_reference(rel_path: str) -> Optional[str]:
    """Return a canonical logical offload path, or ``None`` when not offload."""
    if not is_agent_offload_path(rel_path):
        return None
    from app.services.runtime_paths import normalize_rel, offload_filename_from_logical

    filename = offload_filename_from_logical(rel_path)
    if not filename:
        return None
    return f"{_OFFLOAD_DIR}/{filename}"


def _collect_offload_references_from_value(value: Any, found: set[str]) -> None:
    if isinstance(value, str):
        normalized = normalize_offload_reference(value)
        if normalized:
            found.add(normalized)
            return
        if value.lstrip().startswith("{"):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                return
            _collect_offload_references_from_value(parsed, found)
        return

    if isinstance(value, dict):
        path = value.get("path")
        if isinstance(path, str):
            normalized = normalize_offload_reference(path)
            if normalized:
                found.add(normalized)
        for item in value.values():
            _collect_offload_references_from_value(item, found)
        return

    if isinstance(value, list):
        for item in value:
            _collect_offload_references_from_value(item, found)


def collect_offload_references_from_state_payload(payload: dict) -> set[str]:
    """Scan a persisted continuation payload for logical offload references."""
    found: set[str] = set()
    if not isinstance(payload, dict):
        return found
    _collect_offload_references_from_value(payload, found)
    return found


def collect_offload_references_from_state_payloads(
    payloads: Iterable[dict],
) -> set[str]:
    """Union offload references across multiple live continuation payloads."""
    found: set[str] = set()
    for payload in payloads:
        if isinstance(payload, dict):
            found.update(collect_offload_references_from_state_payload(payload))
    return found


def should_skip_tool_result_offload(
    *,
    tool_name: str | None = None,
    tool_args: dict | None = None,
    tool_result: Any = None,
) -> bool:
    """Skip re-offloading dereferenced offload files or marked results."""
    if isinstance(tool_result, dict) and tool_result.get("alreadyOffloaded"):
        return True
    path = None
    if tool_args:
        path = tool_args.get("path")
    if not path and isinstance(tool_result, dict):
        path = tool_result.get("path")
    if path and is_agent_offload_path(str(path)):
        return True
    if tool_name and tool_name.endswith("_read_file"):
        if path and is_agent_offload_path(str(path)):
            return True
    return False


def message_chars(messages: List[dict]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
        elif content is not None:
            total += len(json.dumps(content))
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            total += len(json.dumps(tool_calls))
    return total


def estimate_message_tokens(messages: List[dict], *, reported_prompt_tokens: int = 0) -> int:
    """Best-effort token estimate for compaction triggers."""
    if reported_prompt_tokens and reported_prompt_tokens > 0:
        return reported_prompt_tokens
    # Rough heuristic when provider usage is unavailable (~4 chars/token).
    return max(1, message_chars(messages) // 4)


def _over_budget(messages: List[dict], *, reported_prompt_tokens: int = 0) -> bool:
    if not Config.CONTEXT_COMPACTION_ENABLED:
        return False
    token_budget = Config.CONTEXT_COMPACTION_TOKENS
    if token_budget > 0:
        return estimate_message_tokens(
            messages, reported_prompt_tokens=reported_prompt_tokens
        ) >= token_budget
    char_budget = Config.CONTEXT_COMPACTION_CHARS
    if char_budget <= 0:
        return False
    return message_chars(messages) > char_budget


def offload_tool_result_if_needed(
    content: str,
    *,
    tool_call_id: str,
    tool_name: str,
    local_context: Optional[LocalToolContext],
    skip_offload: bool = False,
) -> str:
    """Replace oversized tool content with a workspace file pointer."""
    if skip_offload:
        return content
    if not Config.CONTEXT_COMPACTION_ENABLED:
        return content
    threshold = Config.TOOL_RESULT_OFFLOAD_CHARS
    if threshold <= 0 or len(content) <= threshold:
        return content
    if local_context is None or not local_context.workspace_path:
        preview = content[:threshold]
        return json.dumps(
            {
                "truncated": True,
                "originalChars": len(content),
                "preview": preview,
                "note": "Tool result truncated (no workspace for offload).",
            }
        )

    from pathlib import Path

    from app.services.runtime_paths import resolve_tool_path

    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in (tool_call_id or "result"))
    rel_path = f"{_OFFLOAD_DIR}/{safe_id}.txt"
    target = Path(
        resolve_tool_path(
            local_context.workspace_path,
            rel_path,
            runtime_path=getattr(local_context, "runtime_path", None),
        )
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to offload tool result %s: %s", tool_call_id, exc)
        preview = content[:threshold]
        return json.dumps(
            {
                "truncated": True,
                "originalChars": len(content),
                "preview": preview,
                "note": f"Offload failed: {exc}",
            }
        )

    preview = content[: min(500, threshold)]
    logger.info(
        "Offloaded tool result for %s (%d chars) -> %s",
        tool_name,
        len(content),
        rel_path,
    )
    return json.dumps(
        {
            "offloaded": True,
            "tool": tool_name,
            "originalChars": len(content),
            "path": rel_path,
            "preview": preview,
            "note": _offload_pointer_note(rel_path),
        }
    )


def compact_tool_messages_if_needed(
    messages: List[dict],
    *,
    local_context: Optional[LocalToolContext] = None,
) -> List[dict]:
    """Shrink older tool messages when history exceeds the budget."""
    if not _over_budget(messages):
        return messages

    keep_recent = max(0, Config.CONTEXT_COMPACTION_KEEP_RECENT_TOOL_MSGS)
    tool_indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if not tool_indexes:
        return messages

    compactable = tool_indexes[:-keep_recent] if keep_recent else tool_indexes
    if not compactable:
        return messages

    compacted = 0
    for idx in compactable:
        if not _over_budget(messages):
            break
        msg = messages[idx]
        content = msg.get("content")
        if not isinstance(content, str) or len(content) < 400:
            continue
        if '"offloaded": true' in content[:80] or '"compacted": true' in content[:80]:
            continue
        tool_name = msg.get("name") or "tool"
        tool_call_id = msg.get("tool_call_id") or f"compacted_{idx}"
        if local_context is not None:
            msg["content"] = offload_tool_result_if_needed(
                content,
                tool_call_id=f"compact_{tool_call_id}",
                tool_name=tool_name,
                local_context=local_context,
            )
        else:
            msg["content"] = json.dumps(
                {
                    "compacted": True,
                    "tool": tool_name,
                    "originalChars": len(content),
                    "preview": content[:400],
                    "note": "Older tool result compacted to free context.",
                }
            )
        compacted += 1

    if compacted:
        logger.info(
            "Compacted %d older tool messages; history now ~%d chars",
            compacted,
            message_chars(messages),
        )
    return messages


async def _summarize_old_turns(
    messages: List[dict],
    *,
    call_litellm: Callable[..., Awaitable[dict]],
    model: str,
    lite_llm_timeout: Optional[int],
) -> List[dict]:
    """Replace older user/assistant turns with one LLM-generated summary."""
    if not Config.CONTEXT_SUMMARIZATION_ENABLED:
        return messages

    keep_recent = max(0, Config.CONTEXT_COMPACTION_KEEP_RECENT_TURNS)
    # Partition: leading system msgs, middle to summarize, trailing recent turns.
    system_msgs: List[dict] = []
    rest: List[dict] = []
    for msg in messages:
        if not rest and msg.get("role") == "system":
            system_msgs.append(msg)
        else:
            rest.append(msg)

    if len(rest) <= keep_recent + 1:
        return messages

    to_summarize = rest[:-keep_recent] if keep_recent else rest
    recent = rest[-keep_recent:] if keep_recent else []

    # Skip if already summarized or nothing meaningful to compress.
    dialogue = [
        m for m in to_summarize
        if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
    ]
    if not dialogue:
        return messages

    transcript_lines: List[str] = []
    for msg in dialogue:
        role = msg.get("role", "user")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = json.dumps(content)
        transcript_lines.append(f"{role.upper()}: {content}")
    transcript = "\n\n".join(transcript_lines)
    if len(transcript) < 500:
        return messages

    prompt = (
        "Summarize the following conversation excerpt for continuation. "
        "Preserve goals, decisions, file paths, errors, and open tasks. "
        "Be concise (under 800 words). Do not invent facts.\n\n"
        f"{transcript}"
    )
    try:
        llm_response = await call_litellm(
            messages=[
                {
                    "role": "system",
                    "content": "You compress conversation history for a coding agent.",
                },
                {"role": "user", "content": prompt},
            ],
            model=Config.CONTEXT_SUMMARIZATION_MODEL or model,
            tools=None,
            lite_llm_request_timeout_in_sec=lite_llm_timeout,
            stream=False,
        )
        summary = (
            llm_response.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            or ""
        ).strip()
    except Exception as exc:
        logger.warning("Context summarization failed: %s", exc)
        return messages

    if not summary:
        return messages

    summary_msg = {
        "role": "system",
        "content": f"{_SUMMARY_MARKER}\n\n{summary}",
    }
    tool_msgs = [m for m in to_summarize if m.get("role") == "tool"]
    rebuilt = system_msgs + [summary_msg] + tool_msgs + recent
    logger.info(
        "Summarized %d older turns into one block (~%d chars); kept %d recent turns",
        len(to_summarize),
        len(summary),
        len(recent),
    )
    return rebuilt


async def manage_context_before_llm(
    messages: List[dict],
    *,
    local_context: Optional[LocalToolContext],
    model: str,
    lite_llm_timeout: Optional[int],
    call_litellm: Callable[..., Awaitable[dict]],
    reported_prompt_tokens: int = 0,
) -> List[dict]:
    """Run preCompact hook, optional LLM summarization, then tool compaction."""
    if not Config.CONTEXT_COMPACTION_ENABLED:
        return messages
    if not _over_budget(messages, reported_prompt_tokens=reported_prompt_tokens):
        return messages

    if local_context is not None and local_context.workspace_path:
        decision = await run_hooks(
            local_context.workspace_path,
            "preCompact",
            {
                "message_count": len(messages),
                "estimated_chars": message_chars(messages),
                "estimated_tokens": estimate_message_tokens(
                    messages, reported_prompt_tokens=reported_prompt_tokens
                ),
                "prompt_tokens": reported_prompt_tokens,
                "model": model,
            },
        )
        if not decision.allowed:
            logger.info(
                "preCompact hook denied compaction: %s",
                decision.agent_message or decision.user_message,
            )
            return messages

    if _over_budget(messages, reported_prompt_tokens=reported_prompt_tokens):
        messages = await _summarize_old_turns(
            messages,
            call_litellm=call_litellm,
            model=model,
            lite_llm_timeout=lite_llm_timeout,
        )

    return compact_tool_messages_if_needed(messages, local_context=local_context)


# Backward-compatible alias used by existing tests/callers.
def compact_messages_if_needed(
    messages: List[dict],
    *,
    local_context: Optional[LocalToolContext] = None,
) -> List[dict]:
    return compact_tool_messages_if_needed(messages, local_context=local_context)


def serialize_tool_result(tool_result: Dict[str, Any]) -> str:
    """JSON-encode a tool result for the messages array."""
    try:
        return json.dumps(tool_result)
    except (TypeError, ValueError):
        return json.dumps({"result": str(tool_result)})
