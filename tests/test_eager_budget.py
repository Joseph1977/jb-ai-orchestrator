# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Root instructions are a mandatory allocation; optional rules fit or drop."""

import pytest

from app.services.harness.base import (
    DEFAULT_EAGER_TOTAL_CHARS,
    EAGER_BUDGET_ENV_VAR,
    MAX_EAGER_TOTAL_CHARS,
    MAX_ROOT_INSTRUCTION_CHARS,
    HarnessManifest,
    HarnessAdapter,
    RootInstructionError,
    eager_budget_from_env,
    read_root_instructions,
)
from app.services.harness.registry import collect_manifest


def adapter():
    return HarnessAdapter()


def manifest():
    return HarnessManifest(orchestration_type="generic", detected=True, confidence=10)


def test_root_limit_equals_the_eager_budget():
    """Accepting 512,000 chars then clipping at 24,000 was self-contradictory."""
    assert MAX_ROOT_INSTRUCTION_CHARS == MAX_EAGER_TOTAL_CHARS


def test_oversized_root_instructions_fail_explicitly():
    big = "x" * 900
    with pytest.raises(RootInstructionError) as excinfo:
        adapter()._assemble_eager([("CLAUDE.md", big)], budget=500)
    assert "refusing to truncate" in str(excinfo.value)


def test_combined_root_instructions_are_one_allocation():
    """Each file fits alone; together they do not."""
    half = "x" * 400
    adapter()._assemble_eager([("CLAUDE.md", half)], budget=500)
    with pytest.raises(RootInstructionError):
        adapter()._assemble_eager(
            [("CLAUDE.md", half), ("AGENTS.md", half)], budget=500
        )


def test_root_instructions_are_never_partially_included():
    body = "ROOT " * 100
    out = adapter()._assemble_eager([("CLAUDE.md", body)], budget=2000)
    assert body.strip() in out
    assert "[truncated]" not in out


def test_optional_rules_are_omitted_whole(tmp_path):
    m = manifest()
    out = adapter()._assemble_eager(
        [("CLAUDE.md", "root")],
        [("Rule A", "A" * 100), ("Rule B", "B" * 5000)],
        budget=400,
        manifest=m,
    )
    assert "A" * 100 in out
    # Half a rule is worse than none: B appears not at all.
    assert "B" not in out.replace("Rule B", "")
    assert any("Eager budget reached" in n for n in m.notes)
    assert "Rule B" in m.notes[0]


def test_optional_omission_does_not_stop_later_smaller_rules():
    out = adapter()._assemble_eager(
        [("CLAUDE.md", "root")],
        [("Big", "B" * 5000), ("Small", "S" * 50)],
        budget=600,
    )
    assert "S" * 50 in out
    assert "B" * 5000 not in out


def test_root_instructions_win_over_optional_rules():
    m = manifest()
    root = "R" * 300
    out = adapter()._assemble_eager(
        [("CLAUDE.md", root)], [("Rule", "X" * 300)], budget=400, manifest=m
    )
    assert root in out
    assert "X" * 300 not in out


def test_read_root_instructions_rejects_beyond_budget(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text("y" * (MAX_ROOT_INSTRUCTION_CHARS + 1), encoding="utf-8")
    with pytest.raises(RootInstructionError):
        read_root_instructions(path)


def test_oversized_playbook_surfaces_rather_than_silently_clipping(tmp_path):
    """collect_manifest swallowing this would reintroduce silent truncation."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / "CLAUDE.md").write_text(
        "z" * (MAX_ROOT_INSTRUCTION_CHARS + 10), encoding="utf-8"
    )
    with pytest.raises(RootInstructionError):
        collect_manifest(str(tmp_path))


def test_budget_is_configurable(monkeypatch):
    monkeypatch.setenv(EAGER_BUDGET_ENV_VAR, "999")
    assert eager_budget_from_env() == 999


def test_unset_budget_uses_the_default(monkeypatch):
    monkeypatch.delenv(EAGER_BUDGET_ENV_VAR, raising=False)
    assert eager_budget_from_env() == DEFAULT_EAGER_TOTAL_CHARS


@pytest.mark.parametrize("bad", ["", "lots", "0", "-5", "12.5"])
def test_unusable_budget_falls_back_rather_than_failing_boot(monkeypatch, bad):
    monkeypatch.setenv(EAGER_BUDGET_ENV_VAR, bad)
    assert eager_budget_from_env() == DEFAULT_EAGER_TOTAL_CHARS
