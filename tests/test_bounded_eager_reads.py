# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Eager and root-instruction reads are bounded at the file handle.

Slicing after ``read_text()`` is not a cap: the whole file is already resident
by then, so a hostile multi-gigabyte rule exhausts memory before the check
runs. These tests fail if any of these paths ever requests an unbounded read.

Instrumentation wraps ``os.fdopen`` rather than replacing the opener, so the
real descriptor-anchored traversal, ``O_NOFOLLOW`` and ``fstat`` checks all
still run underneath the measurement.
"""

import os

import pytest

from app.services.harness.base import (
    MAX_EAGER_FILE_CHARS,
    RootInstructionError,
    RootInstructionTooLarge,
    RootInstructionUnreadable,
)
from harness_helpers import (
    read_capped,
    read_eager_rule,
    read_root_instructions,
    read_text_capped,
)


class _RecordingHandle:
    """Fails the test if the caller ever requests an unbounded read."""

    def __init__(self, handle, sizes):
        self._handle = handle
        self._sizes = sizes

    def read(self, size=-1):
        if size is None or size < 0:
            raise AssertionError("unbounded read of an eager file")
        self._sizes.append(size)
        return self._handle.read(size)

    def close(self):
        self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def instrument(monkeypatch):
    """Record every requested read size across the real open path."""
    sizes: list[int] = []
    real_fdopen = os.fdopen

    def recording_fdopen(fd, *args, **kwargs):
        return _RecordingHandle(real_fdopen(fd, *args, **kwargs), sizes)

    monkeypatch.setattr(os, "fdopen", recording_fdopen)
    return sizes


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- the core bounded reader -------------------------------------------------


def test_read_capped_reports_overflow_without_reading_everything(tmp_path, monkeypatch):
    path = write(tmp_path, "big.md", "x" * 10_000)
    sizes = instrument(monkeypatch)

    text, overflowed = read_capped(path, 100)

    assert overflowed is True
    assert len(text) == 100
    assert max(sizes) <= 101, "read must be bounded by cap + 1"


def test_read_capped_at_exactly_the_cap_is_not_overflow(tmp_path):
    path = write(tmp_path, "exact.md", "y" * 100)
    text, overflowed = read_capped(path, 100)
    assert overflowed is False
    assert text == "y" * 100


def test_read_capped_reports_unreadable_distinctly_from_empty(tmp_path):
    # None and ("", False) are different answers: one file cannot be read, the
    # other is simply empty and still deserves a catalog entry.
    assert read_capped(tmp_path / "missing.md", 100) is None
    empty = write(tmp_path, "empty.md", "")
    assert read_capped(empty, 100) == ("", False)


# --- truncating variant, for content a clip does not invalidate --------------


def test_read_text_capped_is_bounded_and_marks_truncation(tmp_path, monkeypatch):
    path = write(tmp_path, "readme.md", "z" * 50_000)
    sizes = instrument(monkeypatch)

    text = read_text_capped(path, cap=200)

    assert text.startswith("z" * 200)
    assert "[truncated at 200 chars]" in text
    assert max(sizes) <= 201


def test_read_text_capped_leaves_small_files_alone(tmp_path):
    path = write(tmp_path, "small.md", "short body")
    assert read_text_capped(path, cap=200) == "short body"


# --- rule bodies are whole or dropped, never clipped -------------------------


def test_oversized_rule_is_dropped_whole_rather_than_clipped(tmp_path, monkeypatch):
    path = write(tmp_path, "rule.mdc", "r" * (MAX_EAGER_FILE_CHARS * 3))
    sizes = instrument(monkeypatch)

    body, overflowed = read_eager_rule(path)

    assert overflowed is True
    assert body == "", "a partial rule must never reach the model"
    assert max(sizes) <= MAX_EAGER_FILE_CHARS + 1


def test_rule_within_the_cap_is_returned_intact(tmp_path):
    path = write(tmp_path, "ok.mdc", "always follow this")
    body, overflowed = read_eager_rule(path)
    assert overflowed is False
    assert body == "always follow this"


# --- root instructions fail explicitly, and bounded ---------------------------


def test_oversized_root_instructions_raise_without_full_read(tmp_path, monkeypatch):
    path = write(tmp_path, "AGENTS.md", "a" * 5_000)
    sizes = instrument(monkeypatch)

    with pytest.raises(RootInstructionTooLarge) as excinfo:
        read_root_instructions(path, cap=1_000)

    assert "refusing silent truncation" in str(excinfo.value)
    assert excinfo.value.code == "ROOT_INSTRUCTIONS_TOO_LARGE"
    assert max(sizes) <= 1_001


def test_unreadable_root_instructions_are_not_reported_as_oversized(tmp_path):
    # A permissions or I/O failure sends the operator to a different fix than
    # an over-budget playbook, so the two carry different codes.
    fifo = tmp_path / "AGENTS.md"
    os.mkfifo(fifo)

    with pytest.raises(RootInstructionUnreadable) as excinfo:
        read_root_instructions(fifo, cap=1_000)

    assert excinfo.value.code == "ROOT_INSTRUCTIONS_UNREADABLE"
    assert isinstance(excinfo.value, RootInstructionError)


def test_root_instructions_within_budget_load_whole(tmp_path):
    path = write(tmp_path, "AGENTS.md", "# Project\n\nDo the thing.\n")
    assert read_root_instructions(path, cap=1_000) == "# Project\n\nDo the thing.\n"


def test_missing_root_instructions_are_empty_not_an_error(tmp_path):
    assert read_root_instructions(tmp_path / "AGENTS.md") == ""
