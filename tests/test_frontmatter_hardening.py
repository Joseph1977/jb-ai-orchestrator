# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Bounded, non-recursive frontmatter scanning."""

import os

import pytest
import yaml

from app.services.harness.base import (
    MAX_FRONTMATTER_SCAN_CHARS,
    FrontmatterStatus,
)
from tests.harness_helpers import (
    first_description,
    parse_frontmatter_fields,
    scan_path as scan_primitive,
)


class _RecordingHandle:
    """Wraps a real handle; fails if the caller requests an unbounded read."""

    def __init__(self, handle, sizes):
        self._handle = handle
        self._sizes = sizes

    def read(self, size=-1):
        if size is None or size < 0:
            raise AssertionError("unbounded read of a capability file")
        self._sizes.append(size)
        return self._handle.read(size)

    def close(self):
        self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def instrument_reads(monkeypatch):
    """Record every read size, leaving the real secure opener in place."""
    sizes: list[int] = []
    real_fdopen = os.fdopen

    def recording_fdopen(fd, *args, **kwargs):
        return _RecordingHandle(real_fdopen(fd, *args, **kwargs), sizes)

    monkeypatch.setattr(os, "fdopen", recording_fdopen)
    return sizes


def instrument_opens(monkeypatch):
    """Record each file actually opened for reading."""
    opens: list[int] = []
    real_fdopen = os.fdopen

    def counting_fdopen(fd, *args, **kwargs):
        opens.append(fd)
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", counting_fdopen)
    return opens


# --- malformed input never aborts discovery ---------------------------------


def test_deeply_nested_sequence_degrades_without_raising(tmp_path):
    """RecursionError is a RuntimeError, so `except yaml.YAMLError` misses it.

    Nesting reaches the interpreter limit at roughly 1,000 characters, well
    inside the scan window, so this is reachable from any capability file.
    """
    payload = "description: " + "[" * 500 + "]" * 500
    assert len(payload) < MAX_FRONTMATTER_SCAN_CHARS

    with pytest.raises(RecursionError):
        yaml.safe_load(payload)

    path = write(tmp_path, "nested.md", f"---\n{payload}\n---\n# Fallback\n")
    scan = scan_primitive(path)
    assert scan.status is FrontmatterStatus.MALFORMED
    assert scan.metadata == {}
    assert parse_frontmatter_fields(path) == {}


def test_malformed_file_does_not_break_sibling_discovery(tmp_path):
    from app.services.harness.registry import collect_manifest

    skills = tmp_path / ".claude" / "skills"
    (skills / "broken").mkdir(parents=True)
    (skills / "broken" / "SKILL.md").write_text(
        "---\ndescription: " + "[" * 500 + "]" * 500 + "\n---\n", encoding="utf-8"
    )
    (skills / "healthy").mkdir(parents=True)
    (skills / "healthy" / "SKILL.md").write_text(
        "---\nname: healthy\ndescription: Still discovered\n---\n", encoding="utf-8"
    )
    (tmp_path / "CLAUDE.md").write_text("# Playbook\n", encoding="utf-8")

    manifest = collect_manifest(str(tmp_path))
    names = {s.name: s.description for s in manifest.skills}
    assert names["healthy"] == "Still discovered"
    assert "broken" in names


# --- alias graphs are rejected by shape, never walked ------------------------


def test_alias_expansion_is_not_walked(tmp_path):
    """A 289-char payload expands to ~43M nodes under any recursive walk.

    safe_load itself is cheap because PyYAML shares alias references; the cost
    lands entirely on the consumer. Validation must reject by shape.
    """
    payload = 'a: &a ["x","x","x","x","x","x","x","x","x"]\n'
    for i in range(1, 8):
        prev, cur = chr(96 + i), chr(97 + i)
        payload += f"{cur}: &{cur} [" + ",".join(["*" + prev] * 9) + "]\n"
    # Anchors must precede the alias, otherwise YAML errors out and the file is
    # rejected as malformed before the validator is ever reached.
    path = write(tmp_path, "aliases.md", f"---\nname: aliased\n{payload}globs: *h\n---\n")

    scan = scan_primitive(path)
    assert scan.status is FrontmatterStatus.OK, "payload must load, so the validator is exercised"
    # globs resolves to a nested list, not a list of strings, so it is dropped.
    assert scan.metadata.get("globs") is None
    assert scan.name == "aliased"
    assert all(isinstance(v, (str, bool, tuple)) for v in scan.metadata.values())


def test_self_referential_scope_is_rejected(tmp_path):
    path = write(tmp_path, "recursive.md", "---\nname: r\nglobs: &G\n  - *G\n---\n")
    scan = scan_primitive(path)
    assert scan.metadata.get("globs") is None
    assert scan.name == "r"


def test_scope_accepts_string_and_string_list(tmp_path):
    single = write(tmp_path, "s.md", '---\nname: s\nglobs: "**/*.ts"\n---\n')
    multi = write(tmp_path, "m.md", '---\nname: m\nglobs: ["**/*.ts", "**/*.tsx"]\n---\n')
    assert scan_primitive(single).metadata["globs"] == ("**/*.ts",)
    assert scan_primitive(multi).metadata["globs"] == ("**/*.ts", "**/*.tsx")


