# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""YAML prompt templates with strict ``{{placeholder}}`` substitution."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from app.config import Config

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_ANY_MUSTACHE_RE = re.compile(r"\{\{[^}]+\}\}")
_FORBIDDEN_PLACEHOLDERS = frozenset(
    {
        "accesstoken",
        "access_token",
        "inputaccesstoken",
        "outputaccesstoken",
        "output_uri",
        "outputuri",
        "uri",
        "sas",
        "sastoken",
        "credential",
        "credentials",
        "password",
        "token",
    }
)

_cache: dict[str, dict[str, Any]] = {}


class PromptTemplateError(Exception):
    """Invalid template or unresolved/forbidden placeholder."""


def _templates_dir() -> Path:
    configured = (Config.PROMPTS_DIR or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent / "prompts"


def _load_raw(template_name: str) -> dict[str, Any]:
    key = template_name[:-5] if template_name.endswith(".yaml") else template_name
    if key in _cache:
        return _cache[key]
    path = _templates_dir() / f"{key}.yaml"
    if not path.is_file():
        raise PromptTemplateError(f"Prompt template not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise PromptTemplateError(f"Prompt template {key} must be a mapping")
    _cache[key] = data
    return data


def _fill(text: str, variables: dict[str, str]) -> str:
    def replacer(match: re.Match[str]) -> str:
        name = match.group(1)
        if name.lower() in _FORBIDDEN_PLACEHOLDERS:
            raise PromptTemplateError(
                f"Forbidden placeholder '{{{{{name}}}}}' (credentials/URI are not allowed)"
            )
        if name not in variables:
            raise PromptTemplateError(f"Unresolved placeholder '{{{{{name}}}}}'")
        return str(variables[name])

    filled = _PLACEHOLDER_RE.sub(replacer, text or "")
    leftover = _ANY_MUSTACHE_RE.search(filled)
    if leftover:
        raise PromptTemplateError(f"Unresolved placeholder '{leftover.group(0)}'")
    return filled


def load_prompt(template_name: str, **variables: str) -> dict[str, str]:
    """Load ``system`` / ``prompt`` keys and substitute placeholders strictly."""
    raw = _load_raw(template_name)
    system = raw.get("system") or ""
    prompt = raw.get("prompt") or ""
    if not isinstance(system, str) or not isinstance(prompt, str):
        raise PromptTemplateError(f"Template {template_name} system/prompt must be strings")
    return {
        "name": str(raw.get("name") or template_name),
        "system": _fill(system, variables),
        "prompt": _fill(prompt, variables),
    }


def clear_cache() -> None:
    _cache.clear()
