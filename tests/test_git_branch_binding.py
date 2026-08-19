# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Git input branch validation, clone argv, and orchestrator persistence."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.models.bindings import (
    INVALID_BRANCH,
    ExecutionMode,
    LocationBinding,
    LocationType,
    Materialization,
)
from app.models.requests import InitiateOrchestratorInput
from app.services import workspace_manager as wm
from app.services.binding_contract import (
    BindingError,
    normalize_git_ref,
    provision_branch,
    sanitize_binding,
    sanitize_execution_config,
    validate_input_binding,
    validate_output_binding,
)
from app.services.binding_runtime import ensure_input_workspace


@pytest.mark.parametrize(
    "ref",
    [
        "main",
        "feature/login",
        "v1.2.3",
        "release-2024",
    ],
)
def test_normalize_git_ref_accepts_safe_refs(ref):
    assert normalize_git_ref(ref) == ref
    assert normalize_git_ref(f"  {ref}  ") == ref


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "   ",
        "-main",
        "feature..login",
        "feature@{upstream",
        "feature\\login",
        "feature/",
        "feature.",
        "feature/.lock",
        "a" * 256,
        "bad ref",
        "feature//login",
        "feature~1",
        "feature^2",
        "feature:next",
        "feature?next",
        "feature*",
        "feature[next",
        ".hidden",
        "feature/.hidden",
        "@",
    ],
)
def test_normalize_git_ref_rejects_unsafe_refs(ref):
    with pytest.raises(BindingError) as exc:
        normalize_git_ref(ref)
    assert exc.value.code == INVALID_BRANCH


def test_validate_input_git_accepts_optional_branch():
    binding = LocationBinding(
        type=LocationType.GIT,
        uri="https://github.com/a/b.git",
        branch=" develop ",
    )
    out = validate_input_binding(binding, mode=ExecutionMode.WORKFLOW)
    assert out.branch == "develop"


def test_validate_input_git_omitted_branch_is_backwards_compatible():
    binding = LocationBinding(
        type=LocationType.GIT,
        uri="https://github.com/a/b.git",
    )
    out = validate_input_binding(binding, mode=ExecutionMode.WORKFLOW)
    assert out.branch is None


def test_validate_input_rejects_branch_for_non_git():
    binding = LocationBinding(
        type=LocationType.SHARED_FOLDER,
        uri="/workspaces/pm",
        branch="main",
    )
    with pytest.raises(BindingError) as exc:
        validate_input_binding(binding, mode=ExecutionMode.WORKFLOW)
    assert exc.value.code == INVALID_BRANCH


def test_validate_output_rejects_branch():
    binding = LocationBinding(
        type=LocationType.SHARED_FOLDER,
        uri="/workspaces/out",
        branch="main",
    )
    with pytest.raises(BindingError) as exc:
        validate_output_binding(binding)
    assert exc.value.code == INVALID_BRANCH


def test_sanitize_binding_persists_branch():
    binding = validate_input_binding(
        LocationBinding(
            type=LocationType.GIT,
            uri="https://github.com/a/b.git",
            branch="main",
        ),
        mode=ExecutionMode.WORKFLOW,
    )
    payload = sanitize_binding(binding)
    assert payload["branch"] == "main"
    assert payload["uri"] == "https://github.com/a/b.git"


def test_sanitize_execution_config_omits_branch_when_unset():
    binding = validate_input_binding(
        LocationBinding(type=LocationType.GIT, uri="https://github.com/a/b.git"),
        mode=ExecutionMode.WORKFLOW,
    )
    config = sanitize_execution_config(input_binding=binding, mode=ExecutionMode.WORKFLOW)
    assert "branch" not in config["input"]


def test_clone_argv_uses_branch_and_single_branch(tmp_path, monkeypatch):
    captured = {}

    async def fake_run_git(args, cwd=None, timeout=None, *, env=None):
        captured["args"] = args
        return 0, "", ""

    monkeypatch.setattr(wm, "_run_git", fake_run_git)
    asyncio.run(
        wm._clone_repo(
            "https://example.com/private/repo.git",
            tmp_path / "repo",
            [],
            branch="develop",
        )
    )

    assert captured["args"] == [
        "clone",
        "--depth",
        "1",
        "--branch",
        "develop",
        "--single-branch",
        "https://example.com/private/repo.git",
        str(tmp_path / "repo"),
    ]


def test_clone_argv_without_branch_omits_single_branch_flags(tmp_path, monkeypatch):
    captured = {}

    async def fake_run_git(args, cwd=None, timeout=None, *, env=None):
        captured["args"] = args
        return 0, "", ""

    monkeypatch.setattr(wm, "_run_git", fake_run_git)
    asyncio.run(
        wm._clone_repo(
            "https://example.com/private/repo.git",
            tmp_path / "repo",
            [],
        )
    )

    assert captured["args"] == [
        "clone",
        "--depth",
        "1",
        "https://example.com/private/repo.git",
        str(tmp_path / "repo"),
    ]


