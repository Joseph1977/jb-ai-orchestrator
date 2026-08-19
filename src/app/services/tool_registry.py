# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Provider-aware tool registry for LiteLLM routing."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Set

from app.services.agui_service import AGUIToolRecord


class ToolProvider(str, Enum):
    MCP = "mcp"
    AGUI = "agui"
    LOCAL = "local"


@dataclass
class RegisteredTool:
    """One model-visible tool with routing metadata."""

    canonical_name: str
    provider: ToolProvider
    description: str = ""
    parameters: dict = field(default_factory=dict)
    # Provider-specific routing
    mcp_tool: Any = None
    agui_record: Optional[AGUIToolRecord] = None
    local_base_name: Optional[str] = None
    awaits_response: bool = False
    # Compatibility aliases (AGUI-*, server-prefixed MCP names, etc.)
    aliases: List[str] = field(default_factory=list)


def _collision_alias(base: str, provider: ToolProvider, server_or_ns: str) -> str:
    return f"{base}__{provider.value}__{server_or_ns}"


class ToolRegistry:
    """Register tools from multiple providers; expose canonical LiteLLM definitions."""

    def __init__(self) -> None:
        self._by_canonical: Dict[str, RegisteredTool] = {}
        self._alias_to_canonical: Dict[str, str] = {}

    def register(self, tool: RegisteredTool) -> None:
        self._by_canonical[tool.canonical_name] = tool
        self._alias_to_canonical[tool.canonical_name] = tool.canonical_name
        for alias in tool.aliases:
            if alias and alias not in self._alias_to_canonical:
                self._alias_to_canonical[alias] = tool.canonical_name

    def resolve(self, name: str) -> Optional[RegisteredTool]:
        canonical = self._alias_to_canonical.get(name)
        if canonical:
            return self._by_canonical.get(canonical)
        return self._by_canonical.get(name)

    def litellm_tools(self) -> List[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.canonical_name,
                    "description": t.description,
                    "parameters": t.parameters or {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            }
            for t in self._by_canonical.values()
        ]

    @property
    def tools(self) -> List[RegisteredTool]:
        return list(self._by_canonical.values())


def agui_exposed_names(records: Sequence[AGUIToolRecord]) -> Set[str]:
    """All LiteLLM-visible names that belong to AG-UI tools (canonical + legacy)."""
    names: set[str] = set()
    for rec in records:
        for candidate in (rec.name, rec.original_name, rec.prefixed_name):
            if candidate:
                names.add(candidate)
        prefixed = rec.prefixed_name or f"AGUI-{rec.original_name or rec.name}"
        names.add(prefixed)
    return names


def is_agui_litellm_name(name: str, records: Sequence[AGUIToolRecord]) -> bool:
    if not name:
        return False
    if name in agui_exposed_names(records):
        return True
    if name.startswith("AGUI-"):
        stripped = name[5:]
        return any((r.original_name or r.name) == stripped for r in records)
    return False


def mcp_tools_from_stored_litellm(
    litellm_tools: Sequence[dict],
    *,
    agui_records: Sequence[AGUIToolRecord],
    local_namespace: str,
    is_local_tool=None,
) -> List[Any]:
    """Reconstruct MCP tools from persisted litellm_tools, excluding AG-UI/local."""
    from app.services.mcp_agent_service import MCPTool

    is_local = is_local_tool or (lambda _n: False)
    local_suffix = f"_{local_namespace}"
    mcp_tools: list[MCPTool] = []
    for tool_def in litellm_tools:
        fn = tool_def.get("function") or {}
        name = fn.get("name", "")
        if not name:
            continue
        if name.endswith(local_suffix) or is_local(name):
            continue
        if is_agui_litellm_name(name, agui_records):
            continue
        mcp_tools.append(
            MCPTool(
                name=name,
                description=fn.get("description", ""),
                input_schema=fn.get("parameters") or {},
                server_url="",
                server_name="stored",
                original_name=name,
            )
        )
    return mcp_tools


