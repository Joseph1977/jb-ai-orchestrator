"""Resume option precedence and request schema tests."""

import pytest

from app.controllers.ag_ui_controller import AGUIRunRequest
from app.models.requests import OrchestratorResumeInput, ResumeRunInput
from app.services.resume_options import (
    DEFAULT_MODEL,
    resolve_resume_max_tool_calls,
    resolve_resume_model,
)


@pytest.mark.parametrize(
    ("requested", "snapshot", "segment", "expected"),
    [
        ("request-model", "snapshot-model", "segment-model", "request-model"),
        (None, "snapshot-model", "segment-model", "snapshot-model"),
        (None, None, "segment-model", "segment-model"),
        (None, None, None, DEFAULT_MODEL),
    ],
)
def test_resume_model_precedence(requested, snapshot, segment, expected):
    assert resolve_resume_model(requested, snapshot, segment) == expected


@pytest.mark.parametrize(
    ("requested", "snapshot", "segment", "expected"),
    [
        (11, 7, 3, 11),
        (None, 7, 3, 7),
        (None, None, 3, 3),
        (None, None, None, None),
        (0, 7, 3, 0),
    ],
)
def test_resume_budget_precedence_preserves_explicit_zero(
    requested, snapshot, segment, expected
):
    assert resolve_resume_max_tool_calls(requested, snapshot, segment) == expected


def test_resume_request_schemas_accept_overrides():
    orchestrator = OrchestratorResumeInput.model_validate(
        {
            "orchestratorGuid": "2f4eb4a4-f1d1-4f42-9133-f089d15b36d4",
            "stateGuid": "0dc45a15-a222-4ac1-8316-72c2d8d3cb27",
            "toolCallId": "call-a",
            "model": "request-model",
            "maxToolCalls": 0,
        }
    )
    legacy = ResumeRunInput.model_validate(
        {
            "executionGuid": "2f4eb4a4-f1d1-4f42-9133-f089d15b36d4",
            "stateGuid": "0dc45a15-a222-4ac1-8316-72c2d8d3cb27",
            "toolCallId": "call-a",
            "model": "request-model",
            "max_tool_calls": 0,
        }
    )
    agui = AGUIRunRequest.model_validate(
        {
            "threadId": "thread-a",
            "model": "request-model",
            "maxToolCalls": 0,
        }
    )

    assert orchestrator.model == legacy.model == agui.model == "request-model"
    assert orchestrator.maxToolCalls == legacy.max_tool_calls == agui.max_tool_calls == 0


def test_agui_model_is_optional_in_openapi_schema():
    model_schema = AGUIRunRequest.model_json_schema()["properties"]["model"]

    assert model_schema["default"] is None
