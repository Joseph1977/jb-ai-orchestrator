# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import os
import sys
from pathlib import Path

import pytest

# Make `app` importable without installing the package.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Deterministic git identity so git_commit works in CI/sandbox.
os.environ.setdefault("GIT_AUTHOR_NAME", "Test")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
os.environ.setdefault("GIT_COMMITTER_NAME", "Test")
os.environ.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")


@pytest.fixture
def dotdirs(tmp_path):
    """Skip a test if the environment forbids creating dot-directories.

    Some seatbelt sandboxes block creating hidden dirs (.cursor/.git); such
    tests are meaningful only where that's allowed (normal CI/dev machines).
    """
    # Probe the exact names used by adapters; some sandboxes specifically
    # block creating protected config dirs like .cursor/.git/.claude.
    for name in (".cursor", ".git", ".claude"):
        probe = tmp_path / name
        try:
            probe.mkdir()
            probe.rmdir()
        except OSError:
            pytest.skip(f"environment forbids creating '{name}' directories")

