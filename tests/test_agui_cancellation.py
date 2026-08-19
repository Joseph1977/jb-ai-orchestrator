# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Cancellation normalization for AG-UI resume helpers."""

from __future__ import annotations

import json

from ag_ui.core.types import ResumeEntry

from app.services.agui_interrupt import (
    resume_entry_to_tool_response,
    tool_response_content,
)


def test_resume_entry_cancelled_default_error():
    entry = ResumeEntry(interruptId="call_x", status="cancelled", payload=None)
    resp = resume_entry_to_tool_response(entry)
    assert resp["error"] == "cancelled"
    assert resp["cancelled"] is True
    assert "result" not in resp


def test_resume_entry_cancelled_scalar_becomes_error_not_result():
    entry = ResumeEntry(interruptId="call_x", status="cancelled", payload="user dismissed")
    resp = resume_entry_to_tool_response(entry)
    assert resp["error"] == "user dismissed"
    assert "result" not in resp


def test_resume_entry_cancelled_dict_with_result_ignored():
    entry = ResumeEntry(
        interruptId="call_x",
        status="cancelled",
        payload={"result": {"ignored": True}, "error": "custom"},
    )
    resp = resume_entry_to_tool_response(entry)
    assert resp["error"] == "custom"
    assert "result" not in resp


def test_tool_response_content_cancelled():
    assert tool_response_content({"status": "cancelled"}) == {
        "error": "cancelled",
        "cancelled": True,
    }
    assert tool_response_content({"cancelled": True, "error": "nope"}) == {
        "error": "nope",
        "cancelled": True,
    }
