# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Public-facing HTTP surface and configuration hygiene.

The service ships no authentication of its own, so the browser-facing surface
is governed entirely by configuration: which origins may call it, whether those
calls may carry credentials, and whether the API describes itself to anonymous
callers. These pin the shipped defaults and the combinations that are refused
outright.
"""

import importlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Config, _env_list


# --- CORS and documentation defaults ---------------------------------------


def test_cors_ships_closed():
    """No origin is trusted until a deployment names one."""
    assert Config.CORS_ALLOWED_ORIGINS == []
    assert Config.CORS_ALLOW_CREDENTIALS is False


def test_docs_ship_enabled_for_the_quickstart():
    assert Config.DOCS_ENABLED is True


def test_dangerous_capabilities_ship_disabled():
    assert Config.LOCAL_SHELL_ENABLED is False
    assert Config.HOOKS_ENABLED is False
    assert Config.ALLOW_INPLACE_WORKSPACE is False
    # The tool surface itself stays available; only execution is withheld.
    assert Config.LOCAL_TOOLS_ENABLED is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", []),
        ("http://localhost:5173", ["http://localhost:5173"]),
        (
            "http://localhost:5173, http://127.0.0.1:5173",
            ["http://localhost:5173", "http://127.0.0.1:5173"],
        ),
        ("  ,  ", []),
        ("*", ["*"]),
    ],
)
def test_env_list_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("SOME_LIST", raw)
    assert _env_list("SOME_LIST") == expected


# --- Refused combinations ---------------------------------------------------


@pytest.fixture
def valid_baseline(monkeypatch):
    """Satisfy the unrelated required settings so validation reaches our checks."""
    monkeypatch.setenv("MCP_SERVER_URLS", '[{"name": "a", "url": "http://a/mcp"}]')
    monkeypatch.setattr(Config, "DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setattr(Config, "LITELLM_API_KEY", "sk-test")


def test_wildcard_origins_with_credentials_is_refused(monkeypatch, valid_baseline):
    """Starlette echoes the caller's origin for credentialed wildcard requests,
    which would let any site call the API with the browser's cookies."""
    monkeypatch.setattr(Config, "CORS_ALLOWED_ORIGINS", ["*"])
    monkeypatch.setattr(Config, "CORS_ALLOW_CREDENTIALS", True)
    with pytest.raises(ValueError, match="cannot be combined"):
        Config.validate_config()


def test_wildcard_origins_without_credentials_is_allowed(monkeypatch, valid_baseline):
    monkeypatch.setattr(Config, "CORS_ALLOWED_ORIGINS", ["*"])
    monkeypatch.setattr(Config, "CORS_ALLOW_CREDENTIALS", False)
    Config.validate_config()


def test_inplace_workspace_without_allowed_roots_is_refused(monkeypatch, valid_baseline):
    """Unrestricted in-place binding would reach any path in the container."""
    monkeypatch.setattr(Config, "ALLOW_INPLACE_WORKSPACE", True)
    monkeypatch.setattr(Config, "WORKSPACE_ALLOWED_ROOTS", "   ")
    with pytest.raises(ValueError, match="WORKSPACE_ALLOWED_ROOTS"):
        Config.validate_config()


def test_inplace_workspace_with_confined_roots_is_allowed(monkeypatch, valid_baseline):
    """The shipped Docker posture: in place, but only under the mounted root."""
    monkeypatch.setattr(Config, "ALLOW_INPLACE_WORKSPACE", True)
    monkeypatch.setattr(Config, "WORKSPACE_ALLOWED_ROOTS", "/workspaces")
    Config.validate_config()


# --- Credentials without a shipped default ----------------------------------


def test_litellm_api_key_is_required(monkeypatch, valid_baseline):
    """It once defaulted to sk-1234, so every deployment shared one known key."""
    monkeypatch.setattr(Config, "LITELLM_API_KEY", "")

    with pytest.raises(ValueError, match="LITELLM_API_KEY"):
        Config.validate_config()


def test_no_shipped_default_stands_in_for_the_litellm_key():
    """Read from the source: an import-time default would be invisible here."""
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src/app/config.py").read_text()
    match = re.search(r"LITELLM_API_KEY = os\.getenv\('LITELLM_API_KEY',\s*(.*?)\)", source)

    assert match, "LITELLM_API_KEY is no longer read the way this test assumes"
    assert match.group(1).strip() in ("''", '""'), (
        f"LITELLM_API_KEY must have no default, found {match.group(1)!r}"
    )


# --- Unfilled placeholders --------------------------------------------------


def test_placeholder_is_detected_as_a_whole_value(monkeypatch):
    monkeypatch.setattr(Config, "LITELLM_API_KEY", "__LITELLM_API_KEY__")
    assert "LITELLM_API_KEY" in Config._unfilled_placeholders()


