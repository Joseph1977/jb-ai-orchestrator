# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Execute surfaces a root-budget failure instead of hiding it.

``except Exception: pass`` around harness discovery meant an over-budget root
playbook fell back to whatever eager context was already stored, so the segment
ran with instructions the author had since changed. That contradicts the
explicit-failure policy for root instructions. AG-UI's best-effort discovery is
a separate, documented decision and is unaffected.
"""

import logging

import pytest

from app.controllers.orchestrator_controller import _build_execute_system_prompt
from app.services.harness import RootInstructionError


def test_root_instruction_error_propagates(monkeypatch):
    def boom(*args, **kwargs):
        raise RootInstructionError("root instructions are too large")

    monkeypatch.setattr(
        "app.controllers.orchestrator_controller.collect_manifest", boom
    )

    with pytest.raises(RootInstructionError):
        _build_execute_system_prompt(
            {"systemContext": "stale base", "eagerContext": "stale eager"},
            workspace_path="/tmp/ws",
            orchestration_type=None,
        )


def test_other_discovery_failures_fall_back_and_are_logged(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("disk gremlins")

    monkeypatch.setattr(
        "app.controllers.orchestrator_controller.collect_manifest", boom
    )

    with caplog.at_level(logging.WARNING):
        prompt = _build_execute_system_prompt(
            {"systemContext": "base ctx", "eagerContext": "eager ctx"},
            workspace_path="/tmp/ws",
            orchestration_type=None,
        )

    assert "base ctx" in prompt
    assert "eager ctx" in prompt
    assert any("Harness discovery failed" in r.message for r in caplog.records)


def test_fallback_failure_records_a_traceback(monkeypatch, caplog):
    """A silent except made these failures invisible in production logs."""

    def boom(*args, **kwargs):
        raise RuntimeError("disk gremlins")

    monkeypatch.setattr(
        "app.controllers.orchestrator_controller.collect_manifest", boom
    )

    with caplog.at_level(logging.WARNING):
        _build_execute_system_prompt(
            {"systemContext": "base"},
            workspace_path="/tmp/ws",
            orchestration_type=None,
        )

    assert any(r.exc_info for r in caplog.records), "expected exc_info on the warning"


def test_successful_discovery_is_unaffected(monkeypatch):
    monkeypatch.setattr(
        "app.controllers.orchestrator_controller.collect_manifest",
        lambda *a, **k: object(),
    )
    monkeypatch.setattr(
        "app.controllers.orchestrator_controller.render_system_prompt",
        lambda *a, **k: "HARNESS PROMPT",
    )

    prompt = _build_execute_system_prompt(
        {"systemContext": "base"},
        workspace_path="/tmp/ws",
        orchestration_type=None,
    )

    assert "HARNESS PROMPT" in prompt
