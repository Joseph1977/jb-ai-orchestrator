# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ag_ui.core.types import SystemMessage

from app.models.bindings import (
    INPUT_CREDENTIAL_REQUIRED,
    INPUT_FORBIDDEN,
    INPUT_PROVISION_FAILED,
    INPUT_WORKSPACE_MISSING,
    RUN_BINDING_AMBIGUOUS,
)
from app.services.agui_messages import (
    MANDATORY_UI_CONTRACT_BEGIN,
    MANDATORY_UI_CONTRACT_END,
    build_authoritative_system_content,
)
from app.services.binding_contract import BindingError
from app.services.binding_runtime import (
    RUN_BINDING_END,
    RUN_BINDING_START,
    binding_system_prompt,
    ensure_input_workspace,
    refresh_segment_run_binding,
)
from app.services import workspace_manager as wm


def _binding_block(mode: str = "workflow") -> str:
    return binding_system_prompt({"mode": mode, "output": None})


def _wrap(inner: str) -> str:
    return f"{RUN_BINDING_START}\n{inner}\n{RUN_BINDING_END}"


def _config(mode: str = "workflow") -> dict:
    return {"mode": mode, "output": None}


def _client_contract_with_stray_markers() -> str:
    return "\n".join(
        [
            "Use Ask-Text for structured user input.",
            f"{RUN_BINDING_START}",
            "start only without end",
            "between markers",
            f"{RUN_BINDING_END}",
            "end only without start",
            f"{RUN_BINDING_START}",
            "duplicate harness-like block",
            f"{RUN_BINDING_END}",
        ]
    )


def _extract_ui_contract_body(content: str) -> str:
    begin = content.index(MANDATORY_UI_CONTRACT_BEGIN) + len(MANDATORY_UI_CONTRACT_BEGIN)
    end = content.rindex(MANDATORY_UI_CONTRACT_END)
    return content[begin:end]


def test_refresh_ignores_stray_markers_inside_mandatory_ui_contract():
    client_text = _client_contract_with_stray_markers()
    harness_binding = _wrap("Mode: stale")
    composed = build_authoritative_system_content(
        harness_prompt=f"Harness catalog\n\n{harness_binding}",
        incoming_messages=[SystemMessage(id="s1", content=client_text)],
        contexts=[{"description": "thread", "value": "resume-1"}],
    )
    messages = [{"role": "system", "content": composed}]
    out = refresh_segment_run_binding(messages, _config(mode="working_copy"))
    refreshed = out[0]["content"]

    assert _extract_ui_contract_body(refreshed) == client_text
    assert refreshed.count(RUN_BINDING_START) == 3  # one harness + two client strays
    assert refreshed.count(RUN_BINDING_END) == 3  # one harness + two client strays
    assert refreshed.index(RUN_BINDING_START) < refreshed.index(MANDATORY_UI_CONTRACT_BEGIN)
    assert "Mode: stale" not in refreshed
    assert "Mode: working_copy" in refreshed
    assert refreshed.count("Mode: working_copy") == 1


def test_refresh_still_rejects_malformed_markers_outside_ui_contract():
    client_text = "Safe UI contract body"
    composed = build_authoritative_system_content(
        harness_prompt=f"Harness\n{RUN_BINDING_START}\nno end",
        incoming_messages=[SystemMessage(id="s1", content=client_text)],
    )
    messages = [{"role": "system", "content": composed}]
    with pytest.raises(BindingError) as exc:
        refresh_segment_run_binding(messages, _config())
    assert exc.value.code == RUN_BINDING_AMBIGUOUS


def test_refresh_collapses_duplicate_harness_blocks_with_ui_contract_present():
    first = _wrap("first block")
    second = _wrap("second block")
    client_text = "Client contract survives"
    composed = build_authoritative_system_content(
        harness_prompt=f"Harness\n\n{first}\n\nMid harness\n\n{second}",
        incoming_messages=[SystemMessage(id="s1", content=client_text)],
    )
    out = refresh_segment_run_binding(
        [{"role": "system", "content": composed}],
        _config(),
    )
    content = out[0]["content"]
    assert content.count(RUN_BINDING_START) == 1
    assert "first block" not in content
    assert "second block" not in content
    assert _extract_ui_contract_body(content) == client_text
    assert content.index(RUN_BINDING_START) < content.index(MANDATORY_UI_CONTRACT_BEGIN)


def test_refresh_malformed_ui_contract_delimiter_does_not_hide_harness_markers():
    partial_contract = (
        f"{MANDATORY_UI_CONTRACT_BEGIN}client body without closing delimiter"
    )
    messages = [
        {
            "role": "system",
            "content": f"Harness\n{RUN_BINDING_START}\nno end\n\n{partial_contract}",
        }
    ]
    with pytest.raises(BindingError) as exc:
        refresh_segment_run_binding(messages, _config())
    assert exc.value.code == RUN_BINDING_AMBIGUOUS


