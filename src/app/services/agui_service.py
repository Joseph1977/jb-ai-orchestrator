# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from dataclasses import dataclass
from threading import RLock
from typing import Dict, List, Optional, Sequence, Union


DEFAULT_PARAMETERS_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": []
}


@dataclass
class AGUIToolRecord:
    """Internal representation of an AG-UI tool."""
    name: str
    original_name: str
    description: str
    parameters: dict
    prefixed_name: Optional[str] = None
    awaits_response: bool = False

    def to_litellm_tool(self) -> dict:
        """Convert record to LiteLLM tool definition."""
        return {
            "type": "function",
            "function": {
                "name": self.prefixed_name or self.name,
                "description": self.description,
                "parameters": self.parameters or DEFAULT_PARAMETERS_SCHEMA
            }
        }


class AGUIService:
    """Stores the current set of AG-UI tools provided by the UI layer."""

    def __init__(self, prefix: str = "AGUI"):
        self._prefix = prefix
        self._tools: Dict[str, AGUIToolRecord] = {}
        self._lock = RLock()

    def build_records(self, frontend_tools: Sequence[Union[dict, object]]) -> List[AGUIToolRecord]:
        """Convert a frontend-tools payload into records WITHOUT mutating state.

        Use this for per-run/per-session tool scoping so concurrent runs (and
        multiple apps sharing one deployment) never clobber each other.
        """
        records: List[AGUIToolRecord] = []

        for tool in frontend_tools or []:
            tool_dict = self._coerce_tool(tool)
            original_name = tool_dict.get("name")
            if not original_name:
                continue

            prefixed_name = self._prefix_tool_name(original_name)
            description = tool_dict.get("description") or ""
            parameters = tool_dict.get("parameters") or DEFAULT_PARAMETERS_SCHEMA
            extensions = tool_dict.get("extensions") or {}
            awaits_response = bool(extensions.get("awaitsResponse", False))

            records.append(
                AGUIToolRecord(
                    name=original_name,
                    original_name=original_name,
                    description=description,
                    parameters=parameters,
                    prefixed_name=prefixed_name,
                    awaits_response=awaits_response,
                )
            )

        return records

    def refresh_frontend_tools(self, frontend_tools: Sequence[Union[dict, object]]) -> List[AGUIToolRecord]:
        """
        Replace the (legacy, process-global) cached AG-UI tools. Retained for the
        getTools listing and legacy bind-only runs; the agent loop prefers the
        per-run records passed explicitly to ``process_request``.
        """
        records = self.build_records(frontend_tools)
        with self._lock:
            self._tools = {record.prefixed_name or record.name: record for record in records}
        return records

    def get_litellm_tools(self) -> List[dict]:
        """Return LiteLLM-compatible tool definitions for all cached AG-UI tools."""
        with self._lock:
            return [record.to_litellm_tool() for record in self._tools.values()]

    def is_agui_tool(self, tool_name: str) -> bool:
        """Return True if the tool name belongs to an AG-UI tool."""
        with self._lock:
            return tool_name in self._tools

    def get_tool(self, tool_name: str) -> Optional[AGUIToolRecord]:
        """Retrieve metadata for a specific AG-UI tool."""
        with self._lock:
            return self._tools.get(tool_name)

    def list_tools(self) -> List[AGUIToolRecord]:
        """Return all cached AG-UI tools."""
        with self._lock:
            return list(self._tools.values())

    def handle_tool_call(self, tool_name: str, arguments: dict) -> dict:
        """
        Handle an AG-UI tool call.

        The actual execution happens on the UI side, so we simply acknowledge the call and
        include the serialized arguments for downstream consumers (e.g., SSE streaming).
        """
        arguments_json = json.dumps(arguments or {})
        return {
            "result": f"AG-UI tool '{tool_name}' call forwarded to client with args: {arguments_json}"
        }

    def _prefix_tool_name(self, original_name: str) -> str:
        return f"{self._prefix}-{original_name}"

    @staticmethod
    def _coerce_tool(tool: Union[dict, object]) -> Dict[str, object]:
        if isinstance(tool, dict):
            return tool

        if hasattr(tool, "model_dump"):
            return tool.model_dump(by_alias=True)

        if hasattr(tool, "__dict__"):
            return dict(tool.__dict__)

        return {}


# Global instance shared across the application
agui_service = AGUIService()