def test_placeholder_is_detected_inside_a_composite_value(monkeypatch):
    """The check is unanchored: placeholders hide inside connection strings."""
    monkeypatch.setattr(
        Config,
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:__POSTGRES_PASSWORD__@host:5432/pyagent",
    )
    assert "DATABASE_URL" in Config._unfilled_placeholders()


def test_filled_values_are_not_flagged(monkeypatch):
    monkeypatch.setattr(Config, "LITELLM_API_KEY", "sk-real-key")
    monkeypatch.setattr(
        Config, "DATABASE_URL", "postgresql+asyncpg://postgres:hunter2@host:5432/pyagent"
    )
    flagged = Config._unfilled_placeholders()
    assert "LITELLM_API_KEY" not in flagged
    assert "DATABASE_URL" not in flagged


def test_validation_reports_the_variable_and_not_the_value(monkeypatch, valid_baseline):
    monkeypatch.setattr(Config, "DATABASE_URL", "postgres://u:__POSTGRES_PASSWORD__@h/d")
    with pytest.raises(ValueError) as excinfo:
        Config.validate_config()
    message = str(excinfo.value)
    assert "DATABASE_URL" in message
    assert "__POSTGRES_PASSWORD__" not in message


# --- Wiring of the live app -------------------------------------------------


def _reload_app(monkeypatch, *, docs_enabled, origins, credentials=False):
    """Rebuild app.main so module-level FastAPI wiring reflects the settings."""
    monkeypatch.setattr(Config, "DOCS_ENABLED", docs_enabled)
    monkeypatch.setattr(Config, "CORS_ALLOWED_ORIGINS", origins)
    monkeypatch.setattr(Config, "CORS_ALLOW_CREDENTIALS", credentials)
    import app.main as main

    return importlib.reload(main)


@pytest.fixture(autouse=True)
def restore_app_module():
    """Leave app.main matching the ambient configuration for other tests."""
    yield
    import app.main as main

    importlib.reload(main)


def test_docs_disabled_withdraws_swagger_and_the_schema(monkeypatch):
    main = _reload_app(monkeypatch, docs_enabled=False, origins=[])
    client = TestClient(main.app)
    assert client.get("/swagger").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_docs_enabled_serves_swagger_and_the_schema(monkeypatch):
    main = _reload_app(monkeypatch, docs_enabled=True, origins=[])
    client = TestClient(main.app)
    assert client.get("/swagger").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    # ReDoc stays off regardless; Swagger is the only documented surface.
    assert client.get("/redoc").status_code == 404


def test_no_configured_origin_sends_no_cors_headers(monkeypatch):
    main = _reload_app(monkeypatch, docs_enabled=True, origins=[])
    client = TestClient(main.app)
    response = client.get("/openapi.json", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def test_listed_origin_is_allowed_and_others_are_not(monkeypatch):
    main = _reload_app(
        monkeypatch, docs_enabled=True, origins=["http://localhost:5173"]
    )
    client = TestClient(main.app)

    allowed = client.get(
        "/openapi.json", headers={"Origin": "http://localhost:5173"}
    )
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"

    refused = client.get("/openapi.json", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in refused.headers


# --- The quickstart must actually reach the bundled demo ---------------------

REPO = Path(__file__).resolve().parents[1]


def _example_origins() -> list:
    """CORS_ALLOWED_ORIGINS as the Docker quickstart ships it."""
    for line in (REPO / "src/.env/docker/.env.example").read_text().splitlines():
        if line.startswith("CORS_ALLOWED_ORIGINS="):
            _, _, value = line.partition("=")
            return [item.strip() for item in value.split(",") if item.strip()]
    raise AssertionError("CORS_ALLOWED_ORIGINS is absent from the Docker example")


def test_docker_example_allows_the_demo_dev_server_origin():
    """Otherwise the demo builds, starts, and every request fails in the browser."""
    port = re.search(
        r"port:\s*(\d+)", (REPO / "ag-ui-demo/vite.config.ts").read_text()
    )
    assert port, "the demo no longer pins a dev-server port"

    origins = _example_origins()
    for host in ("localhost", "127.0.0.1"):
        assert f"http://{host}:{port.group(1)}" in origins, (
            f"the demo serves on port {port.group(1)}; the Docker example must list it"
        )


def test_the_demo_origin_is_accepted_by_the_app_as_configured(monkeypatch):
    """Drives the real middleware with the origins the example file supplies."""
    main = _reload_app(monkeypatch, docs_enabled=True, origins=_example_origins())
    client = TestClient(main.app)

    response = client.get("/isalive", headers={"Origin": "http://localhost:5173"})

    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_the_code_default_stays_closed_though_the_example_opens_it(monkeypatch):
    """The example is a local convenience; the default is what others inherit."""
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)

    assert _env_list("CORS_ALLOWED_ORIGINS") == []
