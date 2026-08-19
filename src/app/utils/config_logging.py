# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Safe configuration logging — never dump raw environment or secrets."""

from __future__ import annotations

import os
import re
from typing import Any

# Keys safe to log at INFO (explicit allowlist).
_SAFE_LOG_KEYS = frozenset(
    {
        "REGION",
        "Region",
        "ENVIRONMENT",
        "Environment",
        "SERVICE_NAME",
        "ServiceName",
        "LOG_LEVEL",
        "Logging_LogLevel_Default",
        "LOG_FOLDER",
        "General_LogFolder",
        "SWAGGER_BASE_PATH",
        "SwaggerBasePath",
        "ENV",
        "MAX_TOOL_CALLS",
        "LOCAL_TOOLS_ENABLED",
        "LOCAL_TOOLS_NAMESPACE",
        "SUBAGENT_ENABLED",
        "SUBAGENT_MAX_DEPTH",
        "SUBAGENT_MAX_TOOL_CALLS",
        "LLM_STREAMING_ENABLED",
        "HOOKS_ENABLED",
        "CONTEXT_COMPACTION_ENABLED",
        "ALLOW_INPLACE_WORKSPACE",
        "PATH_POLICY_ENABLED",
        "MULTIMODAL_ENABLED",
    }
)

_REDACT_KEY = re.compile(
    r"(password|secret|token|api[_-]?key|auth|credential|private|bearer|jwt|session)",
    re.I,
)


_ALWAYS_REDACT_KEYS = frozenset({"DATABASE_URL", "LITELLM_API_KEY"})


def _is_sensitive_key(key: str) -> bool:
    if key in _SAFE_LOG_KEYS:
        return False
    if key in _ALWAYS_REDACT_KEYS:
        return True
    return bool(_REDACT_KEY.search(key))


def redact_value(key: str, value: Any) -> str:
    """Return a log-safe representation of a config value."""
    if value is None:
        return "(unset)"
    if _is_sensitive_key(key):
        return "***REDACTED***"
    text = str(value)
    if len(text) > 200:
        return text[:200] + "…"
    return text


def safe_config_snapshot(config_cls: type) -> dict[str, str]:
    """Build an allowlisted, redacted snapshot of Config for logging."""
    snapshot: dict[str, str] = {}
    for key in sorted(_SAFE_LOG_KEYS):
        if hasattr(config_cls, key):
            snapshot[key] = redact_value(key, getattr(config_cls, key))
    # MCP server count/names only (never URLs with embedded credentials).
    urls = getattr(config_cls, "MCP_SERVER_URLS", None)
    if isinstance(urls, list):
        snapshot["MCP_SERVER_COUNT"] = str(len(urls))
        snapshot["MCP_SERVER_NAMES"] = ", ".join(
            str(s.get("name", "?")) for s in urls if isinstance(s, dict)
        )
    snapshot["LITELLM_BASE_URL"] = redact_value(
        "LITELLM_BASE_URL", getattr(config_cls, "LITELLM_BASE_URL", None)
    )
    snapshot["DATABASE_URL"] = redact_value(
        "DATABASE_URL", getattr(config_cls, "DATABASE_URL", None)
    )
    return snapshot


def log_safe_configuration(config_cls: type, logger) -> None:
    """Log configuration using the safe snapshot only."""
    snapshot = safe_config_snapshot(config_cls)
    logger.info("--- Configuration (safe snapshot) ---")
    for key, value in snapshot.items():
        logger.info("%s: %s", key, value)
    logger.info("-------------------------------------")
