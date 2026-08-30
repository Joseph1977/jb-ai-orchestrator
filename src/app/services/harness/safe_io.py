# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Workspace-bound file access for harness discovery.

A workspace is untrusted input. Discovery must not be walked or read out of it
by a crafted symlink, and the guarantee cannot live in the directory walker:
adapters enumerate their own locations, and root instructions are read without
walking anything at all. So containment lives here, at the only place discovery
is allowed to open a file.

The design point is that **there is no separate validation step to race
against**. Rather than resolving a path, checking the result, and reopening the
original, every path is opened one component at a time relative to a directory
descriptor anchored on the workspace, with ``O_NOFOLLOW`` on each component.
The open *is* the check. ``O_NOFOLLOW`` constrains only the final component of
whatever path it is given, which is exactly why components are fed in one at a
time -- it is what extends the guarantee to symlinked *parents*, not just
symlinked leaves.

Only one descriptor is held at a time. Keeping one per level would make
correctness depend on the process descriptor limit, which a deep enough tree
could exhaust; re-anchoring per operation costs O(depth) extra opens and keeps
descriptor use constant.

Linux and Python 3.11 are the supported runtime. The reader refuses to
construct where the required primitives are missing rather than silently
falling back to path-based opens.
"""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Optional, TextIO

from app.utils.logger import logger

# Components that can never appear in a workspace-relative path. Rejected
# lexically, before any syscall, so a traversal attempt never reaches the
# filesystem at all.
_FORBIDDEN_COMPONENTS = frozenset({"", ".", ".."})

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

# O_NONBLOCK so a hostile FIFO cannot block discovery in open() before fstat
# has had the chance to reject it for not being a regular file.
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | _CLOEXEC | _NONBLOCK
_DIR_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | _CLOEXEC
# The anchor is the trust boundary itself rather than something inside it, so
# it is not required to be a non-symlink. Everything below it is.
_ANCHOR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC


class UnsafePathError(Exception):
    """A path was rejected before or during opening."""


class ReaderUnavailableError(Exception):
    """The platform lacks the primitives required to read safely."""


def platform_supports_safe_io() -> bool:
    return os.open in os.supports_dir_fd and os.scandir in os.supports_fd


@dataclass(frozen=True)
class WorkspacePath:
    """A path held only as validated components relative to the workspace.

    Never absolute and never reconstructed from ``DirEntry.path``. Discovery
    builds these by joining entry names, so the string that gets opened is
    always one this type has already vetted.
    """

    parts: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> "WorkspacePath":
        """Build from a relative POSIX-ish string, rejecting unsafe shapes."""
        normalized = str(text).replace("\\", "/")
        if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
            raise UnsafePathError(f"Absolute path rejected: {text!r}")
        parts = tuple(p for p in normalized.split("/") if p != "")
        return cls(()).extend(parts)

    def extend(self, names) -> "WorkspacePath":
        for name in names:
            self = self.child(name)
        return self

    def child(self, name: str) -> "WorkspacePath":
        if name in _FORBIDDEN_COMPONENTS or "/" in name or "\\" in name or "\0" in name:
            raise UnsafePathError(f"Unsafe path component: {name!r}")
        return WorkspacePath(self.parts + (name,))

    @property
    def parent(self) -> "WorkspacePath":
        return WorkspacePath(self.parts[:-1])

    @property
    def name(self) -> str:
        return self.parts[-1] if self.parts else ""

    @property
    def stem(self) -> str:
        name = self.name
        return name.rsplit(".", 1)[0] if "." in name else name

    @property
    def posix(self) -> str:
        return "/".join(self.parts)

    def is_root(self) -> bool:
        return not self.parts

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.posix or "."


# Test seam. Called with the WorkspacePath after its parent directory has been
# traversed and immediately before the leaf is opened, so a swap-to-symlink can
# be staged deterministically instead of raced against a thread.
_before_leaf_open: Optional[Callable[[WorkspacePath], None]] = None


def set_before_leaf_open(hook: Optional[Callable[[WorkspacePath], None]]) -> None:
    global _before_leaf_open
    _before_leaf_open = hook


class WorkspaceReader:
    """Opens files only through the workspace, one validated component deep."""

    def __init__(self, workspace: Path) -> None:
        if not platform_supports_safe_io():
            raise ReaderUnavailableError(
                "Safe workspace reads need os.open(dir_fd=) and os.scandir(fd); "
                "refusing to fall back to path-based opens"
            )
        self.workspace = workspace
        self._anchor: Optional[int] = os.open(str(workspace), _ANCHOR_FLAGS)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._anchor is not None:
            try:
                os.close(self._anchor)
            finally:
                self._anchor = None

    def __enter__(self) -> "WorkspaceReader":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @property
    def _anchor_fd(self) -> int:
        if self._anchor is None:
            raise UnsafePathError("Reader is closed")
        return self._anchor

    # -- traversal ---------------------------------------------------------

    def _open_dir(self, wp: WorkspacePath) -> int:
        """Descriptor for ``wp``, refusing a symlink at any component."""
        fd = os.dup(self._anchor_fd)
        try:
            for part in wp.parts:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = nxt
            return fd
        except BaseException:
            os.close(fd)
            raise

    def scandir(self, wp: WorkspacePath) -> List[os.DirEntry]:
        """Entries of ``wp`` sorted by name; unreadable or unsafe yields none.

        Best effort by design: one subtree we cannot read must not abort
        discovery for the whole workspace.
        """
        fd = None
        try:
            fd = self._open_dir(wp)
            with os.scandir(fd) as scan:
                return sorted(scan, key=lambda entry: entry.name)
        except OSError as exc:
            logger.warning("Skipping unreadable directory %s: %s", wp, exc)
            return []
        finally:
            if fd is not None:
                os.close(fd)

    def is_dir(self, wp: WorkspacePath) -> bool:
        """True when ``wp`` is a real directory reachable without a symlink."""
        if wp.is_root():
            return True
        fd = None
        try:
            fd = self._open_dir(wp)
            return True
        except OSError:
            return False
        finally:
            if fd is not None:
                os.close(fd)

    # -- reading -----------------------------------------------------------

    @contextmanager
    def open_text(self, wp: WorkspacePath) -> Iterator[TextIO]:
        """Open a regular file inside the workspace, or raise.

        Rejects a symlink at any component including the leaf, and anything
        that is not a regular file.
        """
        if wp.is_root():
            raise UnsafePathError("Refusing to open the workspace root as a file")
        dir_fd = self._open_dir(wp.parent)
        try:
            if _before_leaf_open is not None:
                _before_leaf_open(wp)
            fd = os.open(wp.name, _FILE_FLAGS, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
        handle: Optional[TextIO] = None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise UnsafePathError(f"Not a regular file: {wp}")
            handle = os.fdopen(fd, "r", encoding="utf-8", errors="replace")
        except BaseException:
            if handle is None:
                os.close(fd)
            raise
        try:
            yield handle
        finally:
            handle.close()

    def read_capped(self, wp: WorkspacePath, cap: int) -> Optional[tuple[str, bool]]:
        """Read at most ``cap + 1`` chars. ``None`` means unreadable or unsafe.

        The extra character separates "exactly at the cap" from "over it"
        without a second metadata lookup. ``None`` is distinct from ``("",
        False)``: an empty file is catalogued with fallback metadata, an
        unreadable or unsafe one is skipped entirely.
        """
        try:
            with self.open_text(wp) as handle:
                chunk = handle.read(cap + 1)
        except (OSError, UnsafePathError) as exc:
            logger.warning("Skipping unreadable file %s: %s", wp, exc)
            return None
        if len(chunk) > cap:
            return chunk[:cap], True
        return chunk, False

    def read_strict(self, wp: WorkspacePath, cap: int) -> tuple[str, bool]:
        """Like ``read_capped`` but surfaces the failure instead of logging it."""
        with self.open_text(wp) as handle:
            chunk = handle.read(cap + 1)
        if len(chunk) > cap:
            return chunk[:cap], True
        return chunk, False

    def exists(self, wp: WorkspacePath) -> bool:
        """True when ``wp`` is a regular file safely reachable in the workspace."""
        try:
            with self.open_text(wp):
                return True
        except (OSError, UnsafePathError):
            return False
