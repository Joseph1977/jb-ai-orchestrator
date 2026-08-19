# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Mode-based policy for built-in input tools."""

from __future__ import annotations

from app.models.bindings import ExecutionMode

WORKFLOW_BLOCKED_LOCAL = frozenset(
    {
        "write_file",
        "edit_file",
        "create_file",
        "create_folder",
        "git_add",
        "git_commit",
        "git_checkout_branch",
        "execute",
    }
)


def blocked_local_tools(mode: str) -> set[str]:
    if mode == ExecutionMode.WORKFLOW.value:
        return set(WORKFLOW_BLOCKED_LOCAL)
    return set()


def local_tool_allowed(base_name: str, mode: str) -> bool:
    return base_name not in blocked_local_tools(mode)
