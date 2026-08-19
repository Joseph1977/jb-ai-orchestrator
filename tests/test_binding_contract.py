# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import pytest
from pydantic import ValidationError

from app.config import Config
from app.models.bindings import (
    AGUI_INPUT_NOT_SUPPORTED,
    BINDING_CREDENTIAL_NOT_ALLOWED,
    BINDING_URI_HAS_CREDENTIALS,
    INPUT_REQUIRED,
    INPUT_TYPE_NOT_ENABLED,
    INVALID_MATERIALIZATION,
    INVALID_MODE_MATERIALIZATION,
    INVALID_RELATIVE_PATH,
    OUTPUT_FEATURE_NOT_ENABLED,
    ExecutionMode,
    LocationBinding,
    LocationType,
    Materialization,
)
from app.services.binding_contract import (
    BindingError,
    apply_relative_inside,
    assert_agui_phase1_input,
    assert_output_feature_enabled,
    input_from_folder,
    normalize_relative_path,
    provision_source,
    resolve_initiate_input,
    sanitize_binding,
    sanitize_execution_config,
    sanitize_validation_errors,
    select_relative_workspace,
    stable_binding_validation_error,
    strip_secrets_from_mapping,
    synthetic_input_from_config,
    uri_contains_credentials,
    validate_input_binding,
    validate_output_binding,
)


def test_uri_rejects_sas_and_userinfo():
    assert uri_contains_credentials(
        "https://acct.blob.core.windows.net/c?sv=2023-01-01&sig=abc"
    )
    assert uri_contains_credentials("https://user:pass@github.com/org/repo.git")
    assert not uri_contains_credentials("https://github.com/org/repo.git")
    assert not uri_contains_credentials("/workspaces/pm")


def test_uri_rejects_expanded_auth_keys_but_allows_ref():
    assert uri_contains_credentials("https://example.com/c?api_key=abc")
    assert uri_contains_credentials("https://example.com/c?api-key=abc")
    assert uri_contains_credentials("https://example.com/c?apikey=abc")
    assert uri_contains_credentials(
        "https://example.com/c?SharedAccessSignature=abc"
    )
    assert uri_contains_credentials("https://example.com/c?auth=bearer")
    assert not uri_contains_credentials("https://github.com/org/repo.git?ref=main")


def test_validate_input_git_forces_copy():
    binding = LocationBinding(type=LocationType.GIT, uri="https://github.com/a/b.git")
    out = validate_input_binding(binding, mode=ExecutionMode.WORKFLOW)
    assert out.materialization == Materialization.COPY


def test_validate_input_git_rejects_in_place():
    binding = LocationBinding(
        type=LocationType.GIT,
        uri="https://github.com/a/b.git",
        materialization=Materialization.IN_PLACE_READ_ONLY,
    )
    with pytest.raises(BindingError) as exc:
        validate_input_binding(binding, mode=ExecutionMode.WORKFLOW)
    assert exc.value.code == INVALID_MATERIALIZATION


def test_working_copy_plus_in_place_read_only_rejected():
    binding = LocationBinding(
        type=LocationType.SHARED_FOLDER,
        uri="/workspaces/pm",
        materialization=Materialization.IN_PLACE_READ_ONLY,
    )
    with pytest.raises(BindingError) as exc:
        validate_input_binding(binding, mode=ExecutionMode.WORKING_COPY)
    assert exc.value.code == INVALID_MODE_MATERIALIZATION


def test_cloud_input_not_enabled():
    binding = LocationBinding(
        type=LocationType.AZURE_BLOB,
        uri="https://acct.blob.core.windows.net/container",
    )
    with pytest.raises(BindingError) as exc:
        validate_input_binding(binding, mode=ExecutionMode.WORKFLOW)
    assert exc.value.code == INPUT_TYPE_NOT_ENABLED


