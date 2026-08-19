# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Config
from app.controllers import ag_ui_controller
from app.controllers.ag_ui_controller import (
    AGUIRunRequest,
    _resolve_local_context,
    run_agui_session,
)
from app.models.bindings import INVALID_RELATIVE_PATH
from app.models.bindings import LocationBinding, LocationType
from app.services import workspace_manager
from app.services.binding_contract import (
    BindingError,
    provision_source,
    select_relative_workspace,
)
from app.services.runtime_paths import runtime_root_for_thread


@asynccontextmanager
async def _noop_manage_run(**_kwargs):
    yield AsyncMock()


def test_resolve_local_context_requires_inplace_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "ALLOW_INPLACE_WORKSPACE", False)
    assert _resolve_local_context(str(tmp_path), True) is None


def test_resolve_local_context_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "ALLOW_INPLACE_WORKSPACE", True)
    ctx = _resolve_local_context(str(tmp_path), True)
    assert ctx is not None
    assert Path(ctx.workspace_path) == tmp_path.resolve()
    assert ctx.in_place is True


def test_resolve_local_context_missing_dir(monkeypatch):
    monkeypatch.setattr(Config, "ALLOW_INPLACE_WORKSPACE", True)
    assert _resolve_local_context("/tmp/does-not-exist-agui-ws-xyz", True) is None


def test_resolve_local_context_keeps_runtime_on_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "ALLOW_INPLACE_WORKSPACE", True)
    monkeypatch.setattr(Config, "WORKSPACES_ROOT", str(tmp_path / "roots"))
    stored = str(tmp_path / "runtime-stored")
    Path(stored).mkdir()
    ctx = _resolve_local_context(
        str(tmp_path),
        True,
        runtime_path=stored,
        thread_id="thread-resume-1",
    )
    assert ctx is not None
    assert ctx.runtime_path == stored


def test_resolve_local_context_creates_thread_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "ALLOW_INPLACE_WORKSPACE", True)
    monkeypatch.setattr(Config, "WORKSPACES_ROOT", str(tmp_path / "roots"))
    ctx = _resolve_local_context(str(tmp_path), True, thread_id="thread-fresh-1")
    assert ctx is not None
    assert ctx.runtime_path == str(runtime_root_for_thread("thread-fresh-1"))
    assert Path(ctx.runtime_path).is_dir()


def test_shared_folder_copy_does_not_mutate_caller(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "WORKSPACES_ROOT", str(tmp_path / "roots"))
    caller = tmp_path / "caller"
    nested = caller / "playbooks" / "demo"
    nested.mkdir(parents=True)
    (nested / "play.md").write_text("src")
    binding = LocationBinding(
        type=LocationType.SHARED_FOLDER,
        uri=str(caller),
        relativePath="playbooks/demo",
    )
    ws = asyncio.run(
        workspace_manager.provision(
            "agui-copy-1", provision_source(binding), in_place=False
        )
    )
    bound = select_relative_workspace(ws.path, binding.relative_path)
    assert Path(bound).resolve() != nested.resolve()
    assert (Path(bound) / "play.md").read_text() == "src"
    (Path(bound) / "play.md").write_text("mutated")
    assert (nested / "play.md").read_text() == "src"


def test_agui_request_accepts_new_contract_fields():
    req = AGUIRunRequest.model_validate(
        {
            "threadId": "t1",
            "input": {"type": "shared_folder", "uri": "/workspaces/pm"},
            "output": None,
            "mode": "workflow",
            "credentials": {"outputAccessToken": "transient"},
        }
    )
    assert req.input is not None
    assert req.input.type == LocationType.SHARED_FOLDER
    assert req.credentials.output_access_token == "transient"
    assert req.mode.value == "workflow"


@pytest.mark.asyncio
async def test_bind_only_does_not_provision_input(monkeypatch):
    provision = AsyncMock()
    monkeypatch.setattr(ag_ui_controller, "get_tool_hub", lambda: object())
    monkeypatch.setattr(workspace_manager, "provision", provision)
    monkeypatch.setattr(
        ag_ui_controller.run_lifecycle_service,
        "ensure_thread_session",
        AsyncMock(),
    )

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(ag_ui_controller, "get_session", lambda: SessionContext())
    monkeypatch.setattr(
        ag_ui_controller.run_lifecycle_service,
        "reject_if_close_requested",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        ag_ui_controller.run_lifecycle_service,
        "get_thread_session",
        AsyncMock(return_value=None),
    )

    response = await run_agui_session(
        AGUIRunRequest.model_validate(
            {
                "threadId": "bind-only",
                "input": {
                    "type": "shared_folder",
                    "uri": "/workspaces/pm",
                },
                "messages": [],
            }
        )
    )

    assert response.status_code == 200
    provision.assert_not_awaited()


