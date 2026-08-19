# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Phase 3 runtime/offload lifecycle helpers."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.context_compaction import (
    collect_offload_references_from_state_payload,
    collect_offload_references_from_state_payloads,
    normalize_offload_reference,
)
from app.services.runtime_paths import (
    cleanup_unreferenced_offloads,
    delete_execution_runtime,
    delete_thread_runtime,
    ensure_runtime,
    is_confined_under_workspaces,
    runtime_root_for_thread,
)


def _runtime(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr("app.config.Config.WORKSPACES_ROOT", str(tmp_path))
    path = Path(ensure_runtime(execution_id="exec-1"))
    return path


def test_collect_offload_references_from_messages_and_nested_payload():
    payload = {
        "messages": [
            {
                "role": "tool",
                "content": json.dumps(
                    {
                        "offloaded": True,
                        "path": ".agent/offload/call_a.txt",
                        "preview": "x",
                    }
                ),
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "arguments": json.dumps(
                                {"path": "./.agent/offload/call_b.txt"}
                            )
                        }
                    }
                ],
            },
        ],
        "pending_tools": [{"args": {"path": ".agent/offload/call_c.txt"}}],
    }
    refs = collect_offload_references_from_state_payload(payload)
    assert refs == {
        ".agent/offload/call_a.txt",
        ".agent/offload/call_b.txt",
        ".agent/offload/call_c.txt",
    }


def test_collect_offload_references_ignores_non_offload_paths():
    payload = {
        "messages": [
            {"role": "tool", "content": json.dumps({"path": "src/main.py"})},
            {"role": "tool", "content": "plain text without offload pointer"},
        ]
    }
    assert collect_offload_references_from_state_payload(payload) == set()


def test_collect_offload_references_union_across_payloads():
    live_a = {
        "messages": [
            {
                "role": "tool",
                "content": json.dumps({"path": ".agent/offload/keep.txt"}),
            }
        ]
    }
    live_b = {
        "messages": [
            {
                "role": "tool",
                "content": json.dumps({"path": ".agent/offload/other.txt"}),
            }
        ]
    }
    refs = collect_offload_references_from_state_payloads([live_a, live_b])
    assert refs == {".agent/offload/keep.txt", ".agent/offload/other.txt"}


def test_normalize_offload_reference_rejects_traversal():
    assert normalize_offload_reference(".agent/offload/../secret.txt") is None
    assert normalize_offload_reference(".agent/offload/nested/x.txt") is None