def test_access_token_on_binding_is_rejected():
    with pytest.raises(ValidationError, match=BINDING_CREDENTIAL_NOT_ALLOWED):
        LocationBinding.model_validate(
            {
                "type": "git",
                "uri": "https://github.com/a/b.git",
                "accessToken": "secret-pat",
            }
        )


def test_binding_validation_error_is_stable_and_secret_free():
    body = {
        "input": {
            "type": "git",
            "uri": "https://github.com/a/b.git",
            "accessToken": "secret-pat",
        }
    }
    errors = [
        {
            "type": "value_error",
            "loc": ("body", "input"),
            "msg": (
                "Value error, BINDING_CREDENTIAL_NOT_ALLOWED: "
                "credentials belong in credentials.*"
            ),
            "input": body["input"],
        }
    ]
    stable = stable_binding_validation_error(body, errors, gate_output=False)
    assert stable == {
        "errorCode": BINDING_CREDENTIAL_NOT_ALLOWED,
        "error": "credentials belong in credentials.*",
    }
    assert "secret-pat" not in str(stable)
    assert "input" not in sanitize_validation_errors(errors)[0]


def test_non_null_output_validation_always_uses_phase1_gate():
    body = {"output": {"accessToken": "secret-output"}}
    stable = stable_binding_validation_error(body, [], gate_output=True)
    assert stable == {
        "errorCode": OUTPUT_FEATURE_NOT_ENABLED,
        "error": "output bindings are not enabled on this deployment yet",
    }
    assert "secret-output" not in str(stable)


def test_sanitize_binding_has_no_credential_fields():
    binding = LocationBinding(
        type=LocationType.GIT,
        uri="https://github.com/a/b.git",
        materialization=Materialization.COPY,
    )
    payload = sanitize_binding(binding)
    assert "accessToken" not in payload
    assert payload["uri"] == "https://github.com/a/b.git"


def test_output_feature_gated_when_flag_off(monkeypatch):
    monkeypatch.setattr(Config, "OUTPUT_BINDINGS_ENABLED", False)
    output = LocationBinding(
        type=LocationType.AZURE_BLOB,
        uri="https://acct.blob.core.windows.net/container",
    )
    with pytest.raises(BindingError) as exc:
        assert_output_feature_enabled(output)
    assert exc.value.code == OUTPUT_FEATURE_NOT_ENABLED
    assert_output_feature_enabled(None)


def test_output_uri_with_sas_rejected_by_validator():
    output = LocationBinding(
        type=LocationType.AZURE_BLOB,
        uri="https://acct.blob.core.windows.net/c?sv=1&sig=nope",
    )
    with pytest.raises(BindingError) as exc:
        validate_output_binding(output)
    assert exc.value.code == BINDING_URI_HAS_CREDENTIALS


def test_relative_path_rejects_parent_and_absolute():
    with pytest.raises(BindingError) as exc:
        normalize_relative_path("../secret")
    assert exc.value.code == INVALID_RELATIVE_PATH
    with pytest.raises(BindingError) as exc:
        normalize_relative_path("/etc/passwd")
    assert exc.value.code == INVALID_RELATIVE_PATH


def test_provision_source_is_uri_only():
    binding = LocationBinding(
        type=LocationType.GIT,
        uri="https://github.com/a/b.git",
        relativePath="docs/playbook",
    )
    assert provision_source(binding) == "https://github.com/a/b.git"


def test_apply_relative_inside_sandbox(tmp_path):
    nested = tmp_path / "docs" / "playbook"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("ok")
    resolved = select_relative_workspace(str(tmp_path), "docs/playbook")
    assert resolved == apply_relative_inside(str(tmp_path), "docs/playbook")
    assert resolved.endswith("docs/playbook")
    with pytest.raises(BindingError) as exc:
        select_relative_workspace(str(tmp_path), "missing")
    assert exc.value.code == INVALID_RELATIVE_PATH