@pytest.mark.asyncio
async def test_agui_copy_uses_persisted_execution_identity(tmp_path, monkeypatch):
    source = tmp_path / "source"
    copied = tmp_path / "copy"
    source.mkdir()
    copied.mkdir()
    (copied / "AGENTS.md").write_text("instructions")
    execution_id = uuid.uuid4()
    create_execution = AsyncMock(return_value=execution_id)
    provision = AsyncMock(
        return_value=SimpleNamespace(path=str(copied), in_place=False)
    )
    update_execution = AsyncMock()

    class FakeHub:
        async def process_request(self, **_kwargs):
            return {"success": True}

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(ag_ui_controller, "get_tool_hub", lambda: FakeHub())
    monkeypatch.setattr(
        ag_ui_controller,
        "_prepare_thread_claims",
        AsyncMock(return_value=(0, False)),
    )
    monkeypatch.setattr(ag_ui_controller, "_create_agui_execution", create_execution)
    monkeypatch.setattr(
        ag_ui_controller.run_lifecycle_service,
        "ensure_thread_session",
        AsyncMock(),
    )
    monkeypatch.setattr(
        ag_ui_controller.run_lifecycle_service,
        "try_create_active_run",
        AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
    )
    monkeypatch.setattr(
        ag_ui_controller.run_lifecycle_service,
        "reject_if_close_requested",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        ag_ui_controller.run_lifecycle_service,
        "get_thread_session",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        ag_ui_controller.run_lifecycle_service,
        "associate_execution_origin",
        AsyncMock(),
    )
    monkeypatch.setattr(
        ag_ui_controller.session_close_service,
        "manage_run",
        _noop_manage_run,
    )
    monkeypatch.setattr(ag_ui_controller, "_finalize_run_segment", AsyncMock())
    monkeypatch.setattr(
        ag_ui_controller,
        "_discard_stale_awaiting_and_cleanup_orphans",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(ag_ui_controller, "_build_fresh_system_prompt", lambda *_args, **_kw: ("", None))
    monkeypatch.setattr(workspace_manager, "provision", provision)
    monkeypatch.setattr(ag_ui_controller, "get_session", lambda: SessionContext())
    monkeypatch.setattr(
        ag_ui_controller.execution_state_service,
        "update_execution",
        update_execution,
    )
    monkeypatch.setattr(
        ag_ui_controller.execution_state_service,
        "discard_awaiting_states_for_thread",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(Config, "WORKSPACES_ROOT", str(tmp_path / "roots"))

    response = await run_agui_session(
        AGUIRunRequest.model_validate(
            {
                "threadId": "tracked-copy",
                "input": {
                    "type": "shared_folder",
                    "uri": str(source),
                },
                "messages": [
                    {"id": "m1", "role": "user", "content": "run"}
                ],
            }
        )
    )

    assert response.status_code == 200
    create_execution.assert_awaited_once()
    provision.assert_awaited_once()
    assert provision.await_args.args[0] == execution_id
    assert provision.await_args.args[1] == str(source)
    assert provision.await_args.kwargs["in_place"] is False
    update_execution.assert_awaited()


@pytest.mark.asyncio
async def test_agui_relative_path_failure_cleans_tracked_copy(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    copied = tmp_path / "copy"
    source.mkdir()
    copied.mkdir()
    execution_id = uuid.uuid4()
    cleanup = MagicMock()
    finalize = AsyncMock()

    monkeypatch.setattr(ag_ui_controller, "get_tool_hub", lambda: object())
    monkeypatch.setattr(
        ag_ui_controller,
        "_prepare_thread_claims",
        AsyncMock(return_value=(0, False)),
    )
    monkeypatch.setattr(
        ag_ui_controller,
        "_create_agui_execution",
        AsyncMock(return_value=execution_id),
    )
    monkeypatch.setattr(
        ag_ui_controller.run_lifecycle_service,
        "ensure_thread_session",
        AsyncMock(),
    )
    monkeypatch.setattr(
        ag_ui_controller.run_lifecycle_service,
        "try_create_active_run",
        AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
    )
    monkeypatch.setattr(
        ag_ui_controller.run_lifecycle_service,
        "reject_if_close_requested",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        ag_ui_controller.run_lifecycle_service,
        "get_thread_session",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        ag_ui_controller.run_lifecycle_service,
        "associate_execution_origin",
        AsyncMock(),
    )
    monkeypatch.setattr(ag_ui_controller.session_close_service, "manage_run", _noop_manage_run)
    monkeypatch.setattr(ag_ui_controller, "_finalize_run_segment", AsyncMock())
    monkeypatch.setattr(
        ag_ui_controller,
        "_discard_stale_awaiting_and_cleanup_orphans",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        ag_ui_controller.execution_state_service,
        "update_execution",
        AsyncMock(),
    )

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(ag_ui_controller, "get_session", lambda: SessionContext())
    monkeypatch.setattr(
        workspace_manager,
        "provision",
        AsyncMock(return_value=SimpleNamespace(path=str(copied), in_place=False)),
    )
    monkeypatch.setattr(workspace_manager, "cleanup", cleanup)
    monkeypatch.setattr(ag_ui_controller, "_finalize", finalize)
    monkeypatch.setattr(
        ag_ui_controller,
        "select_relative_workspace",
        lambda *_args: (_ for _ in ()).throw(
            BindingError(INVALID_RELATIVE_PATH, "invalid relative path")
        ),
    )
    monkeypatch.setattr(Config, "WORKSPACES_ROOT", str(tmp_path / "roots"))

    response = await run_agui_session(
        AGUIRunRequest.model_validate(
            {
                "threadId": "failed-copy",
                "input": {
                    "type": "shared_folder",
                    "uri": str(source),
                },
                "messages": [
                    {"id": "m1", "role": "user", "content": "run"}
                ],
            }
        )
    )

    assert response.status_code == 200
    cleanup.assert_called_once_with(execution_id)
    finalize.assert_awaited_once()
