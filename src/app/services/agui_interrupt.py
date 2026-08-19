# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""AG-UI interrupt/resume helpers aligned with ag-ui-protocol canonical semantics."""

from __future__ import annotations

import json
from typing import Any, List, Optional, Sequence

from ag_ui.core.events import (
    Interrupt,
    MessagesSnapshotEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    StateSnapshotEvent,
)
from ag_ui.core.types import ResumeEntry, ResumeStatus

HOOK_PERMISSION_SOURCE = "hook_permission"
HOOK_PERMISSION_SOURCES = frozenset({HOOK_PERMISSION_SOURCE, "HOOK_ASK"})
HOOK_PERMISSION_REASON = "hook_permission"

HOOK_PERMISSION_DECISION_APPROVE = "approve"
HOOK_PERMISSION_DECISION_DENY = "deny"
HOOK_PERMISSION_DECISIONS = frozenset(
    {HOOK_PERMISSION_DECISION_APPROVE, HOOK_PERMISSION_DECISION_DENY}
)

HOOK_PERMISSION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": [HOOK_PERMISSION_DECISION_APPROVE, HOOK_PERMISSION_DECISION_DENY],
            "description": "Whether to allow the deferred action.",
        },
        "reason": {
            "type": "string",
            "description": "Optional explanation when decision is deny.",
        },
    },
    "required": ["decision"],
}


def is_hook_permission_pending(pt: dict) -> bool:
    return (pt.get("source") or "") in HOOK_PERMISSION_SOURCES


def build_interrupts_from_pending(
    pending_tools: Sequence[dict],
    *,
    default_reason: str = "tool_awaiting_response",
) -> List[Interrupt]:
    """Build canonical Interrupt objects for each awaiting tool call."""
    interrupts: list[Interrupt] = []
    for pt in pending_tools:
        tool_call_id = pt.get("tool_call_id") or pt.get("toolCallId")
        if not tool_call_id:
            continue
        interrupt_id = str(pt.get("interrupt_id") or pt.get("interruptId") or tool_call_id)
        fn = pt.get("agui_original_name") or pt.get("function_name") or "tool"
        reason = pt.get("reason") or default_reason
        message = pt.get("message")
        if not message:
            if is_hook_permission_pending(pt):
                message = pt.get("message") or "Hook permission required"
            else:
                message = f"Awaiting response for tool '{fn}'"
        response_schema = pt.get("response_schema") or pt.get("responseSchema")
        metadata = dict(pt.get("metadata") or {})
        if pt.get("source") and "source" not in metadata:
            metadata["source"] = pt.get("source")
        if not is_hook_permission_pending(pt):
            metadata.setdefault("functionName", fn)
            if pt.get("arguments") is not None:
                metadata.setdefault("arguments", pt.get("arguments"))
        if pt.get("hook_event") and "hookEvent" not in metadata:
            metadata["hookEvent"] = pt.get("hook_event")
        deferred_id = pt.get("original_tool_call_id") or pt.get("deferred_tool_call_id")
        if deferred_id and "deferredToolCallId" not in metadata:
            metadata["deferredToolCallId"] = deferred_id
        if pt.get("deferred_function_name") and "deferredFunctionName" not in metadata:
            metadata["deferredFunctionName"] = pt.get("deferred_function_name")
        if pt.get("deferred_arguments") is not None and "deferredArguments" not in metadata:
            metadata["deferredArguments"] = pt.get("deferred_arguments")
        interrupts.append(
            Interrupt(
                id=interrupt_id,
                reason=reason,
                message=message,
                tool_call_id=deferred_id or tool_call_id,
                response_schema=response_schema,
                metadata=metadata or None,
            )
        )
    return interrupts


def build_interrupt_outcome(pending_tools: Sequence[dict]) -> RunFinishedInterruptOutcome:
    return RunFinishedInterruptOutcome(
        type="interrupt",
        interrupts=build_interrupts_from_pending(pending_tools),
    )


def build_run_finished_interrupt_event(
    *,
    thread_id: str,
    run_id: str,
    pending_tools: Sequence[dict],
    execution_guid: Optional[str] = None,
    state_guid: Optional[str] = None,
    messages_snapshot: Optional[Any] = None,
) -> RunFinishedEvent:
    """Build RUN_FINISHED with canonical interrupt outcome plus legacy result fields."""
    legacy: dict[str, Any] = {
        "awaitsResponse": True,
    }
    if execution_guid:
        legacy["executionGuid"] = execution_guid
    if state_guid:
        legacy["stateGuid"] = state_guid
    if pending_tools:
        first = pending_tools[0]
        legacy["toolCallId"] = first.get("tool_call_id")
        legacy["pendingToolCallIds"] = [
            pt.get("tool_call_id") for pt in pending_tools if pt.get("tool_call_id")
        ]

    outcome = build_interrupt_outcome(pending_tools)
    return RunFinishedEvent(
        thread_id=thread_id,
        run_id=run_id,
        result=legacy,
        outcome=outcome,
    )