def build_active_agui_map(registry: ToolRegistry) -> Dict[str, AGUIToolRecord]:
    """Map canonical AG-UI names and compatibility aliases to AGUIToolRecord."""
    out: dict[str, AGUIToolRecord] = {}
    for reg in registry.tools:
        if reg.provider != ToolProvider.AGUI or not reg.agui_record:
            continue
        out[reg.canonical_name] = reg.agui_record
        for alias in reg.aliases:
            if alias:
                out[alias] = reg.agui_record
        rec = reg.agui_record
        for candidate in (rec.name, rec.original_name, rec.prefixed_name):
            if candidate:
                out[candidate] = rec
    return out


def build_tool_registry(
    *,
    mcp_tools: Sequence[Any],
    agui_records: Sequence[AGUIToolRecord],
    local_litellm_tools: Sequence[dict],
    local_awaits: Optional[dict[str, bool]] = None,
    mcp_convert_fn=None,
) -> ToolRegistry:
    """Build registry from MCP, AG-UI, and local tool sources."""
    registry = ToolRegistry()
    name_counts: Dict[str, int] = {}
    local_awaits = local_awaits or {}

    # Count base-name collisions across all providers.
    for tool in mcp_tools:
        base = getattr(tool, "original_name", None) or getattr(tool, "name", "")
        name_counts[base] = name_counts.get(base, 0) + 1
    for rec in agui_records:
        name_counts[rec.original_name or rec.name] = name_counts.get(rec.original_name or rec.name, 0) + 1
    for lt in local_litellm_tools:
        fn = (lt.get("function") or {})
        base = fn.get("name", "").rsplit("_", 1)[0] if fn.get("name") else ""
        if base:
            name_counts[base] = name_counts.get(base, 0) + 1

    # MCP tools
    for tool in mcp_tools:
        base = tool.original_name
        canonical = tool.name
        if name_counts.get(base, 0) > 1:
            canonical = _collision_alias(base, ToolProvider.MCP, tool.server_name)
        reg = RegisteredTool(
            canonical_name=canonical,
            provider=ToolProvider.MCP,
            description=f"[{tool.server_name}] {tool.description}" if tool.server_name else tool.description,
            parameters=tool.input_schema or {},
            mcp_tool=tool,
            aliases=[tool.name] if tool.name != canonical else [],
        )
        registry.register(reg)

    # AG-UI tools
    for rec in agui_records:
        base = rec.original_name or rec.name
        prefixed = rec.prefixed_name or rec.name
        canonical = base
        if name_counts.get(base, 0) > 1:
            canonical = _collision_alias(base, ToolProvider.AGUI, "frontend")
        reg = RegisteredTool(
            canonical_name=canonical,
            provider=ToolProvider.AGUI,
            description=rec.description,
            parameters=rec.parameters or {},
            agui_record=rec,
            awaits_response=rec.awaits_response,
            aliases=[a for a in (prefixed, rec.name) if a and a != canonical],
        )
        registry.register(reg)

    # Local tools
    for lt in local_litellm_tools:
        fn = lt.get("function") or {}
        full_name = fn.get("name", "")
        if not full_name:
            continue
        parts = full_name.rsplit("_", 1)
        base = parts[0] if len(parts) == 2 else full_name
        ns = parts[1] if len(parts) == 2 else "local"
        canonical = full_name
        if name_counts.get(base, 0) > 1:
            canonical = _collision_alias(base, ToolProvider.LOCAL, ns)
        reg = RegisteredTool(
            canonical_name=canonical,
            provider=ToolProvider.LOCAL,
            description=fn.get("description", ""),
            parameters=fn.get("parameters") or {},
            local_base_name=full_name,
            awaits_response=local_awaits.get(full_name, False),
            aliases=[full_name] if full_name != canonical else [],
        )
        registry.register(reg)

    return registry
