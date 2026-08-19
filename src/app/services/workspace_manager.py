# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Workspace provisioning for orchestration runs.

Given a `folder` reference (a git URL, a shared-folder/archive URL, or a local
path), this module materialises an isolated, per-execution workspace that the
harness (and its local tools) can operate on safely.

Design notes
------------
* Every execution gets its own sandbox under ``Config.WORKSPACES_ROOT/{id}``.
* All filesystem access performed by local tools MUST go through
  :func:`resolve_within` so a workspace can never be escaped via ``..`` or
  absolute paths.
* ``git`` operations shell out to the ``git`` CLI (no extra dependency).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tarfile
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.config import Config
from app.models.bindings import (
    INPUT_CREDENTIAL_REQUIRED,
    INPUT_FORBIDDEN,
    INPUT_PROVISION_FAILED,
)
from app.utils.logger import logger


class SourceKind(str, Enum):
    GIT_URL = "git_url"
    SHARED_FOLDER_URL = "shared_folder_url"
    LOCAL_PATH = "local_path"


@dataclass
class WorkspaceInfo:
    """Result of provisioning a workspace for an execution."""

    execution_id: str
    source: str
    source_kind: SourceKind
    path: str
    in_place: bool = False
    root: Optional[str] = None
    notes: list[str] = field(default_factory=list)


class WorkspaceError(Exception):
    """Raised when a workspace cannot be provisioned or a path is unsafe."""

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


_GIT_HOST_HINTS = ("github.com", "gitlab.com", "bitbucket.org", "dev.azure.com")
_ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar")


def classify_source(source: str) -> SourceKind:
    """Best-effort classification of a folder reference."""
    if not source or not source.strip():
        raise WorkspaceError("folder/source must be a non-empty string")

    value = source.strip()

    # Explicit git forms.
    if (
        value.endswith(".git")
        or value.startswith("git@")
        or value.startswith("git://")
        or value.startswith("git+")
        or re.match(r"^ssh://git@", value)
    ):
        return SourceKind.GIT_URL

    parsed = urlparse(value)
    scheme = parsed.scheme.lower()

    if scheme in ("http", "https"):
        # Known git hosts with a repo-looking path are treated as git.
        host = (parsed.netloc or "").lower()
        if any(hint in host for hint in _GIT_HOST_HINTS) and not value.endswith(
            _ARCHIVE_SUFFIXES
        ):
            return SourceKind.GIT_URL
        return SourceKind.SHARED_FOLDER_URL

    if scheme in ("file", "smb", "ftp", "ftps"):
        return SourceKind.SHARED_FOLDER_URL

    # UNC path (\\server\share) is a shared folder.
    if value.startswith("\\\\"):
        return SourceKind.SHARED_FOLDER_URL

    return SourceKind.LOCAL_PATH


def _execution_root(execution_id: str) -> Path:
    return Path(Config.WORKSPACES_ROOT).expanduser().resolve() / str(execution_id)


def resolve_within(workspace_path: str, relative_path: str) -> str:
    """Resolve ``relative_path`` against ``workspace_path`` with traversal guard.

    Raises :class:`WorkspaceError` if the resulting path escapes the workspace
    or violates the configured path allow/deny policy.
    """
    from app.services import path_policy

    base = Path(workspace_path).expanduser().resolve()
    candidate = (base / (relative_path or "")).resolve()
    try:
        rel = candidate.relative_to(base)
    except ValueError as exc:  # pragma: no cover - defensive
        raise WorkspaceError(
            f"Path '{relative_path}' escapes the workspace sandbox"
        ) from exc
    path_policy.check_path_allowed(str(rel), operation="access")
    return str(candidate)


async def run_git(args: list[str], cwd: Optional[str] = None, timeout: Optional[int] = None) -> tuple[int, str, str]:
    """Public wrapper to run a git command (used by local git tools)."""
    return await _run_git(args, cwd=cwd, timeout=timeout)


def _auth_headers(token: Optional[str]) -> dict[str, str]:
    if not token:
        return {}
    value = token.strip()
    if not value:
        return {}
    if " " in value:
        return {"Authorization": value}
    return {"Authorization": f"Bearer {value}"}


def _git_auth_failure(stderr: str, *, had_token: bool) -> Optional[tuple[str, str]]:
    lowered = stderr.lower()
    if not any(
        hint in lowered
        for hint in (
            "authentication failed",
            "403",
            "401",
            "access denied",
            "permission denied",
            "invalid username or password",
            "repository not found",
        )
    ):
        return None
    if had_token:
        return (INPUT_FORBIDDEN, "input access denied")
    return (INPUT_CREDENTIAL_REQUIRED, "input credentials are required")