def test_cleanup_deletes_only_orphan_offloads(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    offload = runtime / "offload"
    offload.mkdir(parents=True, exist_ok=True)
    (offload / "keep.txt").write_text("referenced", encoding="utf-8")
    (offload / "orphan.txt").write_text("drop me", encoding="utf-8")

    result = cleanup_unreferenced_offloads(
        str(runtime),
        referenced_logical_paths={".agent/offload/keep.txt"},
    )

    assert result.deleted_count == 1
    assert result.preserved_count == 1
    assert result.referenced_count == 1
    assert (offload / "keep.txt").is_file()
    assert not (offload / "orphan.txt").exists()


def test_cleanup_preserves_todos_and_other_runtime_files(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    offload = runtime / "offload"
    offload.mkdir(parents=True, exist_ok=True)
    (runtime / "todos.json").write_text('{"todos":[]}', encoding="utf-8")
    (runtime / "notes.txt").write_text("keep", encoding="utf-8")
    (offload / "orphan.txt").write_text("drop", encoding="utf-8")

    result = cleanup_unreferenced_offloads(str(runtime), referenced_logical_paths=set())

    assert result.deleted_count == 1
    assert (runtime / "todos.json").is_file()
    assert (runtime / "notes.txt").is_file()
    assert runtime.is_dir()


def test_cleanup_skips_symlink_outside_offload(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")

    offload = runtime / "offload"
    offload.mkdir(parents=True, exist_ok=True)
    (offload / "orphan.txt").write_text("drop", encoding="utf-8")
    link = offload / "escape.txt"
    link.symlink_to(outside)

    result = cleanup_unreferenced_offloads(str(runtime), referenced_logical_paths=set())

    assert result.skipped_outside >= 1
    assert outside.is_file()
    assert link.exists()
    assert not (offload / "orphan.txt").exists()


def test_cleanup_rejects_runtime_outside_workspaces_root(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.WORKSPACES_ROOT", str(tmp_path / "ws-root"))
    outside_runtime = tmp_path / "evil-runtime"
    outside_runtime.mkdir(parents=True)
    offload = outside_runtime / "offload"
    offload.mkdir()
    (offload / "orphan.txt").write_text("nope", encoding="utf-8")

    result = cleanup_unreferenced_offloads(str(outside_runtime), referenced_logical_paths=set())

    assert result.deleted_count == 0
    assert (offload / "orphan.txt").is_file()


def test_delete_execution_runtime_confined_under_workspaces_root(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.WORKSPACES_ROOT", str(tmp_path))
    runtime = Path(ensure_runtime(execution_id="exec-close"))
    (runtime / "offload").mkdir()
    (runtime / "offload" / "x.txt").write_text("x", encoding="utf-8")
    (runtime / "todos.json").write_text("{}", encoding="utf-8")

    result = delete_execution_runtime("exec-close")

    assert result.existed is True
    assert result.deleted is True
    assert result.runtime_path == str(runtime)
    assert not runtime.exists()


def test_delete_thread_runtime_removes_sha256_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.WORKSPACES_ROOT", str(tmp_path))
    thread_id = "thread-for-close"
    runtime = runtime_root_for_thread(thread_id)
    runtime.mkdir(parents=True)
    (runtime / "offload").mkdir()
    (runtime / "offload" / "keep.txt").write_text("x", encoding="utf-8")

    result = delete_thread_runtime(thread_id)

    assert result.existed is True
    assert result.deleted is True
    assert not runtime.exists()


def test_delete_runtime_rejects_paths_outside_workspaces_root(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.WORKSPACES_ROOT", str(tmp_path / "ws-root"))
    evil = tmp_path / "not-under-root" / "runtime"
    evil.mkdir(parents=True)
    (evil / "todos.json").write_text("{}", encoding="utf-8")

    # Patch the resolver to simulate a misconfigured path without touching disk layout.
    from app.services import runtime_paths as rp

    original = rp.runtime_root_for_execution

    def _evil_runtime(execution_id):
        return evil

    rp.runtime_root_for_execution = _evil_runtime
    try:
        result = delete_execution_runtime("any-id")
    finally:
        rp.runtime_root_for_execution = original

    assert result.deleted is False
    assert evil.exists()


def test_same_hold_resume_keeps_runtime_when_not_cleaned(tmp_path, monkeypatch):
    """Resume helpers preserve runtime unless an explicit orphan cleanup runs."""
    runtime = _runtime(tmp_path, monkeypatch)
    offload = runtime / "offload"
    offload.mkdir(parents=True, exist_ok=True)
    (offload / "call_1.txt").write_text("payload", encoding="utf-8")

    resume_payload = {
        "messages": [
            {
                "role": "tool",
                "content": json.dumps({"path": ".agent/offload/call_1.txt"}),
            }
        ]
    }
    refs = collect_offload_references_from_state_payload(resume_payload)
    assert refs == {".agent/offload/call_1.txt"}

    # No cleanup call: same-hold resume keeps the full runtime tree.
    assert (offload / "call_1.txt").is_file()
    assert (runtime / "offload").is_dir()


def test_fresh_run_orphan_cleanup_with_live_state_union(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    offload = runtime / "offload"
    offload.mkdir(parents=True, exist_ok=True)
    (offload / "discarded.txt").write_text("old hold", encoding="utf-8")
    (offload / "live.txt").write_text("still referenced", encoding="utf-8")

    live_payload = {
        "messages": [
            {
                "role": "tool",
                "content": json.dumps({"path": ".agent/offload/live.txt"}),
            }
        ]
    }
    refs = collect_offload_references_from_state_payloads([live_payload])
    result = cleanup_unreferenced_offloads(str(runtime), referenced_logical_paths=refs)

    assert result.deleted_count == 1
    assert not (offload / "discarded.txt").exists()
    assert (offload / "live.txt").is_file()


def test_is_confined_under_workspaces_root(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.Config.WORKSPACES_ROOT", str(tmp_path / "ws-root"))
    inside = tmp_path / "ws-root" / "exec-1" / "runtime"
    inside.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    assert is_confined_under_workspaces(inside) is True
    assert is_confined_under_workspaces(outside) is False
