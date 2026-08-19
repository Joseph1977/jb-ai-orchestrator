# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Validate, sanitize, and shim input/output bindings (Phase 1 contract)."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from pathlib import Path

from app.config import Config
from app.models.bindings import (
    AGUI_INPUT_NOT_SUPPORTED,
    BINDING_CREDENTIAL_NOT_ALLOWED,
    BINDING_URI_HAS_CREDENTIALS,
    INPUT_REQUIRED,
    INPUT_TYPE_NOT_ENABLED,
    INPUT_TYPES_V1,
    INPUT_URI_REQUIRED,
    INVALID_BRANCH,
    INVALID_MATERIALIZATION,
    INVALID_MODE_MATERIALIZATION,
    INVALID_RELATIVE_PATH,
    OUTPUT_FEATURE_NOT_ENABLED,
    OUTPUT_TYPE_NOT_ENABLED,
    OUTPUT_TYPES_V1,
    OUTPUT_URI_REQUIRED,
    ExecutionMode,
    LocationBinding,
    LocationType,
    Materialization,
)
from app.services.workspace_manager import SourceKind, classify_source


class BindingError(Exception):
    """Stable binding-contract failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    def as_detail(self) -> dict[str, str]:
        return {"errorCode": self.code, "error": self.message}


_CREDENTIAL_QUERY_KEYS = frozenset(
    {
        "sig",
        "signature",
        "sv",
        "se",
        "sp",
        "spr",
        "sastoken",
        "sas",
        "token",
        "access_token",
        "accesstoken",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "secret",
        "client_secret",
        "shared_access_signature",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "awsaccesskeyid",
        "password",
    }
)
_SECRET_CONFIG_KEYS = frozenset(
    {
        "accesstoken",
        "access_token",
        "inputaccesstoken",
        "outputaccesstoken",
        "credentials",
        "sastoken",
        "sas",
        "password",
        "token",
    }
)


def _normalized_sensitive_key(key: Any) -> str:
    """Normalize credential key spelling across case and separators."""
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


_NORMALIZED_CREDENTIAL_QUERY_KEYS = frozenset(
    _normalized_sensitive_key(key) for key in _CREDENTIAL_QUERY_KEYS
)
_NORMALIZED_SECRET_CONFIG_KEYS = frozenset(
    _normalized_sensitive_key(key) for key in _SECRET_CONFIG_KEYS
)


def uri_contains_credentials(uri: str) -> bool:
    """True when *uri* embeds userinfo or a SAS/auth query parameter."""
    if not uri or not str(uri).strip():
        return False
    parsed = urlparse(str(uri).strip())
    if parsed.username or parsed.password:
        return True
    for key in parse_qs(parsed.query, keep_blank_values=True):
        if _normalized_sensitive_key(key) in _NORMALIZED_CREDENTIAL_QUERY_KEYS:
            return True
    return False


def _require_uri(uri: Optional[str], *, code: str) -> str:
    value = (uri or "").strip()
    if not value:
        raise BindingError(code, "uri is required and must be a location, not a credential")
    if uri_contains_credentials(value):
        raise BindingError(
            BINDING_URI_HAS_CREDENTIALS,
            "uri must not contain SAS, auth query parameters, or embedded user credentials",
        )
    return value


def normalize_git_ref(ref: Optional[str]) -> Optional[str]:
    """Return a safe git branch or tag name, or ``None`` when omitted."""
    if ref is None:
        return None
    value = ref.strip()
    if not value:
        raise BindingError(INVALID_BRANCH, "branch must be a non-empty git ref")
    if len(value) > 255:
        raise BindingError(INVALID_BRANCH, "branch exceeds maximum length")
    if value.startswith("-"):
        raise BindingError(INVALID_BRANCH, "branch must not start with '-'")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise BindingError(INVALID_BRANCH, "branch must not contain control characters")
    if any(ch.isspace() for ch in value):
        raise BindingError(INVALID_BRANCH, "branch must not contain whitespace")
    if any(ch in "~^:?*[" for ch in value):
        raise BindingError(INVALID_BRANCH, "branch contains a forbidden git ref character")
    if ".." in value:
        raise BindingError(INVALID_BRANCH, "branch must not contain '..'")
    if "@{" in value:
        raise BindingError(INVALID_BRANCH, "branch must not contain '@{'")
    if "\\" in value:
        raise BindingError(INVALID_BRANCH, "branch must not contain backslash")
    if value.endswith("/") or value.endswith("."):
        raise BindingError(INVALID_BRANCH, "branch must not end with '/' or '.'")
    parts = value.split("/")
    if any(not part for part in parts):
        raise BindingError(
            INVALID_BRANCH,
            "branch must not contain empty path components",
        )
    if value == "@" or any(part.startswith(".") for part in parts):
        raise BindingError(INVALID_BRANCH, "branch contains an invalid git ref component")
    if any(part.endswith(".lock") for part in parts):
        raise BindingError(INVALID_BRANCH, "branch must not contain '.lock' components")
    return value


def normalize_relative_path(rel: Optional[str]) -> str:
    """Return a confined relative path (no absolute, no ``..``)."""
    value = (rel or ".").strip() or "."
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or Path(value).is_absolute():
        raise BindingError(INVALID_RELATIVE_PATH, "relativePath must be a relative path")
    parts = [p for p in Path(normalized).parts if p not in ("", ".")]
    if ".." in parts:
        raise BindingError(INVALID_RELATIVE_PATH, "relativePath must not contain '..'")
    if not parts:
        return "."
    return "/".join(parts)


def apply_relative_inside(root: str, relative_path: Optional[str]) -> str:
    """Resolve ``relativePath`` inside an already-materialized workspace."""
    from app.services.workspace_manager import WorkspaceError, resolve_within

    rel = normalize_relative_path(relative_path)
    if rel == ".":
        return root
    try:
        return resolve_within(root, rel)
    except WorkspaceError as exc:
        raise BindingError(INVALID_RELATIVE_PATH, str(exc)) from exc


def select_relative_workspace(root: str, relative_path: Optional[str]) -> str:
    """Apply ``relativePath`` and require the result to be a directory."""
    resolved = apply_relative_inside(root, relative_path)
    if not Path(resolved).is_dir():
        raise BindingError(
            INVALID_RELATIVE_PATH,
            "relativePath must exist as a directory inside the materialized input",
        )
    return resolved


def sanitize_binding(binding: LocationBinding) -> dict[str, Any]:
    """Persistable binding: location only."""
    payload: dict[str, Any] = {
        "type": binding.type.value,
        "uri": binding.uri.strip(),
        "relativePath": normalize_relative_path(binding.relative_path),
    }
    if binding.materialization is not None:
        payload["materialization"] = binding.materialization.value
    if binding.branch is not None:
        payload["branch"] = binding.branch
    return payload


def strip_secrets_from_mapping(value: Any) -> Any:
    """Recursively drop credential keys from a config mapping."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if _normalized_sensitive_key(key) in _NORMALIZED_SECRET_CONFIG_KEYS:
                continue
            cleaned[key] = strip_secrets_from_mapping(item)
        return cleaned
    if isinstance(value, list):
        return [strip_secrets_from_mapping(item) for item in value]
    return value


