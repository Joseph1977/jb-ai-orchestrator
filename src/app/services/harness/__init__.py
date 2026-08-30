# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Harness adapters.

A *harness* is the runtime that prepares an AI session: it decides which files
to inject (rules/agents/system prompt), which skills/primitives are available
for lazy loading, and what special artifacts a given orchestration tool
(Cursor, Claude Code, Copilot, ...) uses.

``jb-ai-orchestrator`` is the harness; each adapter here encodes the folder
conventions of one orchestration type so the same playbook folder can be run
regardless of which tool authored it.
"""

from app.services.harness.base import (
    HarnessAdapter,
    HarnessManifest,
    PrimitiveRef,
    RootInstructionError,
    RootInstructionTooLarge,
    RootInstructionUnreadable,
)
from app.services.harness.registry import (
    collect_manifest,
    detect_adapter,
    enrich_system_prompt,
    get_adapter,
    render_subagent_catalog,
    render_system_prompt,
)

__all__ = [
    "HarnessAdapter",
    "HarnessManifest",
    "PrimitiveRef",
    "RootInstructionError",
    "RootInstructionTooLarge",
    "RootInstructionUnreadable",
    "detect_adapter",
    "get_adapter",
    "collect_manifest",
    "render_system_prompt",
    "render_subagent_catalog",
    "enrich_system_prompt",
]
