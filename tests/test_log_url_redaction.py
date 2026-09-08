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


def test_configured_mcp_urls_are_redacted_when_listed(monkeypatch, caplog):
    monkeypatch.setenv(
        "MCP_SERVER_URLS",
        json.dumps([{"name": "general", "url": "https://u:listedpw@mcp.example.com/sse"}]),
    )

    with caplog.at_level(logging.INFO):
        Config._parse_mcp_servers()
        for server in Config.MCP_SERVER_URLS:
            logging.getLogger(__name__).info(
                "  - %s: %s", server["name"], redact_url(server["url"])
            )

    assert "listedpw" not in caplog.text
    assert "mcp.example.com" in caplog.text


def test_no_source_line_interpolates_a_raw_url_into_a_log():
    """Guards the pattern, not just the four call sites fixed today."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "app"
    offenders = []
    # An f-string log call substituting something URL-shaped without redacting.
    # `len(...)` is excluded: counting the configured servers reveals nothing.
    pattern = re.compile(
        r"logger\.(?:info|warning|error|debug|exception)\(\s*f?\"[^\"]*\{(?!len\()[^}]*"
        r"(?:url|URL|base_url|endpoint)[^}]*\}",
    )
    for path in src.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and "redact_url" not in line:
                offenders.append(f"{path.relative_to(src)}:{number}: {line.strip()}")

    assert not offenders, "log a URL through redact_url():\n" + "\n".join(offenders)