def sanitize_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove rejected input values and exception objects from visible errors."""
    sanitized: list[dict[str, Any]] = []
    for error in errors:
        visible = {
            key: strip_secrets_from_mapping(value)
            for key, value in error.items()
            if key not in {"input", "ctx"}
        }
        sanitized.append(visible)
    return sanitized


def stable_binding_validation_error(
    body: Any,
    errors: list[dict[str, Any]],
    *,
    gate_output: bool,
) -> Optional[dict[str, str]]:
    """Map wire validation failures to secret-free stable contract errors."""
    if gate_output and isinstance(body, dict) and body.get("output") is not None:
        return {
            "errorCode": OUTPUT_FEATURE_NOT_ENABLED,
            "error": "output bindings are not enabled on this deployment yet",
        }
    if any(
        BINDING_CREDENTIAL_NOT_ALLOWED in str(error.get("msg", ""))
        for error in errors
    ):
        return {
            "errorCode": BINDING_CREDENTIAL_NOT_ALLOWED,
            "error": "credentials belong in credentials.*",
        }
    return None


def validate_input_binding(
    binding: LocationBinding,
    *,
    mode: ExecutionMode,
) -> LocationBinding:
    uri = _require_uri(binding.uri, code=INPUT_URI_REQUIRED)
    if binding.type not in INPUT_TYPES_V1:
        raise BindingError(
            INPUT_TYPE_NOT_ENABLED,
            f"input type '{binding.type.value}' is not enabled",
        )
    materialization = binding.materialization
    if binding.type in (LocationType.GIT, LocationType.URL):
        if materialization == Materialization.IN_PLACE_READ_ONLY:
            raise BindingError(
                INVALID_MATERIALIZATION,
                "git and url inputs always use copy materialization",
            )
        materialization = Materialization.COPY
    elif materialization is None:
        materialization = Materialization.COPY

    if mode == ExecutionMode.WORKING_COPY and materialization == Materialization.IN_PLACE_READ_ONLY:
        raise BindingError(
            INVALID_MODE_MATERIALIZATION,
            "working_copy cannot be combined with in_place_read_only",
        )

    branch: Optional[str] = None
    if binding.branch is not None:
        if binding.type != LocationType.GIT:
            raise BindingError(
                INVALID_BRANCH,
                "branch is only valid for git input",
            )
        branch = normalize_git_ref(binding.branch)

    return LocationBinding(
        type=binding.type,
        uri=uri,
        relativePath=normalize_relative_path(binding.relative_path),
        materialization=materialization,
        branch=branch,
    )


def validate_output_binding(binding: LocationBinding) -> LocationBinding:
    uri = _require_uri(binding.uri, code=OUTPUT_URI_REQUIRED)
    if binding.type not in OUTPUT_TYPES_V1:
        raise BindingError(
            OUTPUT_TYPE_NOT_ENABLED,
            f"output type '{binding.type.value}' is not enabled",
        )
    if binding.branch is not None:
        raise BindingError(
            INVALID_BRANCH,
            "branch is not allowed on output bindings",
        )
    return LocationBinding(
        type=binding.type,
        uri=uri,
        relativePath=normalize_relative_path(binding.relative_path),
        materialization=binding.materialization,
    )


def assert_output_feature_enabled(output: Optional[LocationBinding]) -> None:
    """Reject output unless the Phase 2 feature is explicitly enabled."""
    if output is None:
        return
    if not Config.OUTPUT_BINDINGS_ENABLED:
        raise BindingError(
            OUTPUT_FEATURE_NOT_ENABLED,
            "output bindings are not enabled on this deployment yet",
        )
    validate_output_binding(output)


def assert_agui_phase1_input(binding: LocationBinding) -> None:
    """AG-UI supports shared-folder copy and read-only in-place inputs."""
    if binding.type != LocationType.SHARED_FOLDER:
        raise BindingError(
            AGUI_INPUT_NOT_SUPPORTED,
            "AG-UI Phase 1 only supports shared_folder input",
        )


def input_from_folder(folder: str, *, in_place: bool) -> tuple[LocationBinding, bool]:
    """Map legacy ``folder`` (+ ``inPlace``) to a synthetic input binding.

    Returns ``(binding, legacy_writable_in_place)``.
    """
    if not folder or not str(folder).strip():
        raise BindingError(INPUT_REQUIRED, "folder or input.uri is required")
    source = str(folder).strip()
    if uri_contains_credentials(source):
        raise BindingError(
            BINDING_URI_HAS_CREDENTIALS,
            "uri must not contain SAS, auth query parameters, or embedded user credentials",
        )
    kind = classify_source(source)
    if kind == SourceKind.GIT_URL:
        loc_type = LocationType.GIT
        materialization = Materialization.COPY
        legacy = False
    elif kind == SourceKind.SHARED_FOLDER_URL:
        loc_type = LocationType.URL
        materialization = Materialization.COPY
        legacy = False
    else:
        loc_type = LocationType.SHARED_FOLDER
        # Legacy inPlace is writable-in-place, not the new read-only materialization.
        materialization = Materialization.COPY
        legacy = bool(in_place)
    binding = LocationBinding(
        type=loc_type,
        uri=source,
        relative_path=".",
        materialization=materialization,
    )
    return binding, legacy


def input_from_workspace_path(
    workspace_path: str, *, in_place: bool
) -> tuple[LocationBinding, bool]:
    """Map legacy AG-UI ``workspacePath`` to a synthetic shared_folder input."""
    return input_from_folder(workspace_path, in_place=in_place)


def resolve_initiate_input(
    *,
    input_binding: Optional[LocationBinding],
    folder: Optional[str],
    in_place: bool,
    mode: Optional[ExecutionMode],
) -> tuple[LocationBinding, ExecutionMode, bool]:
    """Resolve new-contract input or legacy folder. Default mode is workflow."""
    resolved_mode = mode or ExecutionMode.WORKFLOW
    legacy_writable = False

    if input_binding is not None:
        validated = validate_input_binding(input_binding, mode=resolved_mode)
        return validated, resolved_mode, False

    if folder:
        # Legacy writable in-place keeps working_copy so current clients do not
        # lose writes when Phase 2 enforces workflow read-only input.
        if in_place and mode is None:
            resolved_mode = ExecutionMode.WORKING_COPY
        synthetic, legacy_writable = input_from_folder(folder, in_place=in_place)
        validated = validate_input_binding(synthetic, mode=resolved_mode)
        return validated, resolved_mode, legacy_writable

    raise BindingError(INPUT_REQUIRED, "input or folder is required")


def provision_source(binding: LocationBinding) -> str:
    """Remote/local URI for the provisioner. ``relativePath`` is applied after copy."""
    return binding.uri.strip()


def provision_branch(binding: LocationBinding) -> Optional[str]:
    """Git ref for shallow clone when input type is git."""
    if binding.type != LocationType.GIT:
        if binding.branch is not None:
            raise BindingError(INVALID_BRANCH, "branch is only valid for git input")
        return None
    return normalize_git_ref(binding.branch)


def should_provision_in_place(binding: LocationBinding, *, legacy_writable: bool) -> bool:
    if legacy_writable:
        return True
    return binding.materialization == Materialization.IN_PLACE_READ_ONLY


def sanitize_execution_config(
    *,
    input_binding: LocationBinding,
    mode: ExecutionMode,
    output_binding: Optional[LocationBinding] = None,
    runtime_path: Optional[str] = None,
    legacy_writable_in_place: bool = False,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """JSON-safe execution config. Tokens are never included."""
    config: dict[str, Any] = {
        "input": sanitize_binding(input_binding),
        "mode": mode.value,
        "legacyWritableInPlace": bool(legacy_writable_in_place),
        "inPlace": should_provision_in_place(
            input_binding, legacy_writable=legacy_writable_in_place
        ),
    }
    config["output"] = (
        sanitize_binding(output_binding)
        if output_binding is not None and Config.OUTPUT_BINDINGS_ENABLED
        else None
    )
    if runtime_path:
        config["runtimePath"] = runtime_path
    if extra:
        extra_clean = strip_secrets_from_mapping(extra)
        if isinstance(extra_clean, dict):
            for key, value in extra_clean.items():
                if key in ("input", "output", "mode"):
                    continue
                config[key] = value
    return strip_secrets_from_mapping(config)


def synthetic_input_from_config(config: Optional[dict[str, Any]], source: Optional[str]) -> dict[str, Any]:
    """Load-time shim for execution rows that predate sanitized bindings."""
    cfg = strip_secrets_from_mapping(dict(config or {}))
    if isinstance(cfg.get("input"), dict) and cfg["input"].get("uri"):
        return cfg
    folder = source or ""
    in_place = bool(cfg.get("inPlace", False))
    if folder:
        binding, legacy = input_from_folder(folder, in_place=in_place)
        mode = ExecutionMode(cfg["mode"]) if cfg.get("mode") else (
            ExecutionMode.WORKING_COPY if legacy else ExecutionMode.WORKFLOW
        )
        merged = sanitize_execution_config(
            input_binding=binding,
            mode=mode,
            runtime_path=cfg.get("runtimePath"),
            legacy_writable_in_place=legacy,
            extra={k: v for k, v in cfg.items() if k not in ("input", "output", "mode")},
        )
        return merged
    return cfg
