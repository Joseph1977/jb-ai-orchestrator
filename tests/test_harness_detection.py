# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

from app.services.harness import collect_manifest, detect_adapter


def test_detect_cursor(dotdirs, tmp_path):
    (tmp_path / ".cursor" / "rules").mkdir(parents=True)
    (tmp_path / ".cursor" / "rules" / "coding.mdc").write_text("Follow style X")
    adapter, score = detect_adapter(tmp_path)
    assert adapter.type_id == "cursor"
    assert score >= 80


def test_detect_claude(dotdirs, tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / "CLAUDE.md").write_text("# Claude\ninstructions")
    adapter, _ = detect_adapter(tmp_path)
    assert adapter.type_id == "claude-code"


def test_detect_generic_fallback(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Agents\nbase playbook")
    adapter, _ = detect_adapter(tmp_path)
    assert adapter.type_id == "generic"


def test_collect_cursor_manifest(dotdirs, tmp_path):
    (tmp_path / ".cursor" / "rules").mkdir(parents=True)
    (tmp_path / ".cursor" / "rules" / "sec.mdc").write_text("Security rule body")
    (tmp_path / ".cursor" / "skills").mkdir(parents=True)
    (tmp_path / ".cursor" / "skills" / "review.md").write_text("Review skill\nDoes review")
    (tmp_path / "AGENTS.md").write_text("# Agents\nProject guidance here")

    manifest = collect_manifest(str(tmp_path))
    assert manifest.orchestration_type == "cursor"
    assert manifest.detected is True
    assert any(r.name == "sec" for r in manifest.rules)
    assert any(s.name == "review" for s in manifest.skills)
    assert "Project guidance here" in manifest.eager_context
    assert "Security rule body" in manifest.eager_context


def test_explicit_type_overrides_detection(dotdirs, tmp_path):
    # A cursor folder, but caller forces generic.
    (tmp_path / ".cursor").mkdir()
    (tmp_path / "AGENTS.md").write_text("# Agents\nx")
    manifest = collect_manifest(str(tmp_path), orchestration_type="generic")
    assert manifest.orchestration_type == "generic"
    assert manifest.detected is False


def test_unknown_type_raises(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        collect_manifest(str(tmp_path), orchestration_type="nonsense-harness")
