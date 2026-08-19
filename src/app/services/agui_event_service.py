# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Set

from ag_ui.core.events import (
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)


@dataclass
class AGUIEventEnvelope:
    event: Any
    thread_id: Optional[str] = None
    run_id: Optional[str] = None


def build_tool_call_events(
    tool_call_id: str,
    tool_name: str,
    arguments: dict,
    awaits_response: bool = False,
) -> List[Any]:
    """Create the standard AG-UI TOOL_CALL_* events for a single invocation."""
    start = ToolCallStartEvent(tool_call_id=tool_call_id, tool_call_name=tool_name)
    if awaits_response:
        setattr(start, "awaitsResponse", True)
    events: List[Any] = [
        start,
        ToolCallArgsEvent(tool_call_id=tool_call_id, delta=json.dumps(arguments or {})),
        ToolCallEndEvent(tool_call_id=tool_call_id),
    ]
    return events


class AGUIEventService:
    """Broadcast AG-UI events (e.g., tool calls) to SSE subscribers."""

    def __init__(self) -> None:
        self._subscribers: Set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def publish_events(
        self,
        events: Iterable[Any],
        *,
        thread_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> None:
        events_list = list(events)
        if not events_list:
            return

        async with self._lock:
            subscribers = list(self._subscribers)

        for queue in subscribers:
            for event in events_list:
                envelope = AGUIEventEnvelope(event=event, thread_id=thread_id, run_id=run_id)
                queue.put_nowait(envelope)

    async def publish_tool_call(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: dict,
        *,
        thread_id: Optional[str] = None,
        run_id: Optional[str] = None,
        awaits_response: bool = False,
    ) -> None:
        await self.publish_events(
            build_tool_call_events(tool_call_id, tool_name, arguments, awaits_response),
            thread_id=thread_id,
            run_id=run_id,
        )


agui_event_service = AGUIEventService()
