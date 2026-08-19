# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Async durable-output storage contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


class StorageError(Exception):
    """Stable, secret-free storage failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class StorageListResult:
    entries: list[str]
    next_token: Optional[str] = None


class StorageBackend(Protocol):
    async def exists(self, path: str) -> bool: ...
    async def read_text(self, path: str) -> str: ...
    async def write_text(self, path: str, content: str) -> int: ...
    async def list(self, path: str = ".", *, next_token: Optional[str] = None) -> StorageListResult: ...