async def _run_git(
    args: list[str],
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    *,
    env: Optional[dict[str, str]] = None,
) -> tuple[int, str, str]:
    """Run a git command, returning (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise WorkspaceError("git command timed out") from exc
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def _clone_repo(
    source: str,
    dest: Path,
    notes: list[str],
    *,
    branch: Optional[str] = None,
    input_access_token: Optional[str] = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    clone_args = ["clone", "--depth", "1"]
    if branch:
        clone_args.extend(["--branch", branch, "--single-branch"])
    clone_args.extend([source, str(dest)])
    git_env: Optional[dict[str, str]] = None
    if input_access_token:
        git_env = os.environ.copy()
        git_env["GIT_CONFIG_COUNT"] = "1"
        git_env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        git_env["GIT_CONFIG_VALUE_0"] = (
            f"Authorization: Bearer {input_access_token.strip()}"
        )
    code, _out, err = await _run_git(
        clone_args,
        timeout=Config.GIT_CLONE_TIMEOUT_SEC,
        env=git_env,
    )
    if code != 0:
        auth = _git_auth_failure(err, had_token=bool(input_access_token))
        if auth:
            raise WorkspaceError(auth[1], code=auth[0])
        raise WorkspaceError(
            "Failed to clone input repository",
            code=INPUT_PROVISION_FAILED,
        )
    notes.append("Cloned input repository (shallow) into workspace")


def _copy_tree(src: Path, dest: Path, notes: list[str]) -> None:
    if not src.exists():
        raise WorkspaceError(f"Local path does not exist: {src}")
    if src.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    else:
        shutil.copytree(src, dest, dirs_exist_ok=True)
    notes.append(f"Copied {src} into isolated workspace")


async def _download_and_extract(
    url: str,
    dest: Path,
    notes: list[str],
    *,
    input_access_token: Optional[str] = None,
) -> None:
    """Download an archive URL and extract it into ``dest``."""
    lowered = url.lower()
    if not lowered.endswith(_ARCHIVE_SUFFIXES):
        raise WorkspaceError(
            "Only git repos, local paths, file:// folders, and archive URLs "
            "(.zip/.tar.gz/.tgz/.tar) are supported for remote sources.",
            code=INPUT_PROVISION_FAILED,
        )

    dest.mkdir(parents=True, exist_ok=True)
    suffix = ".zip" if lowered.endswith(".zip") else ".tar"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = Path(tmp.name)

    headers = _auth_headers(input_access_token)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=Config.GIT_CLONE_TIMEOUT_SEC) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code in (401, 403):
                    code = (
                        INPUT_FORBIDDEN
                        if input_access_token
                        else INPUT_CREDENTIAL_REQUIRED
                    )
                    raise WorkspaceError("input access denied", code=code)
                resp.raise_for_status()
                with open(tmp_path, "wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        fh.write(chunk)

        if lowered.endswith(".zip"):
            with zipfile.ZipFile(tmp_path) as zf:
                _safe_extract_zip(zf, dest)
        else:
            mode = "r:gz" if lowered.endswith((".tar.gz", ".tgz")) else "r:"
            with tarfile.open(tmp_path, mode) as tf:
                _safe_extract_tar(tf, dest)
        notes.append("Downloaded and extracted input archive into workspace")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in (401, 403):
            code = (
                INPUT_FORBIDDEN
                if input_access_token
                else INPUT_CREDENTIAL_REQUIRED
            )
            raise WorkspaceError("input access denied", code=code) from exc
        raise WorkspaceError(
            "Failed to download input archive",
            code=INPUT_PROVISION_FAILED,
        ) from exc
    except httpx.HTTPError as exc:
        raise WorkspaceError(
            "Failed to download input archive",
            code=INPUT_PROVISION_FAILED,
        ) from exc
    finally:
        tmp_path.unlink(missing_ok=True)


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    base = dest.resolve()
    for member in zf.namelist():
        target = (dest / member).resolve()
        if not str(target).startswith(str(base)):
            raise WorkspaceError(f"Unsafe archive entry: {member}")
    zf.extractall(dest)


def _safe_extract_tar(tf: tarfile.TarFile, dest: Path) -> None:
    base = dest.resolve()
    for member in tf.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(base)):
            raise WorkspaceError(f"Unsafe archive entry: {member.name}")
    tf.extractall(dest)


async def provision(
    execution_id: str | uuid.UUID,
    source: str,
    *,
    in_place: bool = False,
    branch: Optional[str] = None,
    input_access_token: Optional[str] = None,
) -> WorkspaceInfo:
    """Provision an isolated workspace for an execution.

    * git URL           -> shallow clone into the sandbox
    * local path        -> copy into the sandbox (or use in place when allowed)
    * shared folder URL -> file:// copy or archive download+extract
    """
    execution_id = str(execution_id)
    kind = classify_source(source)
    notes: list[str] = []
    root = _execution_root(execution_id)

    if kind == SourceKind.LOCAL_PATH:
        src_path = Path(source).expanduser().resolve()
        if in_place:
            if not Config.ALLOW_INPLACE_WORKSPACE:
                raise WorkspaceError(
                    "in-place execution is disabled (set ALLOW_INPLACE_WORKSPACE=true)"
                )
            if not src_path.exists():
                raise WorkspaceError(f"Local path does not exist: {src_path}")
            from app.services import path_policy

            path_policy.check_workspace_root_allowed(str(src_path))
            notes.append(f"Operating in place on {src_path} (no copy)")
            return WorkspaceInfo(
                execution_id=execution_id,
                source=source,
                source_kind=kind,
                path=str(src_path),
                in_place=True,
                root=None,
                notes=notes,
            )
        workspace = root / "workspace"
        _copy_tree(src_path, workspace, notes)

    elif kind == SourceKind.GIT_URL:
        workspace = root / "workspace"
        await _clone_repo(
            source,
            workspace,
            notes,
            branch=branch,
            input_access_token=input_access_token,
        )

    else:  # SHARED_FOLDER_URL
        workspace = root / "workspace"
        parsed = urlparse(source)
        if parsed.scheme.lower() == "file":
            local = Path(parsed.path)
            _copy_tree(local, workspace, notes)
        elif source.startswith("\\\\"):
            _copy_tree(Path(source), workspace, notes)
        else:
            await _download_and_extract(
                source,
                workspace,
                notes,
                input_access_token=input_access_token,
            )

    _enforce_size_limit(workspace, notes)

    return WorkspaceInfo(
        execution_id=execution_id,
        source=source,
        source_kind=kind,
        path=str(workspace),
        in_place=False,
        root=str(root),
        notes=notes,
    )


def _dir_size_mb(path: Path) -> float:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(dirpath, name)
            try:
                total += os.path.getsize(fp)
            except OSError:
                continue
    return total / (1024 * 1024)


def _enforce_size_limit(workspace: Path, notes: list[str]) -> None:
    if Config.MAX_WORKSPACE_MB and Config.MAX_WORKSPACE_MB > 0:
        size_mb = _dir_size_mb(workspace)
        if size_mb > Config.MAX_WORKSPACE_MB:
            shutil.rmtree(workspace, ignore_errors=True)
            raise WorkspaceError(
                f"Workspace exceeds MAX_WORKSPACE_MB ({size_mb:.1f}MB > {Config.MAX_WORKSPACE_MB}MB)"
            )


class CleanupOutcome(str, Enum):
    """Filesystem cleanup disposition for close/reconcile."""

    ABSENT = "absent"
    NOT_REQUIRED = "not_required"
    SUCCESS = "success"
    FAILURE = "failure"

    @property
    def complete(self) -> bool:
        return self is not CleanupOutcome.FAILURE


@dataclass(frozen=True)
class WorkspaceCleanupResult:
    outcome: CleanupOutcome
    count: int = 0

    @property
    def deleted(self) -> bool:
        return self.outcome is CleanupOutcome.SUCCESS

    @property
    def complete(self) -> bool:
        return self.outcome.complete


def _is_under_workspaces_root(path: Path) -> bool:
    ws_root = Path(Config.WORKSPACES_ROOT).expanduser().resolve()
    try:
        path.resolve().relative_to(ws_root)
    except ValueError:
        return False
    return True


def _not_required_workspace_cleanup() -> WorkspaceCleanupResult:
    return WorkspaceCleanupResult(outcome=CleanupOutcome.NOT_REQUIRED, count=0)


def cleanup_execution_sandbox(
    execution_id: str | uuid.UUID,
    *,
    config: Optional[dict] = None,
    workspace_path: Optional[str] = None,
    origin: Optional[str] = None,
    allow_agui_origin: bool = False,
) -> WorkspaceCleanupResult:
    """Delete a service-owned execution sandbox when ownership checks pass."""
    execution_id = str(execution_id)
    cfg = config or {}
    if cfg.get("inPlace") or cfg.get("legacyWritableInPlace"):
        return _not_required_workspace_cleanup()

    if origin == "agui":
        if not allow_agui_origin:
            return _not_required_workspace_cleanup()
    elif origin not in (None, "orchestrator"):
        return _not_required_workspace_cleanup()

    root = _execution_root(execution_id)
    if not _is_under_workspaces_root(root):
        return _not_required_workspace_cleanup()

    expected_workspace = (root / "workspace").resolve()
    if workspace_path:
        candidate = Path(workspace_path).expanduser().resolve()
        if not _is_under_workspaces_root(candidate):
            return _not_required_workspace_cleanup()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return _not_required_workspace_cleanup()

    if not root.exists():
        return WorkspaceCleanupResult(outcome=CleanupOutcome.ABSENT, count=0)

    if not expected_workspace.exists() and workspace_path is None:
        return WorkspaceCleanupResult(outcome=CleanupOutcome.ABSENT, count=0)

    try:
        shutil.rmtree(root, onerror=_best_effort_rmtree_error)
    except OSError:
        pass
    if root.exists():
        return WorkspaceCleanupResult(outcome=CleanupOutcome.FAILURE, count=0)
    return WorkspaceCleanupResult(outcome=CleanupOutcome.SUCCESS, count=1)


def _best_effort_rmtree_error(func, path, exc_info) -> None:
    del exc_info
    try:
        if func in (os.rmdir, os.remove, os.unlink):
            func(path)
    except OSError:
        pass


def cleanup(execution_id: str | uuid.UUID) -> None:
    """Remove a service-created execution root after provisioning failure."""
    root = _execution_root(str(execution_id))
    if _is_under_workspaces_root(root):
        shutil.rmtree(root, ignore_errors=True)
