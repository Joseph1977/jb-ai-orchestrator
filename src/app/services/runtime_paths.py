# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Service-owned runtime root for ``.agent/**`` (offload, todos)."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Set

from app.config import Config
from app.services.workspace_manager import CleanupOutcome, WorkspaceError, resolve_within

_AGENT_PREFIX = ".agent"
_OFFLOAD_LOGICAL_PREFIX = f"{_AGENT_PREFIX}/offload"


def normalize_rel(rel_path: str) -> str:
    if not rel_path:
        return ""
    normalized = str(rel_path).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def is_agent_runtime_rel(rel_path: str) -> bool:
    """True when *rel_path* is the reserved logical ``.agent`` tree."""
    normalized = normalize_rel(rel_path)
    return normalized == _AGENT_PREFIX or normalized.startswith(f"{_AGENT_PREFIX}/")


def strip_agent_prefix(rel_path: str) -> str:
    normalized = normalize_rel(rel_path)
    if normalized == _AGENT_PREFIX:
        return "."
    if normalized.startswith(f"{_AGENT_PREFIX}/"):
        return normalized[len(_AGENT_PREFIX) + 1 :]
    return normalized


def workspaces_root() -> Path:
    return Path(Config.WORKSPACES_ROOT).expanduser().resolve()


def is_confined_under_workspaces(path: str | Path) -> bool:
    """True when *path* resolves inside ``WORKSPACES_ROOT``."""
    try:
        Path(path).expanduser().resolve().relative_to(workspaces_root())
    except ValueError:
        return False
    return True


def runtime_root_for_execution(execution_id: str | uuid.UUID) -> Path:
    return workspaces_root() / str(execution_id) / "runtime"


def runtime_root_for_thread(thread_id: str) -> Path:
    digest = hashlib.sha256((thread_id or "thread").encode("utf-8")).hexdigest()
    return workspaces_root() / "threads" / digest / "runtime"


def ensure_runtime(
    *,
    execution_id: Optional[str | uuid.UUID] = None,
    thread_id: Optional[str] = None,
) -> str:
    """Create and return the service-owned runtime directory."""
    if execution_id:
        root = runtime_root_for_execution(execution_id)
    elif thread_id:
        root = runtime_root_for_thread(thread_id)
    else:
        raise WorkspaceError("runtime_path requires execution_id or thread_id")
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def resolve_tool_path(workspace_path: str, rel_path: str, *, runtime_path: Optional[str]) -> str:
    """Resolve a local-tool path: ``.agent/**`` → runtime, else input workspace."""
    if is_agent_runtime_rel(rel_path):
        if not runtime_path:
            raise WorkspaceError(
                "runtime_path is required for reserved logical .agent paths"
            )
        return resolve_within(runtime_path, strip_agent_prefix(rel_path))
    return resolve_within(workspace_path, rel_path)


def logical_from_runtime(runtime_path: str, absolute: str) -> str:
    """Map a physical runtime file back to a logical ``.agent/...`` path."""
    rel = Path(absolute).resolve().relative_to(Path(runtime_path).resolve())
    display = str(rel).replace("\\", "/")
    if display in (".", ""):
        return _AGENT_PREFIX
    return f"{_AGENT_PREFIX}/{display}"


def offload_filename_from_logical(logical_path: str) -> Optional[str]:
    """Map ``.agent/offload/foo.txt`` to the physical filename ``foo.txt``."""
    normalized = normalize_rel(logical_path)
    if normalized == _OFFLOAD_LOGICAL_PREFIX:
        return None
    prefix = f"{_OFFLOAD_LOGICAL_PREFIX}/"
    if not normalized.startswith(prefix):
        return None
    remainder = normalized[len(prefix) :]
    if not remainder or remainder.endswith("/") or remainder in (".", ".."):
        return None
    parts = [part for part in remainder.split("/") if part]
    if len(parts) != 1 or parts[0] in (".", ".."):
        return None
    return parts[0]


def _offload_dir(runtime_path: Path) -> Path:
    return runtime_path / "offload"


