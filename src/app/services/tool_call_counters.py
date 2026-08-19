# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Per-segment and cumulative tool-call counters for the agent loop."""

from __future__ import annotations

from typing import Any, Dict, Tuple


def init_fresh_counters() -> Tuple[int, int]:
    """Return (segment_count, total_count) for a new process_request."""
    return 0, 0


def restore_counters_on_resume(resume_state: Dict[str, Any]) -> Tuple[int, int]:
    """Restore cumulative total and reset segment count for a resume invocation."""
    total = resume_state.get("total_tool_call_count")
    if total is None:
        # Legacy snapshots only persisted tool_call_count (cumulative). Use it for
        # metrics/index continuity but never for segment enforcement.
        total = resume_state.get("tool_call_count", 0)
    return 0, int(total)


def snapshot_counter_fields(segment_count: int, total_count: int) -> Dict[str, int]:
    """Persist explicit counter names plus legacy tool_call_count alias."""
    return {
        "segment_tool_call_count": segment_count,
        "total_tool_call_count": total_count,
        "tool_call_count": total_count,
    }


def result_counter_fields(segment_count: int, total_count: int) -> Dict[str, int]:
    """Expose counters on outward-facing loop results."""
    return {
        "tool_calls_made": total_count,
        "segment_tool_calls_made": segment_count,
    }