def test_refresh_appends_when_no_block():
    messages = [{"role": "system", "content": "Harness catalog"}]
    out = refresh_segment_run_binding(messages, {"mode": "workflow", "output": None})
    assert messages[0]["content"] == "Harness catalog"
    assert out[0]["content"].startswith("Harness catalog")
    assert out[0]["content"].count(RUN_BINDING_START) == 1
    assert _binding_block() in out[0]["content"]


def test_refresh_replaces_single_block():
    old = _wrap("Mode: stale")
    messages = [{"role": "system", "content": f"Harness\n\n{old}"}]
    out = refresh_segment_run_binding(messages, {"mode": "working_copy", "output": None})
    assert "Mode: stale" not in out[0]["content"]
    assert "Mode: working_copy" in out[0]["content"]
    assert out[0]["content"].count(RUN_BINDING_START) == 1


def test_refresh_collapses_multiple_blocks():
    first = _wrap("first block")
    second = _wrap("second block")
    messages = [
        {
            "role": "system",
            "content": f"Harness\n\n{first}\n\nTail\n\n{second}\n\nEnd",
        }
    ]
    out = refresh_segment_run_binding(messages, {"mode": "workflow", "output": None})
    content = out[0]["content"]
    assert content.count(RUN_BINDING_START) == 1
    assert "first block" not in content
    assert "second block" not in content
    assert content.index(RUN_BINDING_START) < content.index("Tail")
    assert "End" in content


@pytest.mark.parametrize(
    "content",
    [
        f"{RUN_BINDING_START}\nno end",
        f"no start\n{RUN_BINDING_END}",
        f"{RUN_BINDING_START}\n{RUN_BINDING_START}\n{RUN_BINDING_END}\n{RUN_BINDING_END}",
        f"{RUN_BINDING_END}\n{RUN_BINDING_START}",
    ],
)
def test_refresh_rejects_malformed_markers(content):
    messages = [{"role": "system", "content": content}]
    with pytest.raises(BindingError) as exc:
        refresh_segment_run_binding(messages, {"mode": "workflow", "output": None})
    assert exc.value.code == RUN_BINDING_AMBIGUOUS


def test_refresh_prepends_when_no_system_message():
    messages = [{"role": "user", "content": "hello"}]
    out = refresh_segment_run_binding(messages, {"mode": "workflow", "output": None})
    assert out[0]["role"] == "system"
    assert RUN_BINDING_START in out[0]["content"]
    assert out[1]["content"] == "hello"


def test_refresh_does_not_mutate_caller_messages():
    original = [{"role": "system", "content": "stable"}]
    snapshot = [{"role": "system", "content": "stable"}]
    refresh_segment_run_binding(original, {"mode": "workflow", "output": None})
    assert original == snapshot


def test_refresh_preserves_non_system_messages():
    messages = [
        {"role": "system", "content": "Harness"},
        {"role": "user", "content": "Do work"},
        {"role": "assistant", "content": "OK"},
    ]
    out = refresh_segment_run_binding(messages, {"mode": "workflow", "output": None})
    assert out[1:] == messages[1:]


def test_existing_service_workspace_without_token(tmp_path, monkeypatch):
    monkeypatch.setattr(wm.Config, "WORKSPACES_ROOT", str(tmp_path / "roots"))
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "AGENTS.md").write_text("present")
    config = {
        "input": {
            "type": "shared_folder",
            "uri": "/ignored/for-existing",
            "relativePath": ".",
        },
        "mode": "workflow",
        "inPlace": False,
    }

    async def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("provision must not run when workspace exists")

    monkeypatch.setattr(wm, "provision", _fail_if_called)
    resolved = asyncio.run(
        ensure_input_workspace("exec-existing", config, workspace_path=str(sandbox))
    )
    assert resolved == str(sandbox.resolve())


def test_missing_service_workspace_reprovisions_from_config_input(tmp_path, monkeypatch):
    monkeypatch.setattr(wm.Config, "WORKSPACES_ROOT", str(tmp_path / "roots"))
    src = tmp_path / "playbook"
    src.mkdir()
    (src / "note.md").write_text("src")
    config = {
        "input": {
            "type": "shared_folder",
            "uri": str(src),
            "relativePath": ".",
        },
        "mode": "workflow",
        "inPlace": False,
    }
    resolved = asyncio.run(
        ensure_input_workspace(
            "exec-reprovision",
            config,
            workspace_path=str(tmp_path / "missing"),
        )
    )
    assert Path(resolved).is_dir()
    assert (Path(resolved) / "note.md").read_text() == "src"
    assert str(src.resolve()) != Path(resolved).resolve()


