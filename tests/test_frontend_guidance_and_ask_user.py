# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Frontend guidance and ask_user_local suppression (metadata-based)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ag_ui.core.types import SystemMessage

from app.services.agui_messages import (
    FRONTEND_INTERACTION_GUIDANCE,
    build_authoritative_system_content,
    compose_system_messages,
)
from app.services.agui_event_service import agui_event_service
from app.services.agui_service import agui_service
from app.services.local_tool_provider import LocalToolContext, local_tool_provider
from app.services.tool_hub import ToolExecutionHub


class FakeMCPService:
    litellm_request_timeout_in_sec = 30

    def __init__(self, responses=None):
        self._responses = list(responses or [])
        self.calls = []

    async def fetch_mcp_tools(self):
        return []

    async def call_litellm(self, messages, model="gpt", tools=None, **kwargs):
        self.calls.append({"tools": tools})
        if self._responses:
            return self._responses.pop(0)
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {},
        }


def test_frontend_guidance_only_when_tools_present():
    without = build_authoritative_system_content(
        harness_prompt="Harness",
        incoming_messages=[SystemMessage(id="s1", content="UI rules")],
        has_frontend_tools=False,
    )
    assert FRONTEND_INTERACTION_GUIDANCE not in (without or "")

    with_tools = build_authoritative_system_content(
        harness_prompt="Harness",
        incoming_messages=[SystemMessage(id="s1", content="UI rules")],
        has_frontend_tools=True,
    )
    assert FRONTEND_INTERACTION_GUIDANCE in (with_tools or "")


def test_skips_guidance_when_client_already_covers_it():
    contract = "Client-provided frontend interaction tools are available for this run."
    out = compose_system_messages(
        harness_prompt="Harness",
        incoming_messages=[SystemMessage(id="s1", content=contract)],
        has_frontend_tools=True,
    )
    assert out[0]["content"].count("frontend interaction tools are available") == 1


def test_ask_user_suppressed_when_frontend_awaits_response(tmp_path):
    async def run():
        ws = tmp_path / "ws"
        ws.mkdir()
        fake = FakeMCPService()
        hub = ToolExecutionHub(fake, agui_service, agui_event_service)
        frontend = [{
            "name": "Interactive-Prompt",
            "description": "Ask the user",
            "parameters": {"type": "object", "properties": {}},
            "extensions": {"awaitsResponse": True},
        }]
        await hub.process_request(
            request="go",
            initial_messages=[{"role": "user", "content": "go"}],
            include_agui_tools=True,
            frontend_tools=frontend,
            local_context=LocalToolContext(workspace_path=str(ws), in_place=True),
        )
        names = [
            t["function"]["name"]
            for t in (fake.calls[0]["tools"] or [])
            if t.get("function")
        ]
        ask_name = local_tool_provider.prefixed("ask_user")
        assert ask_name not in names

    asyncio.run(run())


def test_ask_user_retained_without_interactive_frontend_tools(tmp_path: Path):
    async def run():
        agui_service.refresh_frontend_tools([])
        ws = tmp_path / "ws"
        ws.mkdir()
        fake = FakeMCPService()
        hub = ToolExecutionHub(fake, agui_service, agui_event_service)
        await hub.process_request(
            request="go",
            initial_messages=[{"role": "user", "content": "go"}],
            include_agui_tools=False,
            frontend_tools=[],
            local_context=LocalToolContext(workspace_path=str(ws), in_place=True),
        )
        names = [
            t["function"]["name"]
            for t in (fake.calls[0]["tools"] or [])
            if t.get("function")
        ]
        ask_name = local_tool_provider.prefixed("ask_user")
        assert ask_name in names

    asyncio.run(run())


def test_no_hardcoded_frontend_tool_names_in_main_source():
    root = Path(__file__).resolve().parents[1] / "src" / "app"
    forbidden = ("Request-Approval", "Ask-Text", "Ask-User", "Show-Message", "HOOK_ASK_UI")
    hits: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                hits.append(f"{path.relative_to(root)}: {token}")
    assert hits == []