def test_git_input_token_is_not_put_in_clone_arguments(tmp_path, monkeypatch):
    captured = {}

    async def fake_run_git(args, cwd=None, timeout=None, *, env=None):
        captured["args"] = args
        captured["env"] = env
        return 0, "", ""

    monkeypatch.setattr(wm, "_run_git", fake_run_git)
    asyncio.run(
        wm._clone_repo(
            "https://example.com/private/repo.git",
            tmp_path / "repo",
            [],
            branch="main",
            input_access_token="secret-token",
        )
    )

    joined = " ".join(captured["args"])
    assert "secret-token" not in joined
    assert captured["env"]["GIT_CONFIG_VALUE_0"].endswith("secret-token")


def test_missing_workspace_reprovision_passes_persisted_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(wm.Config, "WORKSPACES_ROOT", str(tmp_path / "roots"))
    captured = {}

    async def fake_provision(execution_id, source, *, in_place=False, branch=None, input_access_token=None):
        captured["branch"] = branch
        src = tmp_path / "playbook"
        src.mkdir(exist_ok=True)
        (src / "note.md").write_text("src")
        workspace = tmp_path / "roots" / str(execution_id) / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "note.md").write_text("src")
        return SimpleNamespace(path=str(workspace))

    monkeypatch.setattr(wm, "provision", fake_provision)
    config = {
        "input": {
            "type": "git",
            "uri": "https://github.com/a/b.git",
            "relativePath": ".",
            "branch": "develop",
        },
        "mode": "workflow",
        "inPlace": False,
    }
    resolved = asyncio.run(
        ensure_input_workspace(
            "exec-git-reprovision",
            config,
            workspace_path=str(tmp_path / "missing"),
        )
    )
    assert captured["branch"] == "develop"
    assert Path(resolved).is_dir()


def test_provision_branch_returns_normalized_ref():
    binding = validate_input_binding(
        LocationBinding(
            type=LocationType.GIT,
            uri="https://github.com/a/b.git",
            branch=" main ",
        ),
        mode=ExecutionMode.WORKFLOW,
    )
    assert provision_branch(binding) == "main"


def test_provision_branch_revalidates_persisted_ref():
    binding = LocationBinding(
        type=LocationType.GIT,
        uri="https://github.com/a/b.git",
        branch="-unsafe",
    )
    with pytest.raises(BindingError) as exc:
        provision_branch(binding)
    assert exc.value.code == INVALID_BRANCH


@pytest.mark.asyncio
async def test_initiate_persists_git_branch_in_config():
    from app.controllers.orchestrator_controller import initiate

    exec_id = uuid4()
    execution = SimpleNamespace(id=exec_id)
    manifest = SimpleNamespace(
        orchestration_type="generic",
        eager_context="eager",
        detected=True,
        confidence=80,
        agents=[],
        skills=[],
        rules=[],
        commands=[],
        notes=[],
    )
    ws = SimpleNamespace(
        path="/sandbox",
        source_kind=SimpleNamespace(value="git_url"),
        notes=[],
    )
    persisted: dict = {}

    async def _update_execution(session, execution_id, **kwargs):
        persisted.update(kwargs)

    @asynccontextmanager
    async def fake_get_session():
        yield object()

    git_binding = validate_input_binding(
        LocationBinding(
            type=LocationType.GIT,
            uri="https://github.com/a/b.git",
            branch="develop",
            materialization=Materialization.COPY,
        ),
        mode=ExecutionMode.WORKFLOW,
    )

    with patch("app.controllers.orchestrator_controller.get_session", fake_get_session), \
         patch("app.controllers.orchestrator_controller.execution_state_service.create_execution", AsyncMock(return_value=execution)), \
         patch("app.controllers.orchestrator_controller.run_lifecycle_service.associate_execution_origin", AsyncMock()), \
         patch("app.controllers.orchestrator_controller.workspace_manager.provision", AsyncMock(return_value=ws)) as provision_mock, \
         patch("app.controllers.orchestrator_controller.collect_manifest", return_value=manifest), \
         patch("app.controllers.orchestrator_controller.ensure_runtime", return_value="/tmp/runtime"), \
         patch("app.controllers.orchestrator_controller.execution_state_service.update_execution", _update_execution), \
         patch("app.controllers.orchestrator_controller.select_relative_workspace", return_value="/sandbox/ws"), \
         patch("app.controllers.orchestrator_controller.resolve_initiate_input", return_value=(git_binding, ExecutionMode.WORKFLOW, False)), \
         patch("app.controllers.orchestrator_controller.assert_output_feature_enabled"):
        await initiate(
            InitiateOrchestratorInput(
                input=LocationBinding(
                    type=LocationType.GIT,
                    uri="https://github.com/a/b.git",
                    branch="develop",
                )
            )
        )

    provision_mock.assert_awaited_once()
    assert provision_mock.await_args.kwargs["branch"] == "develop"
    config = persisted.get("config") or {}
    assert config["input"]["branch"] == "develop"
    assert config["input"]["uri"] == "https://github.com/a/b.git"