def test_missing_service_workspace_applies_relative_path(tmp_path, monkeypatch):
    monkeypatch.setattr(wm.Config, "WORKSPACES_ROOT", str(tmp_path / "roots"))
    src = tmp_path / "playbook"
    nested = src / "pkg"
    nested.mkdir(parents=True)
    (nested / "play.md").write_text("play")
    config = {
        "input": {
            "type": "shared_folder",
            "uri": str(src),
            "relativePath": "pkg",
        },
        "mode": "workflow",
        "inPlace": False,
    }
    resolved = asyncio.run(
        ensure_input_workspace("exec-relative", config, workspace_path=None)
    )
    assert Path(resolved).name == "pkg"
    assert (Path(resolved) / "play.md").read_text() == "play"


def test_missing_inplace_workspace_fails_without_provision(tmp_path, monkeypatch):
    async def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("provision must not run for in-place input")

    monkeypatch.setattr(wm, "provision", _fail_if_called)
    config = {
        "input": {
            "type": "shared_folder",
            "uri": str(tmp_path / "caller"),
            "relativePath": ".",
            "materialization": "in_place_read_only",
        },
        "mode": "workflow",
        "inPlace": True,
    }
    with pytest.raises(BindingError) as exc:
        asyncio.run(
            ensure_input_workspace(
                "exec-inplace",
                config,
                workspace_path=str(tmp_path / "gone"),
            )
        )
    assert exc.value.code == INPUT_WORKSPACE_MISSING


@pytest.mark.parametrize(
    "status,token,expected",
    [
        (401, None, INPUT_CREDENTIAL_REQUIRED),
        (403, None, INPUT_CREDENTIAL_REQUIRED),
        (401, "secret-token", INPUT_FORBIDDEN),
        (403, "secret-token", INPUT_FORBIDDEN),
    ],
)
def test_download_auth_errors_are_secret_free(status, token, expected, tmp_path):
    dest = tmp_path / "dest"

    class _FakeResponse:
        status_code = status

        def raise_for_status(self):
            request = httpx.Request("GET", "https://example.com/private/pkg.zip")
            response = httpx.Response(status, request=request)
            raise httpx.HTTPStatusError("denied", request=request, response=response)

    class _FakeStream:
        async def __aenter__(self):
            return _FakeResponse()

        async def __aexit__(self, *_args):
            return False

    class _FakeClient:
        def stream(self, *_args, **_kwargs):
            return _FakeStream()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    with patch("app.services.workspace_manager.httpx.AsyncClient", return_value=_FakeClient()):
        with pytest.raises(wm.WorkspaceError) as exc:
            asyncio.run(
                wm._download_and_extract(
                    "https://example.com/private/pkg.zip",
                    dest,
                    [],
                    input_access_token=token,
                )
            )
    assert exc.value.code == expected
    assert "secret-token" not in exc.value.message
    assert "example.com" not in exc.value.message


def test_ensure_input_workspace_maps_auth_errors(tmp_path, monkeypatch):
    config = {
        "input": {
            "type": "url",
            "uri": "https://example.com/private/pkg.zip",
            "relativePath": ".",
        },
        "mode": "workflow",
        "inPlace": False,
    }
    monkeypatch.setattr(
        wm,
        "provision",
        AsyncMock(
            side_effect=wm.WorkspaceError(
                "input access denied",
                code=INPUT_CREDENTIAL_REQUIRED,
            )
        ),
    )
    with pytest.raises(BindingError) as exc:
        asyncio.run(
            ensure_input_workspace("exec-auth", config, workspace_path=None)
        )
    assert exc.value.code == INPUT_CREDENTIAL_REQUIRED
    assert "example.com" not in exc.value.message
    assert "secret" not in exc.value.message


def test_ensure_input_workspace_maps_generic_provision_failure(tmp_path, monkeypatch):
    config = {
        "input": {
            "type": "url",
            "uri": "https://example.com/private/pkg.zip",
            "relativePath": ".",
        },
        "mode": "workflow",
        "inPlace": False,
    }
    monkeypatch.setattr(
        wm,
        "provision",
        AsyncMock(side_effect=wm.WorkspaceError("network down")),
    )
    with pytest.raises(BindingError) as exc:
        asyncio.run(
            ensure_input_workspace("exec-fail", config, workspace_path=None)
        )
    assert exc.value.code == INPUT_PROVISION_FAILED
    assert "example.com" not in exc.value.message


def test_git_input_token_is_not_put_in_process_arguments(tmp_path, monkeypatch):
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
            input_access_token="secret-token",
        )
    )

    assert "secret-token" not in " ".join(captured["args"])
    assert captured["env"]["GIT_CONFIG_VALUE_0"].endswith("secret-token")
