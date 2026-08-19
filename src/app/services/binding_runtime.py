# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Segment-scoped output backend, prompt rendering, and input workspace recovery."""

from __future__ import annotations

import copy
import re
import uuid
from pathlib import Path
from typing import Any, Optional, Sequence

from app.models.bindings import (
    INPUT_CREDENTIAL_REQUIRED,
    INPUT_FORBIDDEN,
    INPUT_PROVISION_FAILED,
    INPUT_WORKSPACE_MISSING,
    RUN_BINDING_AMBIGUOUS,
    LocationBinding,
    LocationType,
    Materialization,
)
from app.services.agui_messages import (
    MANDATORY_UI_CONTRACT_BEGIN,
    MANDATORY_UI_CONTRACT_END,
)
from app.services.binding_contract import (
    BindingError,
    provision_branch,
    provision_source,
    select_relative_workspace,
    should_provision_in_place,
)
from app.services.prompt_loader import load_prompt
from app.services.storage import StorageBackend, build_output_backend
from app.services import workspace_manager
from app.services.workspace_manager import WorkspaceError

RUN_BINDING_START = "<!-- run-binding:start -->"
RUN_BINDING_END = "<!-- run-binding:end -->"
_RUN_BINDING_MARKER = re.compile(r"<!-- run-binding:(start|end) -->")


def _find_mandatory_ui_contract_span(content: str) -> Optional[tuple[int, int]]:
    """Return the opaque UI-contract span when delimiters are well-formed.

    If ``MANDATORY_UI_CONTRACT_BEGIN`` is missing, or no matching end delimiter
    appears after the contract body, returns ``None`` so harness markers remain
    fully validated.
    """
    if (
        content.count(MANDATORY_UI_CONTRACT_BEGIN) != 1
        or content.count(MANDATORY_UI_CONTRACT_END) != 1
    ):
        return None
    begin_idx = content.index(MANDATORY_UI_CONTRACT_BEGIN)
    body_start = begin_idx + len(MANDATORY_UI_CONTRACT_BEGIN)
    end_idx = content.index(MANDATORY_UI_CONTRACT_END)
    if end_idx < body_start:
        return None
    return (begin_idx, end_idx + len(MANDATORY_UI_CONTRACT_END))


def _marker_in_span(match: re.Match[str], span: Optional[tuple[int, int]]) -> bool:
    if span is None:
        return False
    start, end = span
    pos = match.start()
    return start <= pos < end


def binding_system_prompt(config: dict[str, Any]) -> str:
    output = config.get("output")
    mode = str(config.get("mode") or "workflow")
    if output:
        return load_prompt(
            "output_binding",
            mode=mode,
            output_type=str(output.get("type")),
            logical_prefix=str(output.get("relativePath") or "."),
            write_output_tool="write_output_local",
            read_output_tool="read_output_local",
            list_output_tool="list_output_local",
            read_file_tool="read_file_local",
        )["system"].strip()
    return load_prompt(
        "output_unbound", mode=mode, read_file_tool="read_file_local"
    )["system"].strip()


def segment_output_backend(
    config: dict[str, Any],
    *,
    output_access_token: Optional[str] = None,
) -> Optional[StorageBackend]:
    return build_output_backend(
        config.get("output"),
        output_access_token=output_access_token,
    )


def _input_binding_from_config(config: dict[str, Any]) -> LocationBinding:
    raw = config.get("input") or {}
    materialization = raw.get("materialization")
    return LocationBinding(
        type=LocationType(str(raw["type"])),
        uri=str(raw["uri"]),
        relativePath=str(raw.get("relativePath") or "."),
        materialization=(
            Materialization(str(materialization)) if materialization else None
        ),
        branch=raw.get("branch"),
    )


def _system_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return str(content or "")


