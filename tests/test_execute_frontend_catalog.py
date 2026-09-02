# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Execute names the registered interaction tools in its system prompt.

The tool schemas were passed to the model while the authoritative prompt stayed
silent about them, unlike the AG-UI relay. A workflow step that says "invoke a
matching interaction tool when available" then had nothing in the prompt to
match against, and the model answered a multiple-choice question in prose. The
catalog raises adherence; it cannot force a tool call, so the browser check
still has to confirm the call itself.
"""

import pytest

from app.controllers.orchestrator_controller import _build_execute_system_prompt
from app.services.agui_messages import (
    FRONTEND_INTERACTION_GUIDANCE,
    FRONTEND_TOOL_LIST_PREFIX,
)


@pytest.fixture(autouse=True)
def _harness(monkeypatch):
    monkeypatch.setattr(
        "app.controllers.orchestrator_controller.collect_manifest",
        lambda *a, **k: object(),
    )
    monkeypatch.setattr(
        "app.controllers.orchestrator_controller.render_system_prompt",
        lambda *a, **k: "HARNESS PROMPT",
    )


def _prompt(frontend_tools):
    return _build_execute_system_prompt(
        {"systemContext": "base"},
        workspace_path="/tmp/ws",
        orchestration_type=None,
        frontend_tools=frontend_tools,
    )


def test_registered_tools_are_named_in_the_prompt():
    prompt = _prompt(
        [
            {"name": "Ask-Choice", "description": "single choice"},
            {"name": "Ask-Text", "description": "free text"},
        ]
    )

    assert "HARNESS PROMPT" in prompt
    assert FRONTEND_TOOL_LIST_PREFIX in prompt
    assert FRONTEND_INTERACTION_GUIDANCE in prompt
    assert "Ask-Choice" in prompt
    assert "Ask-Text" in prompt


def test_blank_and_duplicate_names_are_ignored():
    prompt = _prompt(
        [
            {"name": "  Ask-Choice  "},
            {"name": ""},
            {"description": "no name"},
            {"name": "Ask-Choice"},
        ]
    )

    assert prompt.count('"Ask-Choice"') == 1


def test_no_frontend_tools_adds_no_interaction_sections():
    for frontend_tools in (None, []):
        prompt = _prompt(frontend_tools)
        assert "HARNESS PROMPT" in prompt
        assert FRONTEND_TOOL_LIST_PREFIX not in prompt
        assert FRONTEND_INTERACTION_GUIDANCE not in prompt


def test_nameless_tools_still_get_the_guidance_without_a_catalog():
    """Registered but unnamed tools are still exposed to the model, so the
    guidance applies; there is simply nothing to list, as on the AG-UI path."""
    prompt = _prompt([{"description": "no name"}])

    assert FRONTEND_INTERACTION_GUIDANCE in prompt
    assert FRONTEND_TOOL_LIST_PREFIX not in prompt
