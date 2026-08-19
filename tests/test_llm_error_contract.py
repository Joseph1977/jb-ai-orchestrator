# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import uuid

import httpx

from app.controllers.orchestrator_controller import _run_response
from app.models.execution_models import ExecutionStatus
from app.models.requests import ExecuteRequestResponse
from app.services.mcp_agent_service import (
    LLMUpstreamError,
    _llm_upstream_error,
    _safe_litellm_exception,
)


def test_provider_errors_follow_http_status_with_safe_messages():
    quota = _llm_upstream_error(402)
    rate_limit = _llm_upstream_error(429)
    auth = _llm_upstream_error(401)
    unavailable = _llm_upstream_error(500)

    assert quota.code == "QUOTA"
    assert rate_limit.code == "RATE_LIMIT"
    assert auth.code == "AUTH"
    assert unavailable.code == "UNAVAILABLE"
    assert all(
        "secret" not in str(error)
        for error in (quota, rate_limit, auth, unavailable)
    )


def test_only_transport_errors_receive_unavailable_code():
    transport = _safe_litellm_exception(httpx.ConnectError("token=secret"))
    programming = _safe_litellm_exception(ValueError("token=secret"))

    assert isinstance(transport, LLMUpstreamError)
    assert transport.code == "UNAVAILABLE"
    assert str(transport) == "Model service is unavailable"
    assert not isinstance(programming, LLMUpstreamError)
    assert str(programming) == "Model request failed"


def test_execute_response_contract_includes_safe_error_code():
    payload = _run_response(
        execution_id=uuid.uuid4(),
        status=ExecutionStatus.FAILED,
        result={
            "success": False,
            "error": "Model service is unavailable",
            "error_code": "UNAVAILABLE",
        },
    )

    parsed = ExecuteRequestResponse.model_validate(payload)
    assert parsed.errorCode == "UNAVAILABLE"