def test_metadata_holds_only_whitelisted_shapes(tmp_path):
    path = write(
        tmp_path,
        "w.md",
        "---\nname: w\ndescription: d\nalwaysApply: true\nglobs: ['*.py']\n"
        "unexpected: {a: 1}\nalso: [1, 2]\n---\n",
    )
    meta = scan_primitive(path).metadata
    assert set(meta) == {"name", "description", "alwaysApply", "globs"}
    for value in meta.values():
        assert isinstance(value, (str, bool, tuple))
    with pytest.raises(TypeError):
        meta["injected"] = "nope"  # type: ignore[index]


def test_always_apply_requires_a_real_boolean(tmp_path):
    truthy = write(tmp_path, "t.md", "---\nname: t\nalwaysApply: 'yes please'\n---\n")
    real = write(tmp_path, "r.md", "---\nname: r\nalwaysApply: true\n---\n")
    assert "alwaysApply" not in scan_primitive(truthy).metadata
    assert scan_primitive(real).metadata["alwaysApply"] is True


# --- truncated metadata reports itself instead of leaking --------------------


def test_frontmatter_beyond_the_window_returns_empty_not_dashes(tmp_path):
    filler = "x" * (MAX_FRONTMATTER_SCAN_CHARS + 200)
    path = write(tmp_path, "big.md", f"---\nname: big\nfiller: {filler}\n---\n# Real Title\n")
    scan = scan_primitive(path)
    assert scan.status is FrontmatterStatus.TRUNCATED
    assert scan.description == ""
    assert first_description(path) != "---"


def test_unterminated_leading_comment_does_not_leak(tmp_path):
    body = "secret internal note " * 500
    path = write(tmp_path, "c.md", f"<!--\n{body}\n-->\n---\nname: c\n---\n")
    scan = scan_primitive(path)
    assert scan.status is FrontmatterStatus.TRUNCATED
    assert scan.description == ""
    assert "secret internal note" not in scan.description


def test_terminated_comment_is_still_stripped(tmp_path):
    path = write(
        tmp_path,
        "ok.md",
        "\ufeff<!--\nSkill: help\n-->\n\n---\nname: help\ndescription: >\n  Show commands.\n---\n",
    )
    scan = scan_primitive(path)
    assert scan.status is FrontmatterStatus.OK
    assert scan.description == "Show commands."


# --- the read itself is bounded ---------------------------------------------


def test_metadata_read_is_bounded_at_the_handle(tmp_path, monkeypatch):
    """Assert the requested size, not the call count.

    A single `read_text()` of a 5 MB file is one call, so a call-count test
    would pass the very bug this guards.
    """
    huge = "---\nname: huge\ndescription: Bounded\n---\n" + ("y" * 5_000_000)
    path = write(tmp_path, "huge.md", huge)

    sizes = instrument_reads(monkeypatch)

    scan = scan_primitive(path)
    assert scan.description == "Bounded"
    assert sizes, "expected the scanner to read through the file handle"
    # cap + 1: the extra character is what distinguishes "exactly at the
    # window" from "larger than it" without a second metadata lookup.
    assert max(sizes) <= MAX_FRONTMATTER_SCAN_CHARS + 1


def test_scan_reads_each_file_once(tmp_path, monkeypatch):
    path = write(tmp_path, "once.md", "---\nname: once\ndescription: One read\n---\n")
    opens = instrument_opens(monkeypatch)

    scan = scan_primitive(path)
    assert scan.name == "once"
    assert scan.description == "One read"
    assert len(opens) == 1


# --- only a leading unterminated comment suppresses metadata -----------------


def test_unterminated_comment_after_frontmatter_keeps_the_scan(tmp_path):
    """A stray marker in prose must not discard metadata already parsed."""
    path = write(
        tmp_path,
        "stray.md",
        "---\nname: stray\ndescription: Still readable\n---\n\n# Body\n\n<!-- never closed\n",
    )

    scan = scan_primitive(path)

    assert scan.status is FrontmatterStatus.OK
    assert scan.name == "stray"
    assert scan.description == "Still readable"


def test_unterminated_leading_comment_still_marks_truncation(tmp_path):
    path = write(tmp_path, "lead.md", "<!-- opening comment that never closes\nname: hidden\n")

    scan = scan_primitive(path)

    assert scan.status is FrontmatterStatus.TRUNCATED
    assert scan.metadata == {}


def test_closed_leading_comment_does_not_block_frontmatter(tmp_path):
    path = write(
        tmp_path,
        "closed.md",
        "<!-- licence header -->\n---\nname: ok\ndescription: Parsed\n---\n# Body\n",
    )

    scan = scan_primitive(path)

    assert scan.status is FrontmatterStatus.OK
    assert scan.name == "ok"
    assert scan.description == "Parsed"


def test_closed_leading_comment_then_stray_marker_still_parses(tmp_path):
    path = write(
        tmp_path,
        "both.md",
        "<!-- header -->\n---\nname: both\ndescription: Fine\n---\n<!-- dangling\n",
    )

    scan = scan_primitive(path)

    assert scan.status is FrontmatterStatus.OK
    assert scan.description == "Fine"
