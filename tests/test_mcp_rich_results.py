# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""MCP rich result normalization tests."""

from app.services.mcp_agent_service import MCPAgentService


class _Block:
    def __init__(self, text):
        self.text = text

    def model_dump(self, **kwargs):
        return {"type": "text", "text": self.text}


class _Result:
    content = [_Block("hello")]
    structuredContent = {"count": 1}
    isError = False
    meta = {"source": "test"}


def test_normalize_mcp_result_preserves_fields():
    out = MCPAgentService._normalize_mcp_result(_Result())
    assert out["result"] == "hello"
    assert out["structuredContent"] == {"count": 1}
    assert out["isError"] is False
    assert out["metadata"] == {"source": "test"}
    assert len(out["content"]) == 1


def test_normalize_mcp_result_marks_errors():
    err = _Result()
    err.isError = True
    out = MCPAgentService._normalize_mcp_result(err)
    assert out["isError"] is True
    assert "error" in out
