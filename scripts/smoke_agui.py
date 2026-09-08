#!/usr/bin/env python3
# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""
AG-UI compatibility smoke check. Requires a running orchestrator service; set
PY_MAIN_AGENT_BASE_URL to target something other than http://localhost:8000.

Simulates a UI-BE by:
1. Fetching MCP tools.
2. Pushing AG-UI frontend tools (bind-only run).
3. Verifying merged tool list.
4. Replacing the frontend tools and verifying the cache.
5. Triggering an AG-UI run that forces the LLM to call an AG-UI tool and observing
   the TOOL_CALL_* events over SSE.
"""

import asyncio
import json
import os
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

BASE_URL = os.getenv("PY_MAIN_AGENT_BASE_URL", "http://localhost:8000")
MODEL = os.getenv("PY_MAIN_AGENT_SMOKE_MODEL", "gpt-4o")
# An MCP tool to include alongside the UI tool. Deployment-specific, so it is
# omitted unless named: an unknown name makes the whole run fail tool selection.
MCP_TOOL = os.getenv("PY_MAIN_AGENT_SMOKE_MCP_TOOL", "").strip()


def _prefixed_name(tool_name: str) -> str:
    return f"AGUI-{tool_name}"


async def fetch_tools() -> Tuple[List[Dict[str, Any]], List[str]]:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/v1/agent/getTools", timeout=30.0)
        response.raise_for_status()
        data = response.json()
        if not data.get("success"):
            raise RuntimeError(f"/getTools returned success=false: {data}")
        tools = data.get("tools", [])
        names = [tool.get("name") for tool in tools]
        return tools, names


def _build_frontend_tool(name: str, description: str) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Text to render in the UI"}
            },
            "required": ["message"]
        }
    }


async def stream_agui_run(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", f"{BASE_URL}/api/ag-ui/run", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if not data_str:
                    continue
                try:
                    events.append(json.loads(data_str))
                except json.JSONDecodeError:
                    continue
    return events


async def stream_global_agui_events(expected_tool_name: str) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Listen on /api/ag-ui/events until we see the expected tool call."""
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("GET", f"{BASE_URL}/api/ag-ui/events") as response:
            response.raise_for_status()
            tool_event: Optional[Dict[str, Any]] = None
            args_event: Optional[Dict[str, Any]] = None
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload:
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                if event.get("type") == "TOOL_CALL_START" and event.get("toolCallName") == expected_tool_name:
                    tool_event = event
                elif (
                    tool_event
                    and event.get("type") == "TOOL_CALL_ARGS"
                    and event.get("toolCallId") == tool_event.get("toolCallId")
                ):
                    args_event = event
                    break

            if not tool_event:
                raise RuntimeError("SSE stream closed before emitting the expected tool call")

            return tool_event, args_event