def _is_confined_under(parent: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class OffloadCleanupResult:
    runtime_path: str
    referenced_count: int
    deleted_count: int
    preserved_count: int
    skipped_outside: int


@dataclass(frozen=True)
class RuntimeDeletionResult:
    outcome: CleanupOutcome
    runtime_path: Optional[str]
    existed: bool

    @property
    def deleted(self) -> bool:
        return self.outcome is CleanupOutcome.SUCCESS

    @property
    def complete(self) -> bool:
        return self.outcome.complete


def cleanup_unreferenced_offloads(
    runtime_path: str,
    *,
    referenced_logical_paths: Iterable[str],
) -> OffloadCleanupResult:
    """Delete orphan files under ``runtime/offload``; preserve referenced files."""
    runtime = Path(runtime_path).expanduser().resolve()
    if not is_confined_under_workspaces(runtime):
        return OffloadCleanupResult(
            runtime_path=str(runtime),
            referenced_count=0,
            deleted_count=0,
            preserved_count=0,
            skipped_outside=0,
        )

    referenced_names: Set[str] = set()
    for logical in referenced_logical_paths:
        name = offload_filename_from_logical(logical)
        if name:
            referenced_names.add(name)

    offload_dir = _offload_dir(runtime)
    if not offload_dir.is_dir():
        return OffloadCleanupResult(
            runtime_path=str(runtime),
            referenced_count=len(referenced_names),
            deleted_count=0,
            preserved_count=0,
            skipped_outside=0,
        )

    deleted = 0
    preserved = 0
    skipped_outside = 0
    for entry in offload_dir.iterdir():
        if entry.is_symlink():
            if not _is_confined_under(offload_dir, entry):
                skipped_outside += 1
                continue
            if entry.name in referenced_names:
                preserved += 1
                continue
            try:
                entry.unlink(missing_ok=True)
                deleted += 1
            except OSError:
                preserved += 1
            continue
        if entry.is_dir():
            continue
        if not entry.is_file():
            continue
        if not _is_confined_under(offload_dir, entry):
            skipped_outside += 1
            continue
        if entry.name in referenced_names:
            preserved += 1
            continue
        try:
            entry.unlink(missing_ok=True)
            deleted += 1
        except OSError:
            preserved += 1

    return OffloadCleanupResult(
        runtime_path=str(runtime),
        referenced_count=len(referenced_names),
        deleted_count=deleted,
        preserved_count=preserved,
        skipped_outside=skipped_outside,
    )


def _delete_runtime_tree(runtime_path: Path) -> RuntimeDeletionResult:
    resolved = runtime_path.expanduser().resolve()
    existed = resolved.exists()
    if not existed:
        return RuntimeDeletionResult(
            outcome=CleanupOutcome.ABSENT,
            runtime_path=str(resolved),
            existed=False,
        )
    if not is_confined_under_workspaces(resolved):
        return RuntimeDeletionResult(
            outcome=CleanupOutcome.NOT_REQUIRED,
            runtime_path=str(resolved),
            existed=existed,
        )
    try:
        if resolved.is_symlink():
            resolved.unlink(missing_ok=True)
        elif resolved.is_dir():
            shutil.rmtree(resolved, onerror=_best_effort_rmtree_error)
        else:
            resolved.unlink(missing_ok=True)
    except OSError:
        pass
    if resolved.exists():
        return RuntimeDeletionResult(
            outcome=CleanupOutcome.FAILURE,
            runtime_path=str(resolved),
            existed=existed,
        )
    return RuntimeDeletionResult(
        outcome=CleanupOutcome.SUCCESS,
        runtime_path=str(resolved),
        existed=existed,
    )


def _best_effort_rmtree_error(func, path, exc_info) -> None:
    del exc_info
    try:
        if func in (os.rmdir, os.remove, os.unlink):
            func(path)
    except OSError:
        pass


def delete_execution_runtime(execution_id: str | uuid.UUID) -> RuntimeDeletionResult:
    """Remove the service-owned runtime for an orchestrator execution."""
    return _delete_runtime_tree(runtime_root_for_execution(execution_id))


def delete_thread_runtime(thread_id: str) -> RuntimeDeletionResult:
    """Remove the SHA-256 keyed runtime directory for an AG-UI thread."""
    return _delete_runtime_tree(runtime_root_for_thread(thread_id))
