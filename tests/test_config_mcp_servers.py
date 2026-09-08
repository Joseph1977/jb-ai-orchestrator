# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""MCP server configuration parsing.

These pin the behaviour of the two formats the parser actually implements —
a `MCP_SERVER_URLS` JSON array of named objects, and numbered
`MCP_SERVER_URL_{n}` JSON objects — plus the shapes it silently rejects.
"""

import os

import pytest

from app.config import Config


@pytest.fixture(autouse=True)
def clean_mcp_env(monkeypatch):
    """Isolate each test from ambient MCP settings and class-level state."""
    for key in list(os.environ):
        if key.startswith("MCP_SERVER"):
            monkeypatch.delenv(key, raising=False)
    original = Config.MCP_SERVER_URLS
    yield
    Config.MCP_SERVER_URLS = original


def test_named_json_array_is_parsed(monkeypatch):
    monkeypatch.setenv(
        "MCP_SERVER_URLS",
        '[{"name": "general", "url": "https://server1.com/mcp"},'
        ' {"name": "google", "url": "https://mcp.google.com/mcp"}]',
    )
    Config._parse_mcp_servers()
    assert Config.MCP_SERVER_URLS == [
        {"name": "general", "url": "https://server1.com/mcp"},
        {"name": "google", "url": "https://mcp.google.com/mcp"},
    ]


def test_blank_name_becomes_default(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_URLS", '[{"name": "  ", "url": "http://a/mcp"}]')
    Config._parse_mcp_servers()
    assert Config.MCP_SERVER_URLS == [{"name": "default", "url": "http://a/mcp"}]


def test_numbered_objects_are_parsed(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_URL_1", '{"name": "general", "url": "http://a/mcp"}')
    monkeypatch.setenv("MCP_SERVER_URL_2", '{"name": "", "url": "http://b/mcp"}')
    Config._parse_mcp_servers()
    assert Config.MCP_SERVER_URLS == [
        {"name": "general", "url": "http://a/mcp"},
        {"name": "default", "url": "http://b/mcp"},
    ]


def test_numbered_discovery_stops_at_first_gap(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_URL_1", '{"name": "one", "url": "http://a/mcp"}')
    monkeypatch.setenv("MCP_SERVER_URL_3", '{"name": "three", "url": "http://c/mcp"}')
    Config._parse_mcp_servers()
    assert Config.MCP_SERVER_URLS == [{"name": "one", "url": "http://a/mcp"}]


@pytest.mark.parametrize(
    "bad_entry",
    ['{"url": "http://b/mcp"}', '{"name": "b"}', "not-json", '"http://b/mcp"'],
)
def test_malformed_numbered_entry_is_skipped(monkeypatch, bad_entry):
    monkeypatch.setenv("MCP_SERVER_URL_1", '{"name": "one", "url": "http://a/mcp"}')
    monkeypatch.setenv("MCP_SERVER_URL_2", bad_entry)
    monkeypatch.setenv("MCP_SERVER_URL_3", '{"name": "three", "url": "http://c/mcp"}')
    Config._parse_mcp_servers()
    assert Config.MCP_SERVER_URLS == [
        {"name": "one", "url": "http://a/mcp"},
        {"name": "three", "url": "http://c/mcp"},
    ]


def test_urls_array_wins_over_numbered_urls(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_URLS", '[{"name": "array", "url": "http://a/mcp"}]')
    monkeypatch.setenv("MCP_SERVER_URL_1", '{"name": "numbered", "url": "http://b/mcp"}')
    Config._parse_mcp_servers()
    assert Config.MCP_SERVER_URLS == [{"name": "array", "url": "http://a/mcp"}]


def test_malformed_urls_array_falls_back_to_numbered(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_URLS", "{not json")
    monkeypatch.setenv("MCP_SERVER_URL_1", '{"name": "numbered", "url": "http://b/mcp"}')
    Config._parse_mcp_servers()
    assert Config.MCP_SERVER_URLS == [{"name": "numbered", "url": "http://b/mcp"}]


def test_array_of_bare_urls_is_not_supported(monkeypatch):
    """Only arrays of `{name, url}` objects are honoured; bare URLs yield nothing."""
    monkeypatch.setenv("MCP_SERVER_URLS", '["https://server1.com/mcp"]')
    Config._parse_mcp_servers()
    assert Config.MCP_SERVER_URLS == []


def test_empty_array_yields_empty_list(monkeypatch):
    """A distinct case from the above: there is no first element to type-check."""
    monkeypatch.setenv("MCP_SERVER_URLS", "[]")
    Config._parse_mcp_servers()
    assert Config.MCP_SERVER_URLS == []


def test_singular_mcp_server_url_is_not_parsed(monkeypatch):
    """`MCP_SERVER_URL` (no suffix) is not a supported form."""
    monkeypatch.setenv("MCP_SERVER_URL", "https://server1.com/mcp")
    Config._parse_mcp_servers()
    assert Config.MCP_SERVER_URLS == []


@pytest.mark.parametrize(
    "ignored",
    [
        pytest.param('["https://bare/mcp"]', id="bare-url-array"),
        pytest.param("[]", id="empty-array"),
        pytest.param("https://bare/mcp", id="singular-url"),
    ],
)
def test_unsupported_form_falls_through_to_numbered(monkeypatch, ignored):
    """An unsupported form is ignored rather than fatal when numbered vars exist."""
    key = "MCP_SERVER_URLS" if ignored.startswith("[") else "MCP_SERVER_URL"
    monkeypatch.setenv(key, ignored)
    monkeypatch.setenv("MCP_SERVER_URL_1", '{"name": "numbered", "url": "http://b/mcp"}')
    Config._parse_mcp_servers()
    assert Config.MCP_SERVER_URLS == [{"name": "numbered", "url": "http://b/mcp"}]


def test_array_entry_missing_name_becomes_default(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_URLS", '[{"url": "http://a/mcp"}]')
    Config._parse_mcp_servers()
    assert Config.MCP_SERVER_URLS == [{"url": "http://a/mcp", "name": "default"}]


def test_array_entry_missing_url_is_kept_then_rejected(monkeypatch):
    """Unlike the numbered form, a bad array entry is not skipped — it fails validation."""
    monkeypatch.setattr("app.config.Config.DATABASE_URL", "postgresql+asyncpg://x/y")
    monkeypatch.setattr("app.config.Config.LITELLM_API_KEY", "sk-test")
    monkeypatch.setenv("MCP_SERVER_URLS", '[{"name": "a"}]')
    Config._parse_mcp_servers()
    assert Config.MCP_SERVER_URLS == [{"name": "a"}]
    with pytest.raises(ValueError, match="must be an object with 'name' and 'url'"):
        Config.validate_config()


def test_array_mixing_objects_and_strings_raises_attribute_error(monkeypatch):
    """Known rough edge: a mixed array crashes instead of reporting bad config."""
    monkeypatch.setenv(
        "MCP_SERVER_URLS", '[{"name": "a", "url": "http://a/mcp"}, "http://bare/mcp"]'
    )
    with pytest.raises(AttributeError):
        Config._parse_mcp_servers()


def test_no_configuration_yields_empty_list():
    Config._parse_mcp_servers()
    assert Config.MCP_SERVER_URLS == []


def test_validate_config_requires_at_least_one_server(monkeypatch):
    monkeypatch.setattr("app.config.Config.DATABASE_URL", "postgresql+asyncpg://x/y")
    monkeypatch.setattr("app.config.Config.LITELLM_API_KEY", "sk-test")
    with pytest.raises(ValueError, match="At least one MCP server URL"):
        Config.validate_config()


def test_validate_config_accepts_named_servers(monkeypatch):
    monkeypatch.setattr("app.config.Config.DATABASE_URL", "postgresql+asyncpg://x/y")
    monkeypatch.setattr("app.config.Config.LITELLM_API_KEY", "sk-test")
    monkeypatch.setenv("MCP_SERVER_URLS", '[{"name": "general", "url": "http://a/mcp"}]')
    Config.validate_config()
    assert Config.MCP_SERVER_URLS == [{"name": "general", "url": "http://a/mcp"}]


def test_validate_config_requires_database_url(monkeypatch):
    monkeypatch.setattr("app.config.Config.DATABASE_URL", "")
    monkeypatch.setenv("MCP_SERVER_URLS", '[{"name": "general", "url": "http://a/mcp"}]')
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Config.validate_config()
