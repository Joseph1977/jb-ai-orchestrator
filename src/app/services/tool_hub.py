# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ag_ui.core.events import (
    TextMessageStartEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
)

from pathlib import Path

from app.config import Config
from app.services.agui_event_service import AGUIEventService
from app.services.agui_service import AGUIService, AGUIToolRecord
from app.services.context_compaction import (
    manage_context_before_llm,
    offload_tool_result_if_needed,
    serialize_tool_result,
    should_skip_tool_result_offload,
)
from app.services.hooks import hook_ask_sentinel, hook_permission_deny_reason, is_hook_ask_approved, run_hooks
from app.services.hooks.loader import tool_matcher_name
from app.services.local_tool_provider import LocalToolContext, local_tool_provider
from app.services.agui_interrupt import (
    HOOK_PERMISSION_RESPONSE_SCHEMA,
    HOOK_PERMISSION_SOURCE,
    HOOK_PERMISSION_SOURCES,
    build_interrupts_from_pending,
    is_hook_permission_pending,
    tool_response_content,
)
from app.services.mcp_agent_service import LLMUpstreamError, MCPAgentService, MCPTool
from app.services.multimodal import build_multimodal_user_message
from app.services.tool_registry import (
    ToolProvider,
    ToolRegistry,
    build_active_agui_map,
    build_tool_registry,
    mcp_tools_from_stored_litellm,
)
from app.services.tool_call_counters import (
    init_fresh_counters,
    restore_counters_on_resume,
    result_counter_fields,
    snapshot_counter_fields,
)
from app.services import workspace_manager
from app.utils.logger import logger

HOOK_ASK_SOURCE = HOOK_PERMISSION_SOURCE  # backward-compatible alias


def _frontend_tools_await_response(frontend_tools: Optional[List[dict]]) -> bool:
    for tool in frontend_tools or []:
        if not isinstance(tool, dict):
            continue
        extensions = tool.get("extensions") or {}
        if extensions.get("awaitsResponse"):
            return True
    return False


def _agui_records_await_response(records: Sequence[AGUIToolRecord]) -> bool:
    return any(rec.awaits_response for rec in records)


def _suppress_headless_ask_user(
    *,
    frontend_tools: Optional[List[dict]],
    agui_records: Sequence[AGUIToolRecord],
) -> bool:
    """Hide headless ask_user when client registered interactive frontend tools."""
    return _frontend_tools_await_response(frontend_tools) or _agui_records_await_response(
        agui_records
    )


def _file_path_arg(function_name: str, function_args: dict) -> Optional[str]:
    if not function_name:
        return None
    if function_name.startswith(("read_file_", "write_file_", "create_file_", "edit_file_")):
        path = function_args.get("path")
        return str(path) if path else None
    return None


def _serialize_and_offload_tool_result(
    tool_result,
    *,
    tool_call_id: str,
    function_name: str,
    function_args: dict,
    local_context: Optional[LocalToolContext],
    segment_tool_call_count: int,
) -> str:
    tool_content = serialize_tool_result(tool_result)
    return offload_tool_result_if_needed(
        tool_content,
        tool_call_id=tool_call_id or f"call_{segment_tool_call_count}",
        tool_name=function_name or "tool",
        local_context=local_context,
        skip_offload=should_skip_tool_result_offload(
            tool_name=function_name,
            tool_args=function_args,
            tool_result=tool_result,
        ),
    )


def _apply_hook_decision(decision, *, event: str) -> Optional[dict]:
    """Return a tool-result dict when the hook blocks or asks; else None to proceed."""
    if decision.needs_confirmation:
        return hook_ask_sentinel(event=event, decision=decision)
    if not decision.allowed:
        return {
            "error": decision.agent_message or decision.user_message or "Blocked by hook",
            "hookBlocked": True,
        }
    return None


@dataclass
class AGUIRunContext:
    """Metadata for correlating AG-UI events to a specific thread/run."""

    thread_id: str
    run_id: str
    parent_run_id: Optional[str] = None


def _clone_json(obj: Any) -> Any:
    """Return a JSON-safe deep copy of the provided object."""
    return json.loads(json.dumps(obj))


def _message_has_tool_result(messages: list, tool_call_id: str) -> bool:
    return any(
        m.get("role") == "tool" and m.get("tool_call_id") == tool_call_id for m in messages
    )


