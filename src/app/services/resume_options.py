"""Resolve caller-controlled options for a resumed model segment."""

from typing import Optional


DEFAULT_MODEL = "gpt-3.5-turbo"


def resolve_resume_model(
    requested: Optional[str],
    snapshot: Optional[str],
    segment: Optional[str] = None,
) -> str:
    """Prefer a request override without changing snapshot-first compatibility."""
    return requested or snapshot or segment or DEFAULT_MODEL


def resolve_resume_max_tool_calls(
    requested: Optional[int],
    snapshot: Optional[int],
    segment: Optional[int] = None,
) -> Optional[int]:
    """Resolve a segment budget while preserving zero as an explicit value."""
    if requested is not None:
        return requested
    if snapshot is not None:
        return snapshot
    return segment
