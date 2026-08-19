# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Build one output backend per execute/resume segment."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from app.models.bindings import OUTPUT_CREDENTIAL_REQUIRED, LocationType
from app.services.binding_contract import apply_relative_inside
from app.services.storage.azure_blob import AzureBlobBackend
from app.services.storage.base import StorageBackend, StorageError
from app.services.storage.shared_folder import SharedFolderBackend


def build_output_backend(
    binding: Optional[dict[str, Any]],
    *,
    output_access_token: Optional[str] = None,
) -> Optional[StorageBackend]:
    if not binding:
        return None
    kind = LocationType(binding["type"])
    uri = str(binding["uri"])
    relative = binding.get("relativePath") or "."
    if kind == LocationType.SHARED_FOLDER:
        parsed = urlparse(uri)
        local_uri = unquote(parsed.path) if parsed.scheme == "file" else uri
        root = apply_relative_inside(str(Path(local_uri).expanduser().resolve()), relative)
        return SharedFolderBackend(root)
    if kind == LocationType.AZURE_BLOB:
        if not output_access_token:
            raise StorageError(
                OUTPUT_CREDENTIAL_REQUIRED,
                "output credentials are required for azure_blob",
            )
        suffix = "" if relative in ("", ".") else "/" + str(relative).strip("/")
        return AzureBlobBackend(uri.rstrip("/") + suffix, access_token=output_access_token)
    raise StorageError("OUTPUT_TYPE_NOT_ENABLED", "output provider is not enabled")