def build_persist_snapshot_events(
    *,
    state_payload: dict,
    pending_tools: Sequence[dict],
) -> List[Any]:
    """Build STATE/Messages snapshots aligned with persisted interrupt state."""
    from app.services.agui_messages import litellm_messages_to_agui_snapshot

    messages = state_payload.get("messages") or []
    snapshot_state = {
        "executionGuid": state_payload.get("executionGuid"),
        "stateGuid": state_payload.get("stateGuid"),
        "pendingToolCallIds": [
            pt.get("tool_call_id") for pt in pending_tools if pt.get("tool_call_id")
        ],
        "interruptIds": [
            pt.get("interrupt_id") or pt.get("tool_call_id") for pt in pending_tools
        ],
        "harnessManifest": state_payload.get("harness_manifest"),
    }
    return [
        MessagesSnapshotEvent(messages=litellm_messages_to_agui_snapshot(messages)),
        StateSnapshotEvent(snapshot=snapshot_state),
    ]


def extract_resume_entries(payload: Any) -> List[ResumeEntry]:
    """Parse canonical resume[] from request payload."""
    if not payload:
        return []
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict) and "resume" in payload:
        entries = payload["resume"]
    else:
        return []
    out: list[ResumeEntry] = []
    for item in entries:
        if isinstance(item, ResumeEntry):
            out.append(item)
        elif isinstance(item, dict):
            try:
                out.append(ResumeEntry.model_validate(item))
            except Exception:
                continue
    return out


def resume_entry_to_tool_response(entry: ResumeEntry) -> dict:
    """Convert a ResumeEntry into an internal tool-response dict."""
    result: dict[str, Any] = {"interrupt_id": entry.interrupt_id, "status": entry.status}
    payload = entry.payload
    if entry.status == "cancelled":
        error = "cancelled"
        if isinstance(payload, dict):
            error = str(payload.get("error") or payload.get("message") or "cancelled")
        elif isinstance(payload, str) and payload.strip():
            error = payload.strip()
        result["error"] = error
        result["cancelled"] = True
        if isinstance(payload, dict):
            tc_id = payload.get("toolCallId") or payload.get("tool_call_id")
            if tc_id:
                result["tool_call_id"] = tc_id
        return result
    if isinstance(payload, dict):
        tc_id = payload.get("toolCallId") or payload.get("tool_call_id")
        if tc_id:
            result["tool_call_id"] = tc_id
        if entry.status == "resolved":
            inner = payload.get("result", payload)
            result["result"] = inner
    elif payload is not None:
        result["result"] = payload
    return result


def tool_response_content(resp: dict) -> Any:
    """Normalize a resume/tool response into LLM tool message JSON content."""
    if resp.get("status") == "cancelled" or resp.get("cancelled"):
        return {
            "error": resp.get("error") or "cancelled",
            "cancelled": True,
        }
    if resp.get("error") is not None:
        return {"error": resp["error"]}
    if resp.get("result") is not None:
        return resp["result"]
    internal = {"tool_call_id", "toolCallId", "interrupt_id", "status", "cancelled"}
    bare = {k: v for k, v in resp.items() if k not in internal}
    if bare:
        return bare
    return {"status": "acknowledged"}


def _pending_tools_from_state(state_payload: Optional[dict]) -> list[dict]:
    pending = (state_payload or {}).get("pending_tools") or []
    if not pending and (state_payload or {}).get("pending_tool"):
        pending = [(state_payload or {}).get("pending_tool")]
    return list(pending)


def _build_interrupt_to_tool_call_map(pending_tools: Sequence[dict]) -> dict[str, str]:
    """Map interrupt IDs (canonical or persisted) to tool_call_id."""
    interrupt_map: dict[str, str] = {}
    pending_ids: set[str] = set()
    for pt in pending_tools:
        tc_id = pt.get("tool_call_id") or pt.get("toolCallId")
        if not tc_id:
            continue
        pending_ids.add(str(tc_id))
        intr_id = pt.get("interrupt_id") or pt.get("interruptId")
        if intr_id:
            interrupt_map[str(intr_id)] = str(tc_id)
        # Canonical round-trip: Interrupt.id == tool_call_id.
        interrupt_map[str(tc_id)] = str(tc_id)
        for intr in pt.get("interrupts") or []:
            if not isinstance(intr, dict):
                continue
            iid = intr.get("id")
            mapped = intr.get("toolCallId") or intr.get("tool_call_id") or tc_id
            if iid:
                interrupt_map[str(iid)] = str(mapped)
    return interrupt_map, pending_ids


def normalize_resume_responses(
    *,
    resume_entries: Optional[Sequence[ResumeEntry]] = None,
    legacy_tool_response: Optional[dict] = None,
    state_payload: Optional[dict] = None,
) -> List[dict]:
    """Merge canonical resume[] and legacy toolResponse into normalized responses."""
    responses: list[dict] = []
    pending_tools = _pending_tools_from_state(state_payload)
    interrupt_map, pending_ids = _build_interrupt_to_tool_call_map(pending_tools)

    for entry in resume_entries or []:
        resp = resume_entry_to_tool_response(entry)
        tc_id = resp.get("tool_call_id")
        if not tc_id:
            tc_id = interrupt_map.get(str(entry.interrupt_id))
        if not tc_id and str(entry.interrupt_id) in pending_ids:
            tc_id = str(entry.interrupt_id)
        if tc_id:
            resp["tool_call_id"] = tc_id
            responses.append(resp)

    if legacy_tool_response:
        tc_id = legacy_tool_response.get("tool_call_id") or legacy_tool_response.get("toolCallId")
        if tc_id and not any(r.get("tool_call_id") == tc_id for r in responses):
            legacy = dict(legacy_tool_response)
            legacy["tool_call_id"] = tc_id
            responses.append(legacy)

    return responses
