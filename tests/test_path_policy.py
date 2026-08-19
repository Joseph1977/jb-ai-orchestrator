# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import pytest

from app.services import path_policy
from app.services.workspace_manager import WorkspaceError, resolve_within


def test_denylist_blocks_resolve(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.PATH_POLICY_ENABLED", True)
    monkeypatch.setattr("app.config.Config.PATH_DENYLIST", ".env,**/*.pem")
    monkeypatch.setattr("app.config.Config.PATH_ALLOWLIST", "")
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / "ok.txt").write_text("hi")
    assert resolve_within(str(tmp_path), "ok.txt").endswith("ok.txt")
    with pytest.raises(WorkspaceError, match="PATH_DENYLIST"):
        resolve_within(str(tmp_path), ".env")


def test_allowlist_restricts(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.PATH_POLICY_ENABLED", True)
    monkeypatch.setattr("app.config.Config.PATH_DENYLIST", "")
    monkeypatch.setattr("app.config.Config.PATH_ALLOWLIST", "output/**")
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "memory.json").write_text("{}")
    (tmp_path / "secret.txt").write_text("x")
    assert "memory.json" in resolve_within(str(tmp_path), "output/memory.json")
    with pytest.raises(WorkspaceError, match="PATH_ALLOWLIST"):
        resolve_within(str(tmp_path), "secret.txt")


def test_workspace_allowed_roots(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.PATH_POLICY_ENABLED", True)
    allowed = tmp_path / "sessions"
    allowed.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr("app.config.Config.WORKSPACE_ALLOWED_ROOTS", str(allowed))
    path_policy.check_workspace_root_allowed(str(allowed / "u1"))
    # create nested so relative_to works — path must exist? check uses resolve only
    nested = allowed / "u1"
    nested.mkdir()
    path_policy.check_workspace_root_allowed(str(nested))
    with pytest.raises(WorkspaceError, match="WORKSPACE_ALLOWED_ROOTS"):
        path_policy.check_workspace_root_allowed(str(other))


def test_shell_denylist(monkeypatch):
    monkeypatch.setattr("app.config.Config.PATH_POLICY_ENABLED", True)
    monkeypatch.setattr("app.config.Config.SHELL_COMMAND_DENYLIST", r"rm\s+-rf\s+/")
    monkeypatch.setattr("app.config.Config.SHELL_COMMAND_ALLOWLIST", "")
    assert path_policy.check_shell_command("echo hi") is None
    assert path_policy.check_shell_command("rm -rf /") is not None