def _parse_run_binding_blocks(content: str) -> list[tuple[int, int]]:
    """Return well-formed non-nested block spans or raise RUN_BINDING_AMBIGUOUS."""
    opaque_span = _find_mandatory_ui_contract_span(content)
    depth = 0
    open_start: Optional[int] = None
    blocks: list[tuple[int, int]] = []

    for match in _RUN_BINDING_MARKER.finditer(content):
        if _marker_in_span(match, opaque_span):
            continue
        marker = match.group(1)
        if marker == "start":
            if depth > 0:
                raise BindingError(
                    RUN_BINDING_AMBIGUOUS,
                    "run-binding markers are nested or crossed",
                )
            open_start = match.start()
            depth = 1
            continue

        if depth == 0:
            raise BindingError(
                RUN_BINDING_AMBIGUOUS,
                "run-binding markers are nested or crossed",
            )
        end_pos = match.end()
        assert open_start is not None
        blocks.append((open_start, end_pos))
        open_start = None
        depth = 0

    if depth != 0:
        raise BindingError(
            RUN_BINDING_AMBIGUOUS,
            "run-binding block is missing an end marker",
        )
    return blocks


def _apply_run_binding_to_content(content: str, binding_block: str) -> str:
    blocks = _parse_run_binding_blocks(content)
    if not blocks:
        trimmed = content.rstrip()
        if trimmed:
            return f"{trimmed}\n\n{binding_block}"
        return binding_block

    first_start = blocks[0][0]
    rebuilt = content
    for start, end in reversed(blocks):
        rebuilt = rebuilt[:start] + rebuilt[end:]
    prefix = rebuilt[:first_start]
    suffix = rebuilt[first_start:]
    if prefix and not prefix.endswith("\n"):
        prefix = f"{prefix.rstrip()}\n\n"
    if suffix and not suffix.startswith("\n"):
        suffix = f"\n\n{suffix.lstrip()}"
    return f"{prefix}{binding_block}{suffix}"


def refresh_segment_run_binding(
    messages: Sequence[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Refresh the tagged run-binding block exactly once for a segment."""
    binding_block = binding_system_prompt(config)
    copied = copy.deepcopy(list(messages))

    system_idx = next(
        (idx for idx, msg in enumerate(copied) if msg.get("role") == "system"),
        None,
    )
    if system_idx is None:
        return [{"role": "system", "content": binding_block}, *copied]

    system_message = copied[system_idx]
    updated = dict(system_message)
    updated["content"] = _apply_run_binding_to_content(
        _system_content(system_message),
        binding_block,
    )
    copied[system_idx] = updated
    return copied


def _map_workspace_error(exc: WorkspaceError) -> BindingError:
    if exc.code:
        return BindingError(exc.code, exc.message)
    return BindingError(
        INPUT_PROVISION_FAILED,
        "input workspace could not be provisioned",
    )


async def ensure_input_workspace(
    execution_id: str | uuid.UUID,
    config: dict[str, Any],
    *,
    workspace_path: Optional[str] = None,
    input_access_token: Optional[str] = None,
) -> str:
    """Ensure the selected input workspace exists for this segment."""
    binding = _input_binding_from_config(config)
    legacy_writable = bool(config.get("legacyWritableInPlace", False))
    in_place = should_provision_in_place(binding, legacy_writable=legacy_writable)

    if workspace_path:
        selected = Path(workspace_path).expanduser()
        if selected.is_dir():
            return str(selected.resolve())

    if in_place:
        raise BindingError(
            INPUT_WORKSPACE_MISSING,
            "caller-owned input workspace is missing",
        )

    source = provision_source(binding)
    try:
        ws = await workspace_manager.provision(
            execution_id,
            source,
            in_place=False,
            branch=provision_branch(binding),
            input_access_token=input_access_token,
        )
    except WorkspaceError as exc:
        raise _map_workspace_error(exc) from exc

    try:
        return select_relative_workspace(ws.path, binding.relative_path)
    except BindingError as exc:
        raise BindingError(
            INPUT_PROVISION_FAILED,
            "input workspace could not be provisioned",
        ) from exc
