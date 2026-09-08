# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

import pytest

from app.services.prompt_loader import PromptTemplateError, clear_cache, load_prompt


def setup_function():
    clear_cache()


def test_load_output_binding_template():
    filled = load_prompt(
        "output_binding",
        mode="workflow",
        output_type="azure_blob",
        logical_prefix="projects/demo",
        write_output_tool="write_output",
        edit_output_tool="edit_output",
        read_output_tool="read_output",
        list_output_tool="list_output",
        read_file_tool="read_file_local",
    )
    assert "azure_blob" in filled["system"]
    assert "run-binding:start" in filled["system"]
    assert "edit_output" in filled["system"]
    assert "https://" not in filled["system"]
    assert "sas" not in filled["system"].lower()


def test_unresolved_placeholder_fails():
    with pytest.raises(PromptTemplateError, match="Unresolved"):
        load_prompt("output_unbound")


def test_leftover_mustache_fails(tmp_path, monkeypatch):
    from app.services import prompt_loader as pl

    (tmp_path / "odd.yaml").write_text(
        "name: odd\nsystem: |\n  keep={{mode}} leftover={{not-a-word}}\nprompt: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pl, "_templates_dir", lambda: tmp_path)
    clear_cache()
    with pytest.raises(PromptTemplateError, match="Unresolved"):
        load_prompt("odd", mode="workflow")


def test_forbidden_uri_placeholder_rejected(tmp_path, monkeypatch):
    from app.services import prompt_loader as pl

    (tmp_path / "bad.yaml").write_text(
        "name: bad\nsystem: |\n  uri={{output_uri}}\nprompt: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pl, "_templates_dir", lambda: tmp_path)
    clear_cache()
    with pytest.raises(PromptTemplateError, match="Forbidden"):
        load_prompt("bad", output_uri="https://example.com/container")
