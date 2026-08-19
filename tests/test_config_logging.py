# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for safe configuration logging."""

import logging

from app.config import Config
from app.utils.config_logging import redact_value, safe_config_snapshot


def test_redact_sensitive_keys():
    assert redact_value("LITELLM_API_KEY", "sk-secret") == "***REDACTED***"
    assert redact_value("DATABASE_PASSWORD", "hunter2") == "***REDACTED***"
    assert redact_value("AUTH_TOKEN", "abc") == "***REDACTED***"


def test_allowlisted_keys_not_redacted():
    assert redact_value("MAX_TOOL_CALLS", "10") == "10"
    assert redact_value("LOCAL_TOOLS_ENABLED", "True") == "True"


def test_safe_snapshot_never_includes_secrets(monkeypatch):
    monkeypatch.setattr("app.config.Config.LITELLM_API_KEY", "sk-test-secret", raising=False)
    monkeypatch.setattr("app.config.Config.DATABASE_URL", "postgresql://user:pass@localhost/db", raising=False)
    snapshot = safe_config_snapshot(Config)
    blob = str(snapshot)
    assert "sk-test-secret" not in blob
    assert "pass@localhost" not in blob
    assert "LITELLM_API_KEY" not in snapshot
    assert snapshot["DATABASE_URL"] == "***REDACTED***"
