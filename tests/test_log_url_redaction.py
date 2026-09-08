# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Logs must not reproduce credentials carried in configured URLs.

An endpoint is configuration rather than a secret, but operators routinely
embed a credential in one -- userinfo before the host, or a token in the query
string -- and logs are copied into issue reports and shipped to aggregators.
These tests pin the sanitizing so a future edit cannot quietly restore an
f-string that interpolates a raw URL.
"""

import json
import logging

import pytest

from app.config import Config
from app.utils.config_logging import (
    describe_malformed_value,
    redact_url,
    safe_config_snapshot,
)

# Values that must never appear in a log line, paired with a URL carrying them.
LEAKY_URLS = [
    ("https://user:hunter2@mcp.example.com/sse", "hunter2"),
    ("https://mcp.example.com/sse?token=abcd1234secret", "abcd1234secret"),
    ("https://mcp.example.com/sse#access_token=fragmentsecret", "fragmentsecret"),
    ("https://svc:pw@mcp.example.com:8443/sse?api_key=querysecret", "querysecret"),
]


@pytest.mark.parametrize("url,secret", LEAKY_URLS)
def test_redact_url_drops_the_secret(url, secret):
    redacted = redact_url(url)

    assert secret not in redacted
    assert "mcp.example.com" in redacted, "the host is what makes the line useful"


def test_redact_url_keeps_the_diagnosable_parts():
    assert redact_url("https://mcp.example.com:8443/sse") == "https://mcp.example.com:8443/sse"


def test_redact_url_reports_what_it_removed():
    redacted = redact_url("https://user:pw@mcp.example.com/sse?token=x")

    assert "credentials removed" in redacted
    assert "query removed" in redacted


@pytest.mark.parametrize("value", ["", None, "   "])
def test_redact_url_handles_absent_values(value):
    assert redact_url(value) == "(unset)"


@pytest.mark.parametrize("value", ["not a url", "sk-live-abcdef123456", "host:port/path"])
def test_redact_url_never_echoes_an_unparsable_value(value):
    """A value that is not a URL is usually not the thing we assumed it was."""
    assert redact_url(value) == "(unparsable url)"


def test_describe_malformed_value_reports_shape_only():
    described = describe_malformed_value('{"url": "https://u:pw@h/s?token=leak"')

    assert "leak" not in described
    assert "pw" not in described
    assert "characters" in described


def test_config_snapshot_redacts_the_litellm_url(monkeypatch):
    monkeypatch.setattr(Config, "LITELLM_BASE_URL", "https://key:sneaky@llm.example.com/v1")

    snapshot = safe_config_snapshot(Config)

    assert "sneaky" not in json.dumps(snapshot)


def test_malformed_mcp_server_urls_are_not_logged_verbatim(monkeypatch, caplog):
    """The malformed value is the one most likely to hold a mistyped secret."""
    monkeypatch.setenv("MCP_SERVER_URLS", '[{"url": "https://u:leakedpw@h/sse"')

    with caplog.at_level(logging.WARNING):
        Config._parse_mcp_servers()

    assert "leakedpw" not in caplog.text
    assert "MCP_SERVER_URLS" in caplog.text, "the operator still needs to find it"


def test_malformed_numbered_mcp_url_is_not_logged_verbatim(monkeypatch, caplog):
    monkeypatch.delenv("MCP_SERVER_URLS", raising=False)
    monkeypatch.setenv("MCP_SERVER_URL_1", '{"name": "a", "url": "https://u:leakedpw@h"')

    with caplog.at_level(logging.WARNING):
        Config._parse_mcp_servers()

    assert "leakedpw" not in caplog.text
    assert "MCP_SERVER_URL_1" in caplog.text


def test_validate_config_does_not_log_credentials_in_mcp_urls(monkeypatch, caplog):
    """Drives the real listing path: validate_config() logs each server."""
    monkeypatch.setenv(
        "MCP_SERVER_URLS",
        json.dumps([{"name": "general", "url": "https://u:listedpw@mcp.example.com/sse?t=qs"}]),
    )
    monkeypatch.setattr(Config, "DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setattr(Config, "LITELLM_API_KEY", "sk-test")
    monkeypatch.setattr(Config, "LITELLM_BASE_URL", "https://k:basepw@llm.example.com/v1")

    with caplog.at_level(logging.INFO):
        Config.validate_config()

    assert "listedpw" not in caplog.text
    assert "qs" not in caplog.text
    assert "mcp.example.com" in caplog.text, "the host is what makes the line useful"


def test_service_construction_does_not_log_credentials(caplog):
    """Drives MCPAgentService.__init__, which lists servers and the gateway."""
    from app.services.mcp_agent_service import MCPAgentService

    with caplog.at_level(logging.INFO):
        MCPAgentService(
            mcp_server_configs=[
                {"name": "general", "url": "https://u:ctorpw@mcp.example.com/sse"}
            ],
            litellm_base_url="https://key:gatewaypw@llm.example.com/v1",
            litellm_api_key="sk-test",
        )

    assert "ctorpw" not in caplog.text
    assert "gatewaypw" not in caplog.text
    assert "mcp.example.com" in caplog.text


async def test_connect_failure_does_not_log_credentials(caplog):
    """The error path is the one most likely to be pasted into a bug report."""
    from app.services.mcp_agent_service import MCPAgentService

    service = MCPAgentService(
        mcp_server_configs=[],
        litellm_base_url="http://llm.example.com",
        litellm_api_key="sk-test",
    )
    caplog.clear()

    # An unroutable host makes the real connect attempt fail, so the failure
    # log is produced by the code under test rather than by a patched stub.
    with caplog.at_level(logging.INFO):
        tools = await service.fetch_mcp_tools_from_server(
            {"name": "general", "url": "http://u:failpw@127.0.0.1:1/sse?token=failqs"}
        )

    assert tools == []
    assert "failpw" not in caplog.text
    assert "failqs" not in caplog.text
    assert "127.0.0.1" in caplog.text, "the operator still needs to know which server"


def _url_bearing_log_arguments(source: str):
    """Yield (line, expression) for URL-valued arguments passed to a logger.

    Parsed rather than pattern-matched, so it judges the values a call
    substitutes and not the wording of its message. A message that merely
    mentions MCP_SERVER_URLS is fine; passing `server_url` is what matters.
    Both f-string interpolations and %-style arguments are covered.
    """
    import ast
    import re

    # A name whose value is a URL. `len(...)` of a server list is not one.
    url_name = re.compile(r"\b\w*(?:url|urls|base_url|endpoint)\b", re.I)

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if not isinstance(callee, ast.Attribute):
            continue
        if callee.attr not in {"info", "warning", "error", "debug", "exception"}:
            continue
        if not (isinstance(callee.value, ast.Name) and "logger" in callee.value.id):
            continue

        # Every expression whose value is substituted into the message.
        substituted = []
        for argument in node.args + [keyword.value for keyword in node.keywords]:
            if isinstance(argument, ast.JoinedStr):
                substituted += [
                    piece.value
                    for piece in argument.values
                    if isinstance(piece, ast.FormattedValue)
                ]
            elif not isinstance(argument, ast.Constant):
                substituted.append(argument)

        for expression in substituted:
            text = ast.get_source_segment(source, expression) or ""
            if "redact_url" in text or text.startswith("len("):
                continue
            if url_name.search(text):
                yield expression.lineno, text


def test_no_log_call_passes_a_raw_url():
    """Supplementary to the execution tests above.

    Those cover the paths that exist today; this catches a new call site before
    any test exercises it.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "app"
    offenders = [
        f"{path.relative_to(src)}:{line}: {expression}"
        for path in sorted(src.rglob("*.py"))
        for line, expression in _url_bearing_log_arguments(
            path.read_text(encoding="utf-8")
        )
    ]

    assert not offenders, (
        "pass URLs through redact_url() before logging them:\n" + "\n".join(offenders)
    )


def test_the_guard_detects_both_leak_styles():
    """A guard that silently matches nothing would pass for the wrong reason."""
    fstring_leak = 'logger.info(f"connecting to {server_url}")'
    percent_leak = 'logger.info("connecting to %s", server_url)'
    message_only = 'logger.warning("Invalid JSON in MCP_SERVER_URLS; expected an array")'
    redacted = 'logger.info("connecting to %s", redact_url(server_url))'

    assert list(_url_bearing_log_arguments(fstring_leak)), "missed an f-string leak"
    assert list(_url_bearing_log_arguments(percent_leak)), "missed a %-style leak"
    assert not list(_url_bearing_log_arguments(message_only)), "flagged message wording"
    assert not list(_url_bearing_log_arguments(redacted)), "flagged a redacted call"
