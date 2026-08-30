# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Input/output location bindings, session mode, and transient credentials."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enum import Enum


class LocationType(str, Enum):
    GIT = "git"
    URL = "url"
    SHARED_FOLDER = "shared_folder"
    AZURE_BLOB = "azure_blob"
    S3 = "s3"
    GCS = "gcs"


class Materialization(str, Enum):
    COPY = "copy"
    IN_PLACE_READ_ONLY = "in_place_read_only"


class ExecutionMode(str, Enum):
    WORKFLOW = "workflow"
    WORKING_COPY = "working_copy"


_BINDING_SECRET_KEYS = frozenset(
    {
        "accesstoken",
        "access_token",
        "inputaccesstoken",
        "outputaccesstoken",
        "sastoken",
        "sas",
    }
)
_NORMALIZED_BINDING_SECRET_KEYS = frozenset(
    key.replace("_", "").replace("-", "").lower()
    for key in _BINDING_SECRET_KEYS
)


class LocationBinding(BaseModel):
    """Caller-supplied location. Credentials are not allowed on this object."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        hide_input_in_errors=True,
    )

    type: LocationType
    uri: str
    relative_path: Optional[str] = Field(default=".", alias="relativePath")
    materialization: Optional[Materialization] = None
    branch: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def reject_credential_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in data:
                normalized = str(key).replace("_", "").replace("-", "").lower()
                if normalized in _NORMALIZED_BINDING_SECRET_KEYS:
                    raise ValueError(
                        "BINDING_CREDENTIAL_NOT_ALLOWED: credentials belong in credentials.*"
                    )
        return data


class TransientCredentials(BaseModel):
    """Per-segment tokens. Never persisted or logged."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        hide_input_in_errors=True,
    )

    input_access_token: Optional[str] = Field(default=None, alias="inputAccessToken")
    output_access_token: Optional[str] = Field(default=None, alias="outputAccessToken")


INPUT_TYPES_V1 = frozenset(
    {LocationType.GIT, LocationType.URL, LocationType.SHARED_FOLDER}
)
OUTPUT_TYPES_V1 = frozenset({LocationType.SHARED_FOLDER, LocationType.AZURE_BLOB})

INPUT_REQUIRED = "INPUT_REQUIRED"
INPUT_URI_REQUIRED = "INPUT_URI_REQUIRED"
OUTPUT_URI_REQUIRED = "OUTPUT_URI_REQUIRED"
BINDING_URI_HAS_CREDENTIALS = "BINDING_URI_HAS_CREDENTIALS"
BINDING_CREDENTIAL_NOT_ALLOWED = "BINDING_CREDENTIAL_NOT_ALLOWED"
INVALID_MATERIALIZATION = "INVALID_MATERIALIZATION"
INVALID_MODE_MATERIALIZATION = "INVALID_MODE_MATERIALIZATION"
INVALID_RELATIVE_PATH = "INVALID_RELATIVE_PATH"
INVALID_BRANCH = "INVALID_BRANCH"
INPUT_TYPE_NOT_ENABLED = "INPUT_TYPE_NOT_ENABLED"
OUTPUT_TYPE_NOT_ENABLED = "OUTPUT_TYPE_NOT_ENABLED"
OUTPUT_FEATURE_NOT_ENABLED = "OUTPUT_FEATURE_NOT_ENABLED"
OUTPUT_CREDENTIAL_REQUIRED = "OUTPUT_CREDENTIAL_REQUIRED"
OUTPUT_FORBIDDEN = "OUTPUT_FORBIDDEN"
OUTPUT_BINDING_REPLACEMENT_NOT_ALLOWED = "OUTPUT_BINDING_REPLACEMENT_NOT_ALLOWED"
INPUT_CREDENTIAL_REQUIRED = "INPUT_CREDENTIAL_REQUIRED"
STORAGE_NOT_FOUND = "STORAGE_NOT_FOUND"
STORAGE_PATH_INVALID = "STORAGE_PATH_INVALID"
STORAGE_LIMIT_EXCEEDED = "STORAGE_LIMIT_EXCEEDED"
STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
AGUI_INPUT_NOT_SUPPORTED = "AGUI_INPUT_NOT_SUPPORTED"
SESSION_CLOSED = "SESSION_CLOSED"
SESSION_CLOSING = "SESSION_CLOSING"
INPUT_WORKSPACE_MISSING = "INPUT_WORKSPACE_MISSING"
INPUT_FORBIDDEN = "INPUT_FORBIDDEN"
INPUT_PROVISION_FAILED = "INPUT_PROVISION_FAILED"
INPUT_BINDING_REPLACEMENT_NOT_ALLOWED = "INPUT_BINDING_REPLACEMENT_NOT_ALLOWED"
RUN_BINDING_AMBIGUOUS = "RUN_BINDING_AMBIGUOUS"
ROOT_INSTRUCTIONS_TOO_LARGE = "ROOT_INSTRUCTIONS_TOO_LARGE"
ROOT_INSTRUCTIONS_UNREADABLE = "ROOT_INSTRUCTIONS_UNREADABLE"
