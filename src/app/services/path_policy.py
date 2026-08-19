# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Path and shell allow/deny policy for multi-tenant local tools.

Traversal escape is still enforced by :func:`workspace_manager.resolve_within`.
This module adds optional glob allow/deny lists, in-place root allowlisting,
and shell-command deny/allow patterns.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Optional

from app.config import Config
from app.services.workspace_manager import WorkspaceError


def _split_csv(raw: str) -> List[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _denylist() -> List[str]:
    return _split_csv(Config.PATH_DENYLIST)


def _allowlist() -> List[str]:
    return _split_csv(Config.PATH_ALLOWLIST)


def _allowed_roots() -> List[str]:
    roots: List[str] = []
    for item in _split_csv(Config.WORKSPACE_ALLOWED_ROOTS):
        try:
            roots.append(str(Path(item).expanduser().resolve()))
        except OSError:
            roots.append(str(Path(item).expanduser()))
    return roots


def _shell_denylist() -> List[str]:
    return _split_csv(Config.SHELL_COMMAND_DENYLIST)


def _shell_allowlist() -> List[str]:
    return _split_csv(Config.SHELL_COMMAND_ALLOWLIST)


def _norm_rel(relative_path: str) -> str:
    """Normalize a workspace-relative path for glob matching (posix, no leading ./)."""
    text = (relative_path or ".").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if text in ("", "."):
        return "."
    return str(PurePosixPath(text))


def _matches_any(path: str, patterns: Iterable[str]) -> bool:
    candidate = _norm_rel(path)
    name = PurePosixPath(candidate).name
    for pattern in patterns:
        pat = pattern.replace("\\", "/")
        if fnmatch.fnmatch(candidate, pat) or fnmatch.fnmatch(name, pat):
            return True
        # Also match "**/" + pattern when pattern has no slash (e.g. "*.pem").
        if "/" not in pat.rstrip("/") and fnmatch.fnmatch(candidate, f"**/{pat}"):
            return True
    return False


def check_workspace_root_allowed(abs_path: str) -> None:
    """Reject in-place workspace binding outside WORKSPACE_ALLOWED_ROOTS (when set)."""
    if not Config.PATH_POLICY_ENABLED:
        return
    roots = _allowed_roots()
    if not roots:
        return
    try:
        resolved = str(Path(abs_path).expanduser().resolve())
    except OSError as exc:
        raise WorkspaceError(f"Invalid workspace path: {abs_path}") from exc
    for root in roots:
        try:
            Path(resolved).relative_to(root)
            return
        except ValueError:
            continue
    raise WorkspaceError(
        f"Workspace path '{resolved}' is outside WORKSPACE_ALLOWED_ROOTS"
    )


def check_path_allowed(relative_path: str, *, operation: str = "access") -> None:
    """Enforce PATH_DENYLIST / PATH_ALLOWLIST against a workspace-relative path."""
    if not Config.PATH_POLICY_ENABLED:
        return
    rel = _norm_rel(relative_path)
    deny = _denylist()
    if deny and _matches_any(rel, deny):
        raise WorkspaceError(
            f"Path '{rel}' is denied by PATH_DENYLIST ({operation})"
        )
    allow = _allowlist()
    if allow and rel != "." and not _matches_any(rel, allow):
        raise WorkspaceError(
            f"Path '{rel}' is not permitted by PATH_ALLOWLIST ({operation})"
        )


def check_shell_command(command: str) -> Optional[str]:
    """Return an error message if the shell command is blocked; else None."""
    if not Config.PATH_POLICY_ENABLED:
        return None
    cmd = str(command or "")
    allow = _shell_allowlist()
    if allow:
        if not any(re.search(pat, cmd) for pat in allow):
            return "Command blocked by SHELL_COMMAND_ALLOWLIST"
    deny = _shell_denylist()
    for pat in deny:
        try:
            if re.search(pat, cmd):
                return f"Command blocked by SHELL_COMMAND_DENYLIST (matched /{pat}/)"
        except re.error:
            if pat in cmd:
                return f"Command blocked by SHELL_COMMAND_DENYLIST (matched '{pat}')"
    return None
