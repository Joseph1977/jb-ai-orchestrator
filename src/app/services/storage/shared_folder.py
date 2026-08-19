# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Confined shared-folder durable output."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from app.config import Config
from app.models.bindings import (
    STORAGE_LIMIT_EXCEEDED,
    STORAGE_NOT_FOUND,
    STORAGE_PATH_INVALID,
    STORAGE_UNAVAILABLE,
)
from app.services.storage.base import StorageError, StorageListResult


def _logical(path: str) -> str:
    raw = str(path or ".").replace("\\", "/")
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise StorageError(STORAGE_PATH_INVALID, "output path must be relative and confined")
    parts = [part for part in candidate.parts if part not in ("", ".")]
    if parts and parts[0] == ".agent":
        raise StorageError(STORAGE_PATH_INVALID, ".agent paths belong to service runtime")
    return "/".join(parts)


class SharedFolderBackend:
    """Atomic UTF-8 writes; concurrent writers use last-writer-wins."""

    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser().resolve()

    def _resolve(self, path: str) -> Path:
        rel = _logical(path)
        target = (self.root / rel).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise StorageError(STORAGE_PATH_INVALID, "output path escapes its binding") from exc
        return target

    async def exists(self, path: str) -> bool:
        target = self._resolve(path)
        return await asyncio.to_thread(target.is_file)

    async def read_text(self, path: str) -> str:
        target = self._resolve(path)

        def _read() -> str:
            if not target.is_file():
                raise StorageError(STORAGE_NOT_FOUND, "output file was not found")
            if target.stat().st_size > Config.OUTPUT_READ_MAX_BYTES:
                raise StorageError(STORAGE_LIMIT_EXCEEDED, "output file exceeds the read limit")
            try:
                return target.read_text(encoding="utf-8")
            except OSError as exc:
                raise StorageError(STORAGE_UNAVAILABLE, "output storage is unavailable") from exc

        return await asyncio.to_thread(_read)

    async def write_text(self, path: str, content: str) -> int:
        target = self._resolve(path)
        payload = content.encode("utf-8")
        if len(payload) > Config.OUTPUT_WRITE_MAX_BYTES:
            raise StorageError(STORAGE_LIMIT_EXCEEDED, "output content exceeds the write limit")

        def _write() -> int:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp_name: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=target.parent, prefix=".output-", delete=False
                ) as tmp:
                    temp_name = tmp.name
                    tmp.write(payload)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(temp_name, target)
                return len(payload)
            except OSError as exc:
                raise StorageError(STORAGE_UNAVAILABLE, "output storage is unavailable") from exc
            finally:
                if temp_name:
                    try:
                        Path(temp_name).unlink(missing_ok=True)
                    except OSError:
                        pass

        return await asyncio.to_thread(_write)

    async def list(self, path: str = ".", *, next_token: str | None = None) -> StorageListResult:
        del next_token
        target = self._resolve(path)

        def _list() -> StorageListResult:
            if not target.exists():
                return StorageListResult(entries=[])
            if target.is_file():
                return StorageListResult(entries=[_logical(path)])
            entries: list[str] = []
            for item in sorted(target.rglob("*")):
                rel = item.relative_to(self.root).as_posix()
                if rel == ".agent" or rel.startswith(".agent/"):
                    continue
                entries.append(rel + ("/" if item.is_dir() else ""))
                if len(entries) >= Config.OUTPUT_LIST_MAX_ENTRIES:
                    break
            return StorageListResult(entries=entries)

        return await asyncio.to_thread(_list)