def test_folder_shim_git():
    binding, legacy = input_from_folder("https://github.com/a/b.git", in_place=False)
    assert binding.type == LocationType.GIT
    assert binding.materialization == Materialization.COPY
    assert legacy is False


def test_folder_shim_local_inplace_is_legacy_writable():
    binding, legacy = input_from_folder("/tmp/playbook", in_place=True)
    assert binding.type == LocationType.SHARED_FOLDER
    assert legacy is True


def test_resolve_requires_input_or_folder():
    with pytest.raises(BindingError) as exc:
        resolve_initiate_input(
            input_binding=None, folder=None, in_place=False, mode=None
        )
    assert exc.value.code == INPUT_REQUIRED


def test_legacy_inplace_defaults_to_working_copy():
    _binding, mode, legacy = resolve_initiate_input(
        input_binding=None,
        folder="/tmp/playbook",
        in_place=True,
        mode=None,
    )
    assert mode == ExecutionMode.WORKING_COPY
    assert legacy is True


def test_sanitize_execution_config_omits_tokens():
    binding = LocationBinding(
        type=LocationType.SHARED_FOLDER,
        uri="/workspaces/pm",
    )
    validated = validate_input_binding(binding, mode=ExecutionMode.WORKFLOW)
    config = sanitize_execution_config(
        input_binding=validated,
        mode=ExecutionMode.WORKFLOW,
        extra={"credentials": {"outputAccessToken": "nope"}, "accessToken": "x"},
    )
    dumped = str(config)
    assert "nope" not in dumped
    assert "accessToken" not in dumped
    assert config["output"] is None
    assert config["mode"] == "workflow"


def test_strip_secrets_from_nested_mapping():
    cleaned = strip_secrets_from_mapping(
        {
            "inPlace": True,
            "accessToken": "secret",
            "access-token": "hyphen-secret",
            "nested": {
                "outputAccessToken": "nope",
                "input_access_token": "underscore-secret",
                "keep": 1,
            },
        }
    )
    assert cleaned == {"inPlace": True, "nested": {"keep": 1}}


def test_synthetic_input_from_old_row():
    cfg = synthetic_input_from_config(
        {"inPlace": False, "systemContext": "hi"},
        "https://github.com/a/b.git",
    )
    assert cfg["input"]["type"] == "git"
    assert cfg["input"]["uri"] == "https://github.com/a/b.git"
    assert cfg["mode"] == "workflow"


def test_synthetic_input_existing_binding_is_sanitized():
    cfg = synthetic_input_from_config(
        {
            "input": {
                "type": "shared_folder",
                "uri": "/workspaces/pm",
                "access-token": "legacy-secret",
            },
            "credentials": {"inputAccessToken": "nested-secret"},
            "mode": "workflow",
        },
        "/workspaces/pm",
    )
    assert cfg == {
        "input": {
            "type": "shared_folder",
            "uri": "/workspaces/pm",
        },
        "mode": "workflow",
    }


def test_agui_rejects_git_but_allows_read_only_shared_folder():
    git = LocationBinding(type=LocationType.GIT, uri="https://github.com/a/b.git")
    with pytest.raises(BindingError) as exc:
        assert_agui_phase1_input(git)
    assert exc.value.code == AGUI_INPUT_NOT_SUPPORTED

    readonly = LocationBinding(
        type=LocationType.SHARED_FOLDER,
        uri="/workspaces/pm",
        materialization=Materialization.IN_PLACE_READ_ONLY,
    )
    assert_agui_phase1_input(readonly)


def test_agui_read_only_is_not_coupled_to_output_flag(monkeypatch):
    monkeypatch.setattr(Config, "OUTPUT_BINDINGS_ENABLED", False)
    readonly = LocationBinding(
        type=LocationType.SHARED_FOLDER,
        uri="/workspaces/pm",
        materialization=Materialization.IN_PLACE_READ_ONLY,
    )
    assert_agui_phase1_input(readonly)