def _cancel_requested(
    cancel_event: Optional[asyncio.Event] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> bool:
    if cancel_event is not None and cancel_event.is_set():
        return True
    if cancel_check is not None and cancel_check():
        return True
    return False


def _raise_if_cancelled(
    cancel_event: Optional[asyncio.Event] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> None:
    if _cancel_requested(cancel_event, cancel_check):
        raise asyncio.CancelledError()


class ToolExecutionHub:
    """Orchestrates LiteLLM tool calls across local, MCP, and AG-UI providers."""

    def __init__(
        self,
        mcp_service: MCPAgentService,
        agui_service: AGUIService,
        agui_event_service: AGUIEventService,
    ) -> None:
        self.mcp_service = mcp_service
        self.agui_service = agui_service
        self.agui_event_service = agui_event_service


    async def process_request(
        self,
        request: str,
        model: str = "gpt-3.5-turbo",
        max_tool_calls: Optional[int] = None,
        requested_tools: Optional[List[str]] = None,
        lite_llm_request_timeout_in_sec: Optional[int] = None,
        include_agui_tools: bool = False,
        agui_context: Optional[AGUIRunContext] = None,
        resume_state: Optional[dict] = None,
        resume_tool_result: Optional[dict] = None,
        resume_tool_results: Optional[List[dict]] = None,
        initial_messages: Optional[List[dict]] = None,
        local_context: Optional[LocalToolContext] = None,
        system_prompt: Optional[str] = None,
        frontend_tools: Optional[List[dict]] = None,
        harness_manifest: Optional[dict] = None,
        cancel_event: Optional[asyncio.Event] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Expose local built-in, MCP, and AG-UI tools to LiteLLM.

        ``local_context`` (a workspace) enables the in-process built-in tools;
        AG-UI tools are only exposed when explicitly requested for this run.
        Any tool that awaits a user response persists state and returns so any
        instance can resume (no in-memory futures).
        """
        resume_state = resume_state or {}
        resumed_run = bool(resume_state)
        original_prompt = resume_state.get("request", request)

        snapshot_max_calls = resume_state.get("max_calls")
        max_calls = (
            max_tool_calls
            if max_tool_calls is not None
            else snapshot_max_calls
            if snapshot_max_calls is not None
            else Config.MAX_TOOL_CALLS
        )
        lite_llm_timeout = (
            resume_state.get("lite_llm_request_timeout_in_sec")
            or lite_llm_request_timeout_in_sec
            or self.mcp_service.litellm_request_timeout_in_sec
        )
        requested_tools = resume_state.get("requested_tools", requested_tools)

        try:
            all_mcp_tools = await self.mcp_service.fetch_mcp_tools()
            if include_agui_tools:
                # Prefer per-run frontend tools (isolated) over the legacy global cache.
                if frontend_tools is not None:
                    agui_service_records = self.agui_service.build_records(frontend_tools)
                else:
                    agui_service_records = self.agui_service.list_tools()
            else:
                agui_service_records = []
            mcp_by_name = {tool.name: tool for tool in all_mcp_tools}
            agui_by_name = {record.prefixed_name or record.name: record for record in agui_service_records}
            pending_multimodal: List[dict] = []

            if resumed_run:
                messages = _clone_json(resume_state.get("messages", []))
                if not messages:
                    messages = [{"role": "user", "content": original_prompt}]

                litellm_tools = resume_state.get("litellm_tools", [])
                stored_records = resume_state.get("agui_records", [])
                selected_agui_tools = [AGUIToolRecord(**record) for record in stored_records]

                segment_tool_call_count, total_tool_call_count = restore_counters_on_resume(
                    resume_state
                )
                total_prompt_tokens = resume_state.get("total_prompt_tokens", 0)
                total_completion_tokens = resume_state.get("total_completion_tokens", 0)
                total_tokens = resume_state.get("total_tokens", 0)
                llm_interaction_count = resume_state.get("llm_interaction_count", 0)
                executed_tool_calls_info = list(resume_state.get("tool_calls_info", []))
                forwarded_agui_tool_calls = list(resume_state.get("agui_tool_calls", []))

                # Normalize resume responses (single legacy or multi).
                normalized_responses: List[dict] = list(resume_tool_results or [])
                if resume_tool_result is not None:
                    entry = dict(resume_tool_result)
                    if not entry.get("tool_call_id") and not entry.get("toolCallId"):
                        first_pending = (resume_state.get("pending_tools") or [resume_state.get("pending_tool")])[0]
                        if first_pending:
                            entry["tool_call_id"] = first_pending.get("tool_call_id")
                    normalized_responses.append(entry)

                pending_tools = list(resume_state.get("pending_tools") or [])
                if not pending_tools and resume_state.get("pending_tool"):
                    pending_tools = [resume_state["pending_tool"]]

                if pending_tools and normalized_responses:
                    pending_by_id: dict[str, dict] = {}
                    for pt in pending_tools:
                        tc_key = pt.get("tool_call_id")
                        if tc_key:
                            pending_by_id[str(tc_key)] = pt
                        intr_key = pt.get("interrupt_id")
                        if intr_key:
                            pending_by_id[str(intr_key)] = pt
                    for resp in normalized_responses:
                        tc_id = (
                            resp.get("tool_call_id")
                            or resp.get("toolCallId")
                            or resp.get("interrupt_id")
                            or resp.get("interruptId")
                        )
                        if not tc_id:
                            continue
                        tc_id = str(tc_id)
                        pending = pending_by_id.get(tc_id)
                        if not pending:
                            continue
                        pending_key = str(pending.get("tool_call_id") or tc_id)
                        if pending.get("source") in HOOK_PERMISSION_SOURCES:
                            original_id = pending.get("original_tool_call_id")
                            if original_id and _message_has_tool_result(messages, original_id):
                                pending_by_id.pop(pending_key, None)
                                if pending.get("interrupt_id"):
                                    pending_by_id.pop(str(pending.get("interrupt_id")), None)
                                continue
                            await self._append_hook_ask_resume(
                                messages=messages,
                                pending_tool=pending,
                                resume_tool_result=resp,
                                local_context=local_context,
                                model=model,
                                lite_llm_timeout=lite_llm_timeout,
                                agui_context=agui_context,
                                pending_multimodal=pending_multimodal,
                            )
                        else:
                            if _message_has_tool_result(messages, tc_id):
                                pending_by_id.pop(pending_key, None)
                                continue
                            content = tool_response_content(resp)
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc_id,
                                    "name": pending.get("function_name"),
                                    "content": json.dumps(content),
                                }
                            )
                        pending_by_id.pop(pending_key, None)
                        if pending.get("interrupt_id"):
                            pending_by_id.pop(str(pending.get("interrupt_id")), None)
                    seen_pending: set[str] = set()
                    pending_tools = []
                    for pt in pending_by_id.values():
                        tc = pt.get("tool_call_id")
                        if tc and str(tc) not in seen_pending:
                            seen_pending.add(str(tc))
                            pending_tools.append(pt)
                    if pending_tools:
                        return self._build_await_result(
                            pending_tools=pending_tools,
                            messages=messages,
                            original_prompt=original_prompt,
                            model=model,
                            max_calls=max_calls,
                            requested_tools=requested_tools,
                            lite_llm_timeout=lite_llm_timeout,
                            segment_tool_call_count=segment_tool_call_count,
                            total_tool_call_count=total_tool_call_count,
                            llm_interaction_count=llm_interaction_count,
                            total_prompt_tokens=total_prompt_tokens,
                            total_completion_tokens=total_completion_tokens,
                            total_tokens=total_tokens,
                            litellm_tools=litellm_tools,
                            selected_agui_tools=selected_agui_tools,
                            executed_tool_calls_info=executed_tool_calls_info,
                            forwarded_agui_tool_calls=forwarded_agui_tool_calls,
                            harness_manifest=resume_state.get("harness_manifest"),
                        )
                elif pending_tools and not normalized_responses:
                    raise ValueError("resume_tool_result(s) required when pending tools exist")
            else:
                messages = list(initial_messages) if initial_messages else []
                if not messages:
                    if system_prompt:
                        messages.append({"role": "system", "content": system_prompt})
                    messages.append({"role": "user", "content": request})
                try:
                    mcp_tools, selected_agui_tools = self._select_tools(
                        requested_tools,
                        all_mcp_tools,
                        agui_service_records,
                        mcp_by_name,
                        agui_by_name,
                    )
                except ValueError as err:
                    logger.error(str(err))
                    return {
                        "success": False,
                        "error": str(err),
                        "tool_calls_made": 0,
                        "total_tokens": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "tool_calls_info": [],
                        "agui_tool_calls": [],
                    }

                include_local = local_context is not None and local_tool_provider.enabled
                if include_local and Config.FILTER_MCP_TOOLS_CONFLICTING_WITH_LOCAL:
                    local_bases = local_tool_provider.base_names()
                    kept: List[MCPTool] = []
                    for tool in mcp_tools:
                        if tool.original_name in local_bases:
                            logger.info(
                                "Filtering MCP tool '%s' from '%s' (conflicts with local built-in)",
                                tool.original_name,
                                tool.server_name,
                            )
                        else:
                            kept.append(tool)
                    mcp_tools = kept

                segment_tool_call_count, total_tool_call_count = init_fresh_counters()
                total_prompt_tokens = 0
                total_completion_tokens = 0
                total_tokens = 0
                llm_interaction_count = 0
                executed_tool_calls_info: List[dict] = []
                forwarded_agui_tool_calls: List[dict] = []
                litellm_tools: List[dict] = []

            harness_manifest = resume_state.get("harness_manifest") if resumed_run else harness_manifest

            # Build provider-aware registry for routing (canonical + alias resolution).
            local_litellm = []
            if local_context is not None and local_tool_provider.enabled:
                depth = getattr(local_context, "subagent_depth", 0) or 0
                exclude_base = {"task", "ask_user"} if depth > 0 else set()
                if _suppress_headless_ask_user(
                    frontend_tools=frontend_tools,
                    agui_records=selected_agui_tools,
                ):
                    exclude_base.add("ask_user")
                local_litellm = local_tool_provider.list_litellm_tools(
                    exclude_base=exclude_base,
                    mode=local_context.mode,
                    output_bound=local_context.output_backend is not None,
                )
            local_awaits = {
                lt["function"]["name"]: local_tool_provider.awaits_response(lt["function"]["name"])
                for lt in local_litellm
                if lt.get("function", {}).get("name")
            }
            mcp_for_registry = mcp_tools if not resumed_run else mcp_tools_from_stored_litellm(
                litellm_tools,
                agui_records=selected_agui_tools,
                local_namespace=local_tool_provider.namespace,
                is_local_tool=local_tool_provider.is_local_tool,
            )
            tool_registry = build_tool_registry(
                mcp_tools=mcp_for_registry,
                agui_records=selected_agui_tools,
                local_litellm_tools=local_litellm,
                local_awaits=local_awaits,
            )
            active_agui_map = build_active_agui_map(tool_registry)

            # Local/output availability is segment-scoped (mode + fresh credentials),
            # so resume cannot rely only on frozen schemas from LLMState.
            litellm_tools = tool_registry.litellm_tools()

            def _snapshot_state(pending_tools_list: list) -> dict:
                """Serialize the full loop state so any instance can resume."""
                first = pending_tools_list[0] if pending_tools_list else {}
                return {
                    "messages": _clone_json(messages),
                    "request": original_prompt,
                    "model": model,
                    "max_calls": max_calls,
                    "requested_tools": requested_tools,
                    "lite_llm_request_timeout_in_sec": lite_llm_timeout,
                    **snapshot_counter_fields(segment_tool_call_count, total_tool_call_count),
                    "llm_interaction_count": llm_interaction_count,
                    "total_prompt_tokens": total_prompt_tokens,
                    "total_completion_tokens": total_completion_tokens,
                    "total_tokens": total_tokens,
                    "litellm_tools": _clone_json(litellm_tools),
                    "agui_records": [asdict(record) for record in selected_agui_tools],
                    "agui_tool_calls": _clone_json(forwarded_agui_tool_calls),
                    "tool_calls_info": _clone_json(executed_tool_calls_info),
                    "pending_tools": pending_tools_list,
                    "pending_tool": first,
                    "harness_manifest": harness_manifest,
                }

            def _awaits_return(pending_tools_list: list) -> dict:
                return self._build_await_result(
                    pending_tools=pending_tools_list,
                    messages=messages,
                    original_prompt=original_prompt,
                    model=model,
                    max_calls=max_calls,
                    requested_tools=requested_tools,
                    lite_llm_timeout=lite_llm_timeout,
                    segment_tool_call_count=segment_tool_call_count,
                    total_tool_call_count=total_tool_call_count,
                    llm_interaction_count=llm_interaction_count,
                    total_prompt_tokens=total_prompt_tokens,
                    total_completion_tokens=total_completion_tokens,
                    total_tokens=total_tokens,
                    litellm_tools=litellm_tools,
                    selected_agui_tools=selected_agui_tools,
                    executed_tool_calls_info=executed_tool_calls_info,
                    forwarded_agui_tool_calls=forwarded_agui_tool_calls,
                    harness_manifest=harness_manifest,
                )

            text_streamed = False

            # Session / prompt hooks (new runs only; resume continues an existing session).
            if not resumed_run and local_context is not None:
                ws = local_context.workspace_path
                start = await run_hooks(
                    ws,
                    "sessionStart",
                    {
                        "session_id": agui_context.thread_id if agui_context else None,
                        "model": model,
                    },
                )
                blocked = _apply_hook_decision(start, event="sessionStart")
                if blocked and blocked.get("_hookAsk"):
                    # sessionStart ask is rare; deny rather than pause before any tool call.
                    return {
                        "success": False,
                        "error": blocked.get("message") or "sessionStart hook requires confirmation",
                        "tool_calls_made": 0,
                        "total_tokens": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "tool_calls_info": [],
                        "agui_tool_calls": [],
                    }
                if blocked:
                    return {
                        "success": False,
                        "error": blocked.get("error") or "Blocked by sessionStart hook",
                        "tool_calls_made": 0,
                        "total_tokens": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "tool_calls_info": [],
                        "agui_tool_calls": [],
                    }
                submit = await run_hooks(
                    ws,
                    "beforeSubmitPrompt",
                    {"prompt": original_prompt, "model": model},
                    matcher_subject=original_prompt or "",
                )
                blocked = _apply_hook_decision(submit, event="beforeSubmitPrompt")
                if blocked:
                    return {
                        "success": False,
                        "error": blocked.get("message")
                        or blocked.get("error")
                        or "Blocked by beforeSubmitPrompt hook",
                        "tool_calls_made": 0,
                        "total_tokens": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "tool_calls_info": [],
                        "agui_tool_calls": [],
                    }

            last_reported_prompt_tokens = 0

            while segment_tool_call_count < max_calls:
                _raise_if_cancelled(cancel_event, cancel_check)
                messages = await manage_context_before_llm(
                    messages,
                    local_context=local_context,
                    model=model,
                    lite_llm_timeout=lite_llm_timeout,
                    call_litellm=self.mcp_service.call_litellm,
                    reported_prompt_tokens=last_reported_prompt_tokens,
                )
                if pending_multimodal:
                    mm_msg = build_multimodal_user_message(
                        pending_multimodal, model=model
                    )
                    pending_multimodal.clear()
                    if mm_msg:
                        messages.append(mm_msg)
                        logger.info(
                            "Attached multimodal user message with %d content parts",
                            len(mm_msg.get("content") or []),
                        )
                llm_interaction_count += 1
                logger.info(
                    "Calling LiteLLM (segment attempt %s, cumulative tools %s)",
                    segment_tool_call_count + 1,
                    total_tool_call_count,
                )

                use_stream = bool(
                    Config.LLM_STREAMING_ENABLED and agui_context is not None
                )
                stream_msg_id: Optional[str] = None
                stream_started = False

                async def _on_delta(piece: str, _sid_holder={"id": None, "started": False}):
                    nonlocal stream_msg_id, stream_started, text_streamed
                    if not piece or agui_context is None:
                        return
                    if not _sid_holder["started"]:
                        _sid_holder["started"] = True
                        stream_started = True
                        stream_msg_id = str(uuid.uuid4())
                        _sid_holder["id"] = stream_msg_id
                        await self.agui_event_service.publish_events(
                            [
                                TextMessageStartEvent(
                                    message_id=stream_msg_id, role="assistant"
                                )
                            ],
                            thread_id=agui_context.thread_id,
                            run_id=agui_context.run_id,
                        )
                    await self.agui_event_service.publish_events(
                        [
                            TextMessageContentEvent(
                                message_id=_sid_holder["id"], delta=piece
                            )
                        ],
                        thread_id=agui_context.thread_id,
                        run_id=agui_context.run_id,
                    )
                    text_streamed = True

                try:
                    llm_response = await self.mcp_service.call_litellm(
                        messages=messages,
                        model=model,
                        tools=litellm_tools if litellm_tools else None,
                        lite_llm_request_timeout_in_sec=lite_llm_timeout,
                        stream=use_stream,
                        on_content_delta=_on_delta if use_stream else None,
                    )
                finally:
                    if stream_started and stream_msg_id and agui_context is not None:
                        await self.agui_event_service.publish_events(
                            [TextMessageEndEvent(message_id=stream_msg_id)],
                            thread_id=agui_context.thread_id,
                            run_id=agui_context.run_id,
                        )

                choice = llm_response.get("choices", [{}])[0]
                message = choice.get("message", {})

                usage = llm_response.get("usage", {})
                last_reported_prompt_tokens = usage.get("prompt_tokens", 0) or 0
                total_prompt_tokens += last_reported_prompt_tokens
                total_completion_tokens += usage.get("completion_tokens", 0)
                total_tokens += usage.get("total_tokens", 0)

                messages.append(message)

                tool_calls = message.get("tool_calls")

                # Non-stream fallback: emit text that accompanies tool calls.
                content_text = message.get("content") or ""
                if content_text and tool_calls and agui_context and not stream_started:
                    text_msg_id = str(uuid.uuid4())
                    await self.agui_event_service.publish_events(
                        [
                            TextMessageStartEvent(message_id=text_msg_id, role="assistant"),
                            TextMessageContentEvent(message_id=text_msg_id, delta=content_text),
                            TextMessageEndEvent(message_id=text_msg_id),
                        ],
                        thread_id=agui_context.thread_id,
                        run_id=agui_context.run_id,
                    )
                    text_streamed = True
                    logger.info(
                        "Emitted TEXT_MESSAGE for assistant content (%d chars) before tool calls",
                        len(content_text),
                    )

                if not tool_calls:
                    result = {
                        "success": True,
                        "response": message.get("content", "") or "",
                        **result_counter_fields(
                            segment_tool_call_count, total_tool_call_count
                        ),
                        "total_tokens": total_tokens,
                        "prompt_tokens": total_prompt_tokens,
                        "completion_tokens": total_completion_tokens,
                        "tool_calls_info": executed_tool_calls_info,
                        "agui_tool_calls": forwarded_agui_tool_calls,
                        "text_streamed": text_streamed,
                    }
                    await self._emit_session_end(
                        local_context, status="completed", result=result
                    )
                    return result

                batch_pending: List[dict] = []
                batch_completed: List[tuple] = []

                for tool_call in tool_calls:
                    _raise_if_cancelled(cancel_event, cancel_check)
                    if segment_tool_call_count >= max_calls:
                        max_result = {
                            "success": False,
                            "error": f"Maximum tool calls ({max_calls}) reached",
                            "error_code": "MAX_TOOL_CALLS",
                            **result_counter_fields(
                                segment_tool_call_count, total_tool_call_count
                            ),
                            "partial_response": messages[-1].get("content", "")
                            if messages
                            else "",
                            "total_tokens": total_tokens,
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                            "tool_calls_info": executed_tool_calls_info,
                            "agui_tool_calls": forwarded_agui_tool_calls,
                            "text_streamed": text_streamed,
                        }
                        await self._emit_session_end(
                            local_context, status="max_tool_calls", result=max_result
                        )
                        return max_result

                    segment_tool_call_count += 1
                    total_tool_call_count += 1
                    tool_call_id = tool_call.get("id")
                    function_info = tool_call.get("function", {})
                    function_name = function_info.get("name")
                    function_args_str = function_info.get("arguments", "{}")

                    try:
                        function_args = json.loads(function_args_str)
                    except json.JSONDecodeError:
                        function_args = {}

                    exec_outcome = await self._execute_tool_call(
                        function_name=function_name,
                        function_args=function_args,
                        tool_call_id=tool_call_id,
                        total_tool_call_count=total_tool_call_count,
                        llm_interaction_count=llm_interaction_count,
                        tool_registry=tool_registry,
                        active_agui_map=active_agui_map,
                        local_context=local_context,
                        model=model,
                        lite_llm_timeout=lite_llm_timeout,
                        agui_context=agui_context,
                        forwarded_agui_tool_calls=forwarded_agui_tool_calls,
                        executed_tool_calls_info=executed_tool_calls_info,
                        awaits_return=_awaits_return,
                        cancel_event=cancel_event,
                        cancel_check=cancel_check,
                    )
                    if exec_outcome.get("immediate_return"):
                        return exec_outcome["immediate_return"]
                    if exec_outcome.get("pending"):
                        batch_pending.append(exec_outcome["pending"])
                    elif exec_outcome.get("completed"):
                        batch_completed.append(
                            (*exec_outcome["completed"], function_args)
                        )

                if batch_pending:
                    for _tc_id, fn_name, tool_result, fn_args in batch_completed:
                        tool_content = _serialize_and_offload_tool_result(
                            tool_result,
                            tool_call_id=_tc_id or f"call_{segment_tool_call_count}",
                            function_name=fn_name or "tool",
                            function_args=fn_args or {},
                            local_context=local_context,
                            segment_tool_call_count=segment_tool_call_count,
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": _tc_id,
                                "name": fn_name,
                                "content": tool_content,
                            }
                        )
                        if isinstance(tool_result, dict) and tool_result.get("multimodal"):
                            pending_multimodal.append(dict(tool_result))
                    return _awaits_return(batch_pending)

                for _tc_id, function_name, tool_result, fn_args in batch_completed:
                    tool_content = _serialize_and_offload_tool_result(
                        tool_result,
                        tool_call_id=_tc_id or f"call_{segment_tool_call_count}",
                        function_name=function_name or "tool",
                        function_args=fn_args or {},
                        local_context=local_context,
                        segment_tool_call_count=segment_tool_call_count,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": _tc_id,
                            "name": function_name,
                            "content": tool_content,
                        }
                    )
                    if isinstance(tool_result, dict) and tool_result.get("multimodal"):
                        pending_multimodal.append(dict(tool_result))

            max_result = {
                "success": False,
                "error": f"Maximum tool calls ({max_calls}) reached",
                "error_code": "MAX_TOOL_CALLS",
                **result_counter_fields(segment_tool_call_count, total_tool_call_count),
                "partial_response": messages[-1].get("content", "") if messages else "",
                "total_tokens": total_tokens,
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_completion_tokens,
                "tool_calls_info": executed_tool_calls_info,
                "agui_tool_calls": forwarded_agui_tool_calls,
                "text_streamed": text_streamed,
            }
            await self._emit_session_end(
                local_context, status="max_tool_calls", result=max_result
            )
            return max_result
        except Exception as exc:
            logger.error(f"Failed to process request: {exc}")
            err_result = {
                "success": False,
                "error": str(exc),
                **result_counter_fields(
                    segment_tool_call_count
                    if "segment_tool_call_count" in locals()
                    else 0,
                    total_tool_call_count if "total_tool_call_count" in locals() else 0,
                ),
                "total_tokens": total_tokens if "total_tokens" in locals() else 0,
                "prompt_tokens": total_prompt_tokens if "total_prompt_tokens" in locals() else 0,
                "completion_tokens": total_completion_tokens if "total_completion_tokens" in locals() else 0,
                "tool_calls_info": executed_tool_calls_info if "executed_tool_calls_info" in locals() else [],
                "agui_tool_calls": forwarded_agui_tool_calls if "forwarded_agui_tool_calls" in locals() else [],
            }
            if isinstance(exc, LLMUpstreamError):
                err_result["error_code"] = exc.code
            await self._emit_session_end(
                local_context if "local_context" in locals() else None,
                status="error",
                result=err_result,
            )
            return err_result

    def _build_await_result(
        self,
        *,
        pending_tools: list,
        messages: list,
        original_prompt: str,
        model: str,
        max_calls: int,
        requested_tools,
        lite_llm_timeout,
        segment_tool_call_count: int,
        total_tool_call_count: int,
        llm_interaction_count: int,
        total_prompt_tokens: int,
        total_completion_tokens: int,
        total_tokens: int,
        litellm_tools: list,
        selected_agui_tools: list,
        executed_tool_calls_info: list,
        forwarded_agui_tool_calls: list,
        harness_manifest=None,
    ) -> dict:
        """Build persist-and-return result for one or more awaiting tools."""
        enriched = []
        interrupts = build_interrupts_from_pending(pending_tools)
        for i, pt in enumerate(pending_tools):
            entry = dict(pt)
            if i < len(interrupts):
                entry["interrupt_id"] = interrupts[i].id
            enriched.append(entry)
        first = enriched[0] if enriched else {}
        state = {
            "messages": _clone_json(messages),
            "request": original_prompt,
            "model": model,
            "max_calls": max_calls,
            "requested_tools": requested_tools,
            "lite_llm_request_timeout_in_sec": lite_llm_timeout,
            **snapshot_counter_fields(segment_tool_call_count, total_tool_call_count),
            "llm_interaction_count": llm_interaction_count,
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "total_tokens": total_tokens,
            "litellm_tools": _clone_json(litellm_tools),
            "agui_records": [asdict(record) for record in selected_agui_tools],
            "agui_tool_calls": _clone_json(forwarded_agui_tool_calls),
            "tool_calls_info": _clone_json(executed_tool_calls_info),
            "pending_tools": enriched,
            "pending_tool": first,
            "harness_manifest": harness_manifest,
        }
        return {
            "success": True,
            "response": None,
            "error": None,
            **result_counter_fields(segment_tool_call_count, total_tool_call_count),
            "total_tokens": total_tokens,
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "tool_calls_info": executed_tool_calls_info,
            "agui_tool_calls": forwarded_agui_tool_calls,
            "awaits_response": True,
            "pending_tools": enriched,
            "pending_tool": first,
            "interrupts": [
                i.model_dump(by_alias=True, exclude_none=True)
                for i in interrupts
            ],
            "state": state,
        }

    async def _execute_tool_call(
        self,
        *,
        function_name: str,
        function_args: dict,
        tool_call_id: Optional[str],
        total_tool_call_count: int,
        llm_interaction_count: int,
        tool_registry: ToolRegistry,
        active_agui_map: dict,
        local_context: Optional[LocalToolContext],
        model: str,
        lite_llm_timeout,
        agui_context: Optional[AGUIRunContext],
        forwarded_agui_tool_calls: list,
        executed_tool_calls_info: list,
        awaits_return,
        cancel_event: Optional[asyncio.Event] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Execute one tool call; return pending, completed, or immediate_return."""
        registered = tool_registry.resolve(function_name or "")
        tool_source = "MCP"
        agui_original_name = None
        mcp_tool: Optional[MCPTool] = None
        route_name = function_name

        if registered and registered.provider == ToolProvider.LOCAL:
            tool_source = "LOCAL"
            route_name = registered.local_base_name or function_name
            if registered.awaits_response or local_tool_provider.awaits_response(route_name):
                pending_tool = {
                    "tool_call_id": tool_call_id,
                    "function_name": route_name,
                    "arguments": function_args,
                    "source": "LOCAL",
                }
                if agui_context:
                    await self.agui_event_service.publish_tool_call(
                        tool_call_id=tool_call_id,
                        tool_name=route_name,
                        arguments=function_args,
                        thread_id=agui_context.thread_id,
                        run_id=agui_context.run_id,
                        awaits_response=True,
                    )
                return {"pending": pending_tool}
            tool_result = await self._execute_local_with_hooks(
                function_name=route_name,
                function_args=function_args,
                tool_call_id=tool_call_id,
                local_context=local_context,
                model=model,
                lite_llm_timeout=lite_llm_timeout,
                parent_agui=agui_context,
                cancel_event=cancel_event,
                cancel_check=cancel_check,
            )
            if isinstance(tool_result, dict) and tool_result.get("_hookAsk"):
                hook_return = await self._pause_for_hook_ask(
                    hook_marker=tool_result,
                    original_tool_call_id=tool_call_id,
                    function_name=route_name,
                    function_args=function_args,
                    agui_context=agui_context,
                    active_agui_map=active_agui_map,
                    awaits_return=awaits_return,
                )
                return {"immediate_return": hook_return}
        elif registered and registered.provider == ToolProvider.AGUI and registered.agui_record:
            tool_source = "AGUI"
            agui_record = registered.agui_record
            agui_original_name = agui_record.original_name
            display_name = agui_original_name or registered.canonical_name or function_name
            forwarded_agui_tool_calls.append(
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": display_name,
                    "prefixed_name": agui_record.prefixed_name or function_name,
                    "arguments": function_args,
                    "awaits_response": agui_record.awaits_response,
                }
            )
            await self.agui_event_service.publish_tool_call(
                tool_call_id=tool_call_id,
                tool_name=display_name,
                arguments=function_args,
                thread_id=agui_context.thread_id if agui_context else None,
                run_id=agui_context.run_id if agui_context else None,
                awaits_response=agui_record.awaits_response,
            )
            if agui_record.awaits_response:
                return {
                    "pending": {
                        "tool_call_id": tool_call_id,
                        "function_name": display_name,
                        "arguments": function_args,
                        "agui_original_name": agui_original_name,
                        "prefixed_name": agui_record.prefixed_name,
                        "source": "AGUI",
                    }
                }
            tool_result = self.agui_service.handle_tool_call(function_name, function_args)
        elif (
            local_context is not None
            and local_tool_provider.enabled
            and local_tool_provider.is_local_tool(function_name)
        ):
            # Legacy alias resolution fallback.
            tool_source = "LOCAL"
            if local_tool_provider.awaits_response(function_name):
                pending_tool = {
                    "tool_call_id": tool_call_id,
                    "function_name": function_name,
                    "arguments": function_args,
                    "source": "LOCAL",
                }
                if agui_context:
                    await self.agui_event_service.publish_tool_call(
                        tool_call_id=tool_call_id,
                        tool_name=function_name,
                        arguments=function_args,
                        thread_id=agui_context.thread_id,
                        run_id=agui_context.run_id,
                        awaits_response=True,
                    )
                return {"pending": pending_tool}
            tool_result = await self._execute_local_with_hooks(
                function_name=function_name,
                function_args=function_args,
                tool_call_id=tool_call_id,
                local_context=local_context,
                model=model,
                lite_llm_timeout=lite_llm_timeout,
                parent_agui=agui_context,
                cancel_event=cancel_event,
                cancel_check=cancel_check,
            )
            if isinstance(tool_result, dict) and tool_result.get("_hookAsk"):
                hook_return = await self._pause_for_hook_ask(
                    hook_marker=tool_result,
                    original_tool_call_id=tool_call_id,
                    function_name=function_name,
                    function_args=function_args,
                    agui_context=agui_context,
                    active_agui_map=active_agui_map,
                    awaits_return=awaits_return,
                )
                return {"immediate_return": hook_return}
        elif function_name in active_agui_map:
            agui_record = active_agui_map[function_name]
            tool_source = "AGUI"
            agui_original_name = agui_record.original_name
            display_name = agui_original_name or function_name
            forwarded_agui_tool_calls.append(
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": display_name,
                    "prefixed_name": agui_record.prefixed_name or function_name,
                    "arguments": function_args,
                    "awaits_response": agui_record.awaits_response,
                }
            )
            await self.agui_event_service.publish_tool_call(
                tool_call_id=tool_call_id,
                tool_name=display_name,
                arguments=function_args,
                thread_id=agui_context.thread_id if agui_context else None,
                run_id=agui_context.run_id if agui_context else None,
                awaits_response=agui_record.awaits_response,
            )
            if agui_record.awaits_response:
                return {
                    "pending": {
                        "tool_call_id": tool_call_id,
                        "function_name": display_name,
                        "arguments": function_args,
                        "agui_original_name": agui_original_name,
                        "prefixed_name": agui_record.prefixed_name,
                        "source": "AGUI",
                    }
                }
            tool_result = self.agui_service.handle_tool_call(function_name, function_args)
        else:
            mcp_name = registered.mcp_tool.name if (registered and registered.mcp_tool) else function_name
            mcp_tool = self.mcp_service.find_tool_by_name(mcp_name) or (
                registered.mcp_tool if registered else None
            )
            tool_result = await self.mcp_service.execute_mcp_tool(mcp_name, function_args)

        executed_tool_calls_info.append(
            {
                "tool_index": total_tool_call_count,
                "tool_name": function_name,
                "llm_tool_interaction_index": llm_interaction_count,
                "mcp_server_id": mcp_tool.server_name if (tool_source == "MCP" and mcp_tool) else None,
                "mcp_server_url": mcp_tool.server_url if (tool_source == "MCP" and mcp_tool) else None,
                "tool_source": tool_source,
                "agui_original_name": agui_original_name,
            }
        )
        return {"completed": (tool_call_id, function_name, tool_result)}

    async def _emit_session_end(
        self,
        local_context: Optional[LocalToolContext],
        *,
        status: str,
        result: dict,
    ) -> None:
        if local_context is None:
            return
        await run_hooks(
            local_context.workspace_path,
            "sessionEnd",
            {
                "status": status,
                "success": bool(result.get("success")),
                "error": result.get("error"),
            },
        )

    async def _pause_for_hook_ask(
        self,
        *,
        hook_marker: dict,
        original_tool_call_id: Optional[str],
        function_name: str,
        function_args: dict,
        agui_context: Optional[AGUIRunContext],
        active_agui_map: dict,
        awaits_return,
    ) -> dict:
        """Pause for generic hook permission — no synthetic frontend tool call events."""
        del active_agui_map  # routing metadata only; never select frontend tool names here.
        permission_id = str(uuid.uuid4())
        message = hook_marker.get("message") or "Hook requested confirmation"
        event = hook_marker.get("event") or "hook"
        metadata = {
            "source": HOOK_PERMISSION_SOURCE,
            "hookEvent": event,
            "deferredToolCallId": original_tool_call_id,
            "deferredFunctionName": function_name,
            "deferredArguments": function_args,
        }
        pending_tool = {
            "tool_call_id": permission_id,
            "interrupt_id": permission_id,
            "source": HOOK_PERMISSION_SOURCE,
            "reason": "hook_permission",
            "message": message,
            "response_schema": HOOK_PERMISSION_RESPONSE_SCHEMA,
            "metadata": metadata,
            "original_tool_call_id": original_tool_call_id,
            "deferred_function_name": function_name,
            "deferred_arguments": function_args,
            "hook_event": event,
        }
        return awaits_return([pending_tool])

    async def prepare_hook_permission_resume_events(
        self,
        *,
        state_payload: dict,
        resume_tool_results: Sequence[dict],
        local_context: Optional[LocalToolContext],
        model: str,
        lite_llm_timeout: Optional[int],
        agui_context: Optional[AGUIRunContext],
    ) -> tuple[list, dict]:
        """Resolve hook-permission resumes early; return SSE events for deferred tools."""
        from ag_ui.core.events import ToolCallResultEvent

        messages = list(state_payload.get("messages") or [])
        pending_tools = state_payload.get("pending_tools") or []
        if not pending_tools and state_payload.get("pending_tool"):
            pending_tools = [state_payload["pending_tool"]]
        pending_by_id: dict[str, dict] = {}
        for pt in pending_tools:
            if not is_hook_permission_pending(pt):
                continue
            tc_key = pt.get("tool_call_id")
            if tc_key:
                pending_by_id[str(tc_key)] = pt
            intr_key = pt.get("interrupt_id")
            if intr_key:
                pending_by_id[str(intr_key)] = pt
        resolved_permission_ids: set[str] = set()
        events: list[ToolCallResultEvent] = []
        for resp in resume_tool_results or []:
            tc_id = resp.get("tool_call_id") or resp.get("toolCallId")
            intr_id = resp.get("interrupt_id") or resp.get("interruptId")
            pending = pending_by_id.get(str(tc_id or "")) or pending_by_id.get(str(intr_id or ""))
            if not pending:
                continue
            original_id = pending.get("original_tool_call_id")
            if not original_id or _message_has_tool_result(messages, original_id):
                continue
            await self._append_hook_ask_resume(
                messages=messages,
                pending_tool=pending,
                resume_tool_result=resp,
                local_context=local_context,
                model=model,
                lite_llm_timeout=lite_llm_timeout,
                agui_context=agui_context,
                pending_multimodal=None,
            )
            perm_id = pending.get("tool_call_id")
            if perm_id:
                resolved_permission_ids.add(str(perm_id))
            tool_msg = next(
                (m for m in reversed(messages) if m.get("tool_call_id") == original_id),
                None,
            )
            if tool_msg:
                events.append(
                    ToolCallResultEvent(
                        tool_call_id=original_id,
                        message_id=str(uuid.uuid4()),
                        content=tool_msg.get("content") or "{}",
                        role="tool",
                    )
                )
        if resolved_permission_ids:
            pending_tools = [
                pt
                for pt in pending_tools
                if str(pt.get("tool_call_id") or "") not in resolved_permission_ids
            ]
            state_payload["pending_tools"] = pending_tools
            state_payload["pending_tool"] = pending_tools[0] if pending_tools else None
        state_payload["messages"] = messages
        return events, state_payload

    async def _append_hook_ask_resume(
        self,
        *,
        messages: list,
        pending_tool: dict,
        resume_tool_result: Any,
        local_context: Optional[LocalToolContext],
        model: str,
        lite_llm_timeout: Optional[int],
        agui_context: Optional[AGUIRunContext],
        pending_multimodal: Optional[List[dict]] = None,
    ) -> None:
        """Apply a human decision for a prior hook-ask and append the tool result."""
        deferred_name = pending_tool.get("deferred_function_name")
        deferred_args = pending_tool.get("deferred_arguments") or {}
        original_id = pending_tool.get("original_tool_call_id")
        approved = is_hook_ask_approved(resume_tool_result)

        if not approved:
            deny_msg = (
                hook_permission_deny_reason(resume_tool_result)
                or pending_tool.get("message")
                or "User denied hook confirmation"
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": original_id,
                    "name": deferred_name,
                    "content": json.dumps(
                        {
                            "error": deny_msg,
                            "hookBlocked": True,
                            "hookAskDenied": True,
                        }
                    ),
                }
            )
            return

        if local_context is None:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": original_id,
                    "name": deferred_name,
                    "content": json.dumps(
                        {"error": "No workspace bound to resume hook-approved tool"}
                    ),
                }
            )
            return

        deferred_result = await self._execute_local_with_hooks(
            function_name=deferred_name,
            function_args=deferred_args,
            tool_call_id=original_id,
            local_context=local_context,
            model=model,
            lite_llm_timeout=lite_llm_timeout,
            parent_agui=agui_context,
            skip_ask_events={pending_tool.get("hook_event") or ""},
        )
        if isinstance(deferred_result, dict) and deferred_result.get("_hookAsk"):
            deferred_result = {
                "error": (
                    "Another hook requested confirmation after approval; "
                    "re-issue the tool call."
                ),
                "hookBlocked": True,
            }
        messages.append(
            {
                "role": "tool",
                "tool_call_id": original_id,
                "name": deferred_name,
                "content": serialize_tool_result(deferred_result),
            }
        )
        if (
            pending_multimodal is not None
            and isinstance(deferred_result, dict)
            and deferred_result.get("multimodal")
        ):
            pending_multimodal.append(dict(deferred_result))

    async def _execute_local_with_hooks(
        self,
        *,
        function_name: str,
        function_args: dict,
        tool_call_id: Optional[str],
        local_context: LocalToolContext,
        model: str,
        lite_llm_timeout: Optional[int],
        parent_agui: Optional[AGUIRunContext],
        skip_ask_events: Optional[set] = None,
        cancel_event: Optional[asyncio.Event] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Run a local tool with Cursor/Claude pre/post hooks around it."""
        matcher = tool_matcher_name(function_name or "")
        file_path = _file_path_arg(function_name or "", function_args)
        workspace = local_context.workspace_path
        skip = skip_ask_events or set()

        async def _gate(event: str, decision) -> Optional[dict]:
            if decision.needs_confirmation and event in skip:
                # Already approved by the user for this event on resume.
                return None
            return _apply_hook_decision(decision, event=event)

        pre = await run_hooks(
            workspace,
            "preToolUse",
            {
                "tool_name": matcher,
                "tool_input": function_args,
                "tool_call_id": tool_call_id,
            },
            matcher_subject=matcher,
        )
        blocked = await _gate("preToolUse", pre)
        if blocked:
            return blocked

        if matcher == "Shell":
            command = str(function_args.get("command") or "")
            before_shell = await run_hooks(
                workspace,
                "beforeShellExecution",
                {"command": command, "cwd": "."},
                matcher_subject=command,
            )
            blocked = await _gate("beforeShellExecution", before_shell)
            if blocked:
                return blocked

        if file_path and matcher == "Read":
            before_read = await run_hooks(
                workspace,
                "beforeReadFile",
                {"file_path": file_path, "tool_call_id": tool_call_id},
                matcher_subject=file_path,
            )
            blocked = await _gate("beforeReadFile", before_read)
            if blocked:
                if not blocked.get("_hookAsk"):
                    await run_hooks(
                        workspace,
                        "postToolUseFailure",
                        {
                            "tool_name": matcher,
                            "tool_input": function_args,
                            "error_message": blocked.get("error"),
                        },
                        matcher_subject=matcher,
                    )
                return blocked

        try:
            if local_tool_provider.is_hub_managed(function_name):
                tool_result = await self._run_hub_managed_local_tool(
                    function_name,
                    function_args,
                    local_context=local_context,
                    model=model,
                    lite_llm_timeout=lite_llm_timeout,
                    parent_agui=parent_agui,
                    cancel_event=cancel_event,
                    cancel_check=cancel_check,
                )
            else:
                tool_result = await local_tool_provider.execute(
                    function_name, function_args, local_context
                )
        except Exception as exc:
            await run_hooks(
                workspace,
                "postToolUseFailure",
                {
                    "tool_name": matcher,
                    "tool_input": function_args,
                    "error_message": str(exc),
                },
                matcher_subject=matcher,
            )
            raise

        if file_path and matcher == "Write":
            await run_hooks(
                workspace,
                "afterFileEdit",
                {"file_path": file_path, "tool_call_id": tool_call_id},
                matcher_subject=file_path,
            )
        await run_hooks(
            workspace,
            "postToolUse",
            {
                "tool_name": matcher,
                "tool_input": function_args,
                "tool_output": tool_result if isinstance(tool_result, dict) else {"result": tool_result},
            },
            matcher_subject=matcher,
        )
        return tool_result

    async def _run_hub_managed_local_tool(
        self,
        function_name: str,
        function_args: dict,
        *,
        local_context: LocalToolContext,
        model: str,
        lite_llm_timeout: Optional[int],
        parent_agui: Optional[AGUIRunContext],
        cancel_event: Optional[asyncio.Event] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> dict:
        if function_name == local_tool_provider.prefixed("task"):
            return await self._run_subagent(
                function_args,
                local_context=local_context,
                model=model,
                lite_llm_timeout=lite_llm_timeout,
                parent_agui=parent_agui,
                cancel_event=cancel_event,
                cancel_check=cancel_check,
            )
        return {"error": f"Hub-managed tool '{function_name}' is not implemented"}

    async def _run_subagent(
        self,
        args: dict,
        *,
        local_context: LocalToolContext,
        model: str,
        lite_llm_timeout: Optional[int],
        parent_agui: Optional[AGUIRunContext],
        cancel_event: Optional[asyncio.Event] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> dict:
        if not Config.SUBAGENT_ENABLED:
            return {"error": "task_local is disabled (SUBAGENT_ENABLED=false)"}

        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return {"error": "prompt is required"}

        depth = getattr(local_context, "subagent_depth", 0) or 0
        if depth >= Config.SUBAGENT_MAX_DEPTH:
            return {
                "error": (
                    f"Subagent max depth ({Config.SUBAGENT_MAX_DEPTH}) reached; "
                    "complete the work in the current agent instead"
                )
            }

        agent_ref = (args.get("agent") or "").strip()
        description = (args.get("description") or "").strip()
        agent_system = self._load_agent_system_prompt(agent_ref, local_context)
        system_prompt = (
            "You are a focused subagent. Complete the assigned task, use tools as "
            "needed, and return a concise final summary of findings or changes. "
            "Do not ask the user questions — if blocked, report what is missing."
        )
        if agent_system:
            system_prompt = f"{system_prompt}\n\n# Agent role\n{agent_system}"
        try:
            from app.services.harness import collect_manifest, render_subagent_catalog

            sub_manifest = collect_manifest(local_context.workspace_path)
            catalog = render_subagent_catalog(sub_manifest)
            if catalog:
                system_prompt = f"{system_prompt}\n\n{catalog}"
        except Exception:
            pass

        child_ctx = LocalToolContext(
            workspace_path=local_context.workspace_path,
            in_place=local_context.in_place,
            subagent_depth=depth + 1,
            runtime_path=local_context.runtime_path,
            mode=local_context.mode,
            output_backend=local_context.output_backend,
        )
        child_agui = None
        if parent_agui is not None:
            child_agui = AGUIRunContext(
                thread_id=parent_agui.thread_id,
                run_id=str(uuid.uuid4()),
                parent_run_id=parent_agui.run_id,
            )

        logger.info(
            "Starting subagent depth=%s desc=%s agent=%s",
            child_ctx.subagent_depth,
            description or "(none)",
            agent_ref or "(generic)",
        )
        result = await self.process_request(
            request=prompt,
            model=model,
            max_tool_calls=Config.SUBAGENT_MAX_TOOL_CALLS,
            lite_llm_request_timeout_in_sec=lite_llm_timeout,
            include_agui_tools=False,
            agui_context=child_agui,
            local_context=child_ctx,
            system_prompt=system_prompt,
            cancel_event=cancel_event,
            cancel_check=cancel_check,
        )
        summary = result.get("response") or result.get("partial_response") or result.get("error")
        return {
            "success": bool(result.get("success")),
            "summary": summary,
            "description": description or None,
            "agent": agent_ref or None,
            "tool_calls_made": result.get("tool_calls_made", 0),
            "total_tokens": result.get("total_tokens", 0),
            "error": None if result.get("success") else result.get("error"),
        }

    def _load_agent_system_prompt(
        self, agent_ref: str, local_context: LocalToolContext
    ) -> Optional[str]:
        if not agent_ref:
            return None
        workspace = Path(local_context.workspace_path)
        candidates: List[Path] = []
        # Path-like reference.
        if "/" in agent_ref or agent_ref.endswith(".md"):
            try:
                resolved = Path(
                    workspace_manager.resolve_within(local_context.workspace_path, agent_ref)
                )
                candidates.append(resolved)
            except workspace_manager.WorkspaceError:
                pass
        else:
            for rel in (
                f".cursor/agents/{agent_ref}.md",
                f".claude/agents/{agent_ref}.md",
                f"agents/{agent_ref}.md",
            ):
                candidates.append(workspace / rel)

        for path in candidates:
            if path.is_file():
                try:
                    return path.read_text(encoding="utf-8", errors="replace")[:12000]
                except OSError:
                    continue
        return None

    def _select_tools(
        self,
        requested_tools: Optional[List[str]],
        all_mcp_tools: List[MCPTool],
        agui_records: List[AGUIToolRecord],
        mcp_by_name: Dict[str, MCPTool],
        agui_by_name: Dict[str, AGUIToolRecord],
    ) -> Tuple[List[MCPTool], List[AGUIToolRecord]]:
        """Return the MCP and AG-UI tool subsets requested for this run."""
        if requested_tools is None:
            logger.info(f"Using all {len(all_mcp_tools)} available MCP tools (requested_tools=None)")
            return all_mcp_tools, agui_records

        if len(requested_tools) == 0:
            logger.info("Using 0 requested tools (empty list provided)")
            return [], []

        selected_mcp: List[MCPTool] = []
        selected_agui: List[AGUIToolRecord] = []
        missing: List[str] = []

        for tool_name in requested_tools:
            if tool_name in mcp_by_name:
                selected_mcp.append(mcp_by_name[tool_name])
            elif tool_name in agui_by_name:
                selected_agui.append(agui_by_name[tool_name])
            else:
                missing.append(tool_name)

        if missing:
            raise ValueError(f"Requested tools not found: {', '.join(missing)}")

        logger.info(
            f"Using {len(selected_mcp)} MCP tools and {len(selected_agui)} AG-UI tools: {requested_tools}"
        )
        return selected_mcp, selected_agui
