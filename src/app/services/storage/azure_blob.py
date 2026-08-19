# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Azure Blob durable output using the async SDK."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from app.config import Config
from app.models.bindings import (
    OUTPUT_FORBIDDEN,
    STORAGE_LIMIT_EXCEEDED,
    STORAGE_NOT_FOUND,
    STORAGE_PATH_INVALID,
    STORAGE_UNAVAILABLE,
)
from app.services.storage.base import StorageError, StorageListResult
from app.services.storage.shared_folder import _logical


class AzureBlobBackend:
    def __init__(self, uri: str, *, access_token: str) -> None:
        parsed = urlsplit(uri)
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.scheme not in ("http", "https") or not parsed.netloc or not parts:
            raise StorageError(STORAGE_PATH_INVALID, "azure output URI is invalid")
        self.account_url = f"{parsed.scheme}://{parsed.netloc}"
        self.container = parts[0]
        self.prefix = "/".join(parts[1:])
        self._credential = access_token

    def _name(self, path: str) -> str:
        rel = _logical(path)
        return "/".join(p for p in (self.prefix, rel) if p)

    def _client(self):
        from azure.storage.blob.aio import ContainerClient

        return ContainerClient(
            account_url=self.account_url,
            container_name=self.container,
            credential=self._credential,
        )

    @staticmethod
    def _raise(exc: Exception, *, not_found: bool = False) -> None:
        status = getattr(exc, "status_code", None)
        if status == 403:
            raise StorageError(OUTPUT_FORBIDDEN, "output storage denied access") from exc
        if status == 404 or not_found:
            raise StorageError(STORAGE_NOT_FOUND, "output file was not found") from exc
        raise StorageError(STORAGE_UNAVAILABLE, "output storage is unavailable") from exc

    async def exists(self, path: str) -> bool:
        client = self._client()
        try:
            blob = client.get_blob_client(self._name(path))
            await asyncio.wait_for(
                blob.get_blob_properties(), timeout=Config.AZURE_BLOB_TIMEOUT_SEC
            )
            return True
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return False
            self._raise(exc)
        finally:
            await client.close()

    async def read_text(self, path: str) -> str:
        client = self._client()
        try:
            blob = client.get_blob_client(self._name(path))
            props = await asyncio.wait_for(
                blob.get_blob_properties(), timeout=Config.AZURE_BLOB_TIMEOUT_SEC
            )
            if int(getattr(props, "size", 0) or 0) > Config.OUTPUT_READ_MAX_BYTES:
                raise StorageError(STORAGE_LIMIT_EXCEEDED, "output file exceeds the read limit")
            stream = await asyncio.wait_for(
                blob.download_blob(max_concurrency=1),
                timeout=Config.AZURE_BLOB_TIMEOUT_SEC,
            )
            payload = await asyncio.wait_for(
                stream.readall(), timeout=Config.AZURE_BLOB_TIMEOUT_SEC
            )
            if len(payload) > Config.OUTPUT_READ_MAX_BYTES:
                raise StorageError(STORAGE_LIMIT_EXCEEDED, "output file exceeds the read limit")
            return payload.decode("utf-8")
        except StorageError:
            raise
        except Exception as exc:
            self._raise(exc)
        finally:
            await client.close()

    async def write_text(self, path: str, content: str) -> int:
        payload = content.encode("utf-8")
        if len(payload) > Config.OUTPUT_WRITE_MAX_BYTES:
            raise StorageError(STORAGE_LIMIT_EXCEEDED, "output content exceeds the write limit")
        client = self._client()
        try:
            blob = client.get_blob_client(self._name(path))
            await asyncio.wait_for(
                blob.upload_blob(payload, overwrite=True),
                timeout=Config.AZURE_BLOB_TIMEOUT_SEC,
            )
            return len(payload)
        except Exception as exc:
            self._raise(exc)
        finally:
            await client.close()

    async def list(self, path: str = ".", *, next_token: str | None = None) -> StorageListResult:
        prefix = self._name(path)
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        client = self._client()
        try:
            pager = client.list_blobs(name_starts_with=prefix).by_page(
                continuation_token=next_token,
                results_per_page=Config.OUTPUT_LIST_MAX_ENTRIES,
            )
            page = await asyncio.wait_for(
                pager.__anext__(), timeout=Config.AZURE_BLOB_TIMEOUT_SEC
            )
            entries: list[str] = []
            async for item in page:
                entries.append(str(item.name))
            if self.prefix:
                base = self.prefix.rstrip("/") + "/"
                entries = [name[len(base):] if name.startswith(base) else name for name in entries]
            return StorageListResult(
                entries=entries,
                next_token=getattr(pager, "continuation_token", None),
            )
        except StopAsyncIteration:
            return StorageListResult(entries=[])
        except Exception as exc:
            self._raise(exc)
        finally:
            await client.close()