async def execute_agent_request(task: str, tools: List[str], model: str = MODEL, max_tool_calls: int = 6) -> Dict[str, Any]:
    payload = {
        "task": task,
        "tools": tools,
        "model": model,
        "max_tool_calls": max_tool_calls
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{BASE_URL}/v1/agent/executeRequest", json=payload, timeout=60.0)
        response.raise_for_status()
        return response.json()


async def bind_ui_tools(frontend_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    payload = {
        "threadId": str(uuid.uuid4()),
        "runId": str(uuid.uuid4()),
        "messages": [],
        "frontendTools": frontend_tools,
        "context": [],
    }
    events = await stream_agui_run(payload)
    print(f"Bind-only run emitted {len(events)} events")
    return events


async def run_with_ui_tool(frontend_tools: List[Dict[str, Any]], user_prompt: str) -> List[Dict[str, Any]]:
    message_id = str(uuid.uuid4())
    payload = {
        "threadId": str(uuid.uuid4()),
        "runId": str(uuid.uuid4()),
        "messages": [
            {
                "id": message_id,
                "role": "user",
                "content": user_prompt
            }
        ],
        "frontendTools": frontend_tools,
        "context": [],
        "maxToolCalls": 4
    }
    return await stream_agui_run(payload)


async def submit_tool_response(thread_id: str, run_id: str, tool_call_id: str, message: str) -> None:
    payload = {
        "threadId": thread_id,
        "runId": run_id,
        "messages": [],
        "frontendTools": [],
        "context": [],
        "state": {
            "toolCallId": tool_call_id,
            "result": {"message": message}
        }
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{BASE_URL}/api/ag-ui/run", json=payload, timeout=30.0)
        response.raise_for_status()


async def run_ui_tool_with_response(
    frontend_tools: List[Dict[str, Any]],
    user_prompt: str,
    response_message: str,
) -> List[Dict[str, Any]]:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    payload = {
        "threadId": thread_id,
        "runId": run_id,
        "messages": [
            {
                "id": str(uuid.uuid4()),
                "role": "user",
                "content": user_prompt
            }
        ],
        "frontendTools": frontend_tools,
        "context": [],
        "maxToolCalls": 4
    }
    events: List[Dict[str, Any]] = []
    responded = False
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", f"{BASE_URL}/api/ag-ui/run", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if not data_str:
                    continue
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                events.append(event)

                if (
                    not responded
                    and event.get("type") == "TOOL_CALL_START"
                    and event.get("awaitsResponse")
                ):
                    tool_call_id = event.get("toolCallId")
                    if tool_call_id:
                        await submit_tool_response(thread_id, run_id, tool_call_id, response_message)
                        responded = True
    return events


async def resume_execution(
    execution_guid: str,
    state_guid: str,
    tool_call_id: str,
    message: str,
) -> Dict[str, Any]:
    payload = {
        "executionGuid": execution_guid,
        "stateGuid": state_guid,
        "toolCallId": tool_call_id,
        "result": {"message": message},
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{BASE_URL}/v1/agent/resumeRun", json=payload, timeout=60.0)
        response.raise_for_status()
        return response.json()


async def fetch_execution_status(execution_guid: str) -> Dict[str, Any]:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/v1/agent/execution/{execution_guid}", timeout=30.0)
        response.raise_for_status()
        return response.json()


def assert_tool_presence(names: List[str], tool_name: str) -> None:
    if tool_name not in names:
        raise AssertionError(f"Expected tool '{tool_name}' not found in tool list")


async def main():
    print(f"Connecting to jb-ai-orchestrator at {BASE_URL}")

    tools_before, names_before = await fetch_tools()
    print(f"Initial tool count: {len(names_before)}")

    # 1+2. Push first UI tool list and verify merge
    first_ui_tools = [
        _build_frontend_tool("Weather-Display", "Render the weather for a city"),
        _build_frontend_tool("Announcement-Banner", "Show an announcement to the user")
    ]
    await bind_ui_tools(first_ui_tools)

    tools_after_first, names_after_first = await fetch_tools()
    print(f"Tool count after first bind: {len(names_after_first)}")
    assert_tool_presence(names_after_first, _prefixed_name("Weather-Display"))
    assert_tool_presence(names_after_first, _prefixed_name("Announcement-Banner"))

    # 4+5. Replace UI tools and ensure old definitions disappear
    second_ui_tools = [
        _build_frontend_tool("Weather-Printer", "Print precise weather updates"),
        _build_frontend_tool("City-Alerts", "Show alerts for a city")
    ]
    await bind_ui_tools(second_ui_tools)

    _, names_after_second = await fetch_tools()
    print(f"Tool count after replacement: {len(names_after_second)}")
    assert_tool_presence(names_after_second, _prefixed_name("Weather-Printer"))
    if _prefixed_name("Weather-Display") in names_after_second:
        raise AssertionError("Old UI tool still present after refresh")

    # Remove one UI tool (leave only Weather-Printer) to ensure the cache shrinks.
    third_ui_tools = [
        _build_frontend_tool("Weather-Printer", "Print precise weather updates")
    ]
    await bind_ui_tools(third_ui_tools)

    _, names_after_third = await fetch_tools()
    print(f"Tool count after removal: {len(names_after_third)}")
    assert_tool_presence(names_after_third, _prefixed_name("Weather-Printer"))
    if _prefixed_name("City-Alerts") in names_after_third:
        raise AssertionError("Removed UI tool still present after shrinking list")

    # 6. Execute run and ensure UI tool call is emitted
    print("Triggering AG-UI run that should call Weather-Printer...")
    events = await run_with_ui_tool(
        third_ui_tools,
        "Show me the weather in NY now using your Weather-Printer tool and print the result to the UI."
    )

    tool_call_name = "Weather-Printer"
    tool_call_events = [
        event for event in events
        if event.get("type") == "TOOL_CALL_START" and event.get("toolCallName") == tool_call_name
    ]
    tool_call_args = [
        event for event in events
        if event.get("type") == "TOOL_CALL_ARGS" and event.get("toolCallId") in {tc.get("toolCallId") for tc in tool_call_events}
    ]
    if not tool_call_events:
        raise AssertionError(
            "Expected AG-UI tool call did not occur. Ensure the configured LLM supports tool calling."
        )

    print("AG-UI tool call detected:")
    print(json.dumps(tool_call_events[0], indent=2))
    if tool_call_args:
        print("AG-UI tool call arguments:")
        print(json.dumps(tool_call_args[0], indent=2))

    # 7. Verify /v1/agent/executeRequest publishes AG-UI tool calls globally.
    print("Verifying executeRequest emits AG-UI events via /api/ag-ui/events...")
    global_listener = asyncio.create_task(stream_global_agui_events(tool_call_name))
    await asyncio.sleep(0.25)  # allow SSE subscription to establish

    execute_response = await execute_agent_request(
        task="Get the weather in New York now and display it in the UI.",
        tools=([MCP_TOOL] if MCP_TOOL else []) + [_prefixed_name("Weather-Printer")],
        max_tool_calls=6
    )
    if not execute_response.get("success"):
        raise AssertionError(f"/v1/agent/executeRequest failed: {execute_response}")

    global_start_event, global_args_event = await asyncio.wait_for(global_listener, timeout=30.0)
    print("Global AG-UI tool call detected via /api/ag-ui/events:")
    print(json.dumps(global_start_event, indent=2))
    if global_args_event:
        print("Global AG-UI tool call arguments:")
        print(json.dumps(global_args_event, indent=2))

    # 8. Validate awaitsResponse lifecycle end-to-end.
    print("Validating awaitsResponse flow...")
    interactive_tool = _build_frontend_tool(
        "Weather-Gate",
        "Collect weather context and wait for a UI confirmation before continuing"
    )
    interactive_tool["extensions"] = {"awaitsResponse": True}
    await bind_ui_tools([interactive_tool])

    awaited_events = await run_ui_tool_with_response(
        [interactive_tool],
        "Invoke Weather-Gate for New York, wait for the UI response, then summarize.",
        "UI acknowledged the Weather-Gate instructions for New York.",
    )

    awaited_tool_id = None
    assistant_messages: Dict[str, List[str]] = {}
    assistant_order: List[str] = []
    for event in awaited_events:
        if event.get("type") == "TOOL_CALL_START":
            awaited_tool_id = event.get("toolCallId")
            print("Await-response tool call issued:")
            print(json.dumps(event, indent=2))
        elif event.get("type") == "TOOL_CALL_ARGS" and event.get("toolCallId") == awaited_tool_id:
            print("Await-response tool arguments:")
            print(json.dumps(event, indent=2))
        elif event.get("type") == "TOOL_CALL_RESULT":
            print("Await-response tool result:")
            print(json.dumps(event, indent=2))
        elif event.get("type") == "TOOL_CALL_END" and event.get("toolCallId") == awaited_tool_id:
            print("Await-response tool call completed.")
        elif event.get("type") == "TEXT_MESSAGE_START":
            msg_id = event.get("messageId")
            assistant_messages[msg_id] = []
            assistant_order.append(msg_id)
        elif event.get("type") == "TEXT_MESSAGE_CONTENT":
            msg_id = event.get("messageId")
            if msg_id in assistant_messages:
                assistant_messages[msg_id].append(event.get("delta", ""))
        elif event.get("type") == "TEXT_MESSAGE_END":
            msg_id = event.get("messageId")
            if msg_id in assistant_messages:
                content = "".join(assistant_messages[msg_id]).strip()
                if content:
                    print("Assistant final message fragment:")
                    print(content)
                del assistant_messages[msg_id]
        elif event.get("type") == "RUN_FINISHED":
            print("Await-response run finished payload:")
            print(json.dumps(event, indent=2))

    if not any(event.get("type") == "TOOL_CALL_RESULT" for event in awaited_events):
        raise AssertionError("Expected TOOL_CALL_RESULT after submitting the awaitsResponse reply.")
    if not any(event.get("type") == "RUN_FINISHED" for event in awaited_events):
        raise AssertionError("Run did not finish after the waits-for-response UI payload.")

    print("Validating executeRequest pause/resume flow via /v1/agent/resumeRun...")
    await bind_ui_tools([interactive_tool])
    pause_response = await execute_agent_request(
        task="Use Weather-Gate to confirm with the UI before finishing the NYC weather update, then summarize.",
        tools=([MCP_TOOL] if MCP_TOOL else []) + [_prefixed_name("Weather-Gate")],
        max_tool_calls=6,
    )
    if not pause_response.get("awaitsResponse"):
        raise AssertionError("Expected executeRequest to pause with awaitsResponse=true")

    execution_guid = pause_response.get("executionGuid")
    state_guid = pause_response.get("stateGuid")
    agui_calls = pause_response.get("agui_tool_calls") or []
    if not execution_guid or not state_guid or not agui_calls:
        raise AssertionError("executeRequest did not return execution/state metadata")
    tool_call_id = agui_calls[0].get("tool_call_id")
    print("Paused execution metadata:")
    print(
        json.dumps(
            {
                "executionGuid": execution_guid,
                "stateGuid": state_guid,
                "toolCallId": tool_call_id,
            },
            indent=2,
        )
    )

    resume_message = "UI approved the Weather-Gate instructions for New York."
    resume_response = await resume_execution(execution_guid, state_guid, tool_call_id, resume_message)
    print("Resume response payload:")
    print(json.dumps(resume_response, indent=2))
    if resume_response.get("awaitsResponse"):
        raise AssertionError("Resume endpoint returned awaitsResponse=true unexpectedly")

    status_payload = await fetch_execution_status(execution_guid)
    print("Execution status payload:")
    print(json.dumps(status_payload, indent=2))
    if status_payload.get("status") != "completed":
        raise AssertionError(f"Execution status not completed: {status_payload}")
    if status_payload.get("awaitsResponse"):
        raise AssertionError("Execution status still awaiting response after resume")

    print("AG-UI flow smoke check completed successfully")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"AG-UI smoke check failed: {exc}")
        sys.exit(1)
