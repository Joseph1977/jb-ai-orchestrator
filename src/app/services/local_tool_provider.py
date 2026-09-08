# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Built-in, in-process local tools (the harness's file/git/ask-user primitives).

These are exposed to the LLM exactly like MCP tools, but executed in-process so
the agent loop can lazily read skills/files mid-iteration. Every tool is scoped
to the execution's workspace sandbox via :func:`workspace_manager.resolve_within`.

Tools are namespaced (``<base>_<namespace>`` e.g. ``read_file_local``) so a
remote MCP server can never be confused with a local built-in, and local always
wins on conflict.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.config import Config
from app.services import workspace_manager
from app.utils.logger import logger


@dataclass
class LocalToolContext:
    """Runtime context passed to a local tool handler."""

    workspace_path: str
    in_place: bool = False
    # Nested task/subagent depth (0 = top-level orchestrator/agent run).
    subagent_depth: int = 0
    # Service-owned root for logical ``.agent/**`` (offload, todos).
    runtime_path: Optional[str] = None
    mode: str = "working_copy"
    # Segment-scoped only. It may contain transient credentials and is never persisted.
    output_backend: Any = None


ToolHandler = Callable[[Dict[str, Any], LocalToolContext], Awaitable[Dict[str, Any]]]


@dataclass
class LocalTool:
    base_name: str
    description: str
    parameters: dict
    handler: Optional[ToolHandler] = None
    awaits_response: bool = False
    # Handled by ToolExecutionHub (not the provider's execute()).
    hub_managed: bool = False

    def prefixed_name(self, namespace: str) -> str:
        return f"{self.base_name}_{namespace}"

    def to_litellm_tool(self, namespace: str) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.prefixed_name(namespace),
                "description": f"[local] {self.description}",
                "parameters": self.parameters,
            },
        }


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

def _safe(ctx: LocalToolContext, rel_path: str) -> str:
    from app.services.runtime_paths import resolve_tool_path

    return resolve_tool_path(
        ctx.workspace_path, rel_path, runtime_path=ctx.runtime_path
    )


async def _list_files(args: Dict[str, Any], ctx: LocalToolContext) -> Dict[str, Any]:
    rel_path = args.get("path", ".") or "."
    recursive = bool(args.get("recursive", False))
    base = Path(_safe(ctx, rel_path))
    entries: List[str] = []
    if base.is_file():
        entries = [rel_path]
        walker = []
    elif base.is_dir():
        walker = base.rglob("*") if recursive else base.iterdir()
    else:
        walker = []
    from app.services.runtime_paths import is_agent_runtime_rel, logical_from_runtime

    listing_runtime = is_agent_runtime_rel(rel_path)
    workspace = Path(ctx.workspace_path)
    runtime = Path(ctx.runtime_path).resolve() if ctx.runtime_path else None
    for item in sorted(walker):
        try:
            if listing_runtime and runtime:
                display = logical_from_runtime(str(runtime), str(item.resolve()))
            else:
                display = str(item.relative_to(workspace))
        except ValueError:
            display = item.name
        if not listing_runtime and (display == ".agent" or display.startswith(".agent/")):
            continue
        entries.append(display + ("/" if item.is_dir() else ""))
        if len(entries) >= 2000:
            entries.append("... [truncated]")
            break
    output = {"bound": ctx.output_backend is not None, "entries": []}
    output_note = (
        "Reserved runtime paths are not part of durable output."
        if ctx.output_backend is not None and listing_runtime
        else "No durable output store is bound."
    )
    input_note = (
        "Input workspace (playbook and working copy)."
        if base.exists()
        else "Path is not present in the input workspace."
    )
    if ctx.output_backend is not None and not listing_runtime:
        listed = await ctx.output_backend.list(rel_path, next_token=args.get("nextToken"))
        output = {"bound": True, "entries": listed.entries}
        if listed.next_token:
            output["nextToken"] = listed.next_token
        output_note = "Durable output listing; output is authoritative for workflow state."
    from app.services.prompt_loader import load_prompt

    note = load_prompt(
        "list_partition_note",
        input_note=input_note,
        output_note=output_note,
    )["prompt"].strip()
    return {
        "path": rel_path,
        "entries": entries,
        "input": {"bound": True, "entries": entries},
        "output": output,
        "note": note,
    }


async def _read_file(args: Dict[str, Any], ctx: LocalToolContext) -> Dict[str, Any]:
    from app.services.context_compaction import is_agent_offload_path
    from app.services.runtime_paths import is_agent_runtime_rel

    rel_path = args.get("path")
    if not rel_path:
        return {"error": "path is required"}
    dereferencing_offload = is_agent_offload_path(str(rel_path))
    reading_runtime = is_agent_runtime_rel(str(rel_path))
    target = Path(_safe(ctx, rel_path))
    output_exists = (
        await ctx.output_backend.exists(str(rel_path))
        if ctx.output_backend is not None and not reading_runtime
        else False
    )
    if not target.exists() or not target.is_file():
        if output_exists:
            from app.services.prompt_loader import load_prompt

            return {
                "error": f"File not found in input: {rel_path}",
                "outputExists": True,
                "note": load_prompt(
                    "read_output_hint", read_output_tool="read_output_local"
                )["prompt"].strip(),
            }
        return {"error": f"File not found: {rel_path}"}

    # Binary media (images / PDFs): return a small marker; hub attaches bytes next turn.
    try:
        head = target.read_bytes()[:32]
    except OSError as exc:
        return {"error": f"Failed to read {rel_path}: {exc}"}

    from app.services.multimodal import build_media_marker, detect_media

    media = detect_media(target, head)
    if media is not None:
        kind, mime = media
        try:
            size = target.stat().st_size
        except OSError as exc:
            return {"error": f"Failed to stat {rel_path}: {exc}"}
        return build_media_marker(
            rel_path=rel_path,
            abs_path=str(target.resolve()),
            kind=kind,
            mime=mime,
            size=size,
        )

    max_bytes = int(args.get("maxBytes", 200_000))
    try:
        data = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"error": f"Failed to read {rel_path}: {exc}"}
    truncated = False
    if len(data) > max_bytes:
        data = data[:max_bytes]
        truncated = True
    result: Dict[str, Any] = {"path": rel_path, "content": data, "truncated": truncated}
    if output_exists:
        from app.services.prompt_loader import load_prompt

        result["outputExists"] = True
        result["note"] = load_prompt(
            "read_dual_path_note", read_output_tool="read_output_local"
        )["prompt"].strip()
    if dereferencing_offload:
        result["alreadyOffloaded"] = True
        if truncated:
            result["note"] = (
                "Offloaded file read truncated; increase maxBytes for full content."
            )
    return result


async def _write_file(args: Dict[str, Any], ctx: LocalToolContext) -> Dict[str, Any]:
    rel_path = args.get("path")
    if not rel_path:
        return {"error": "path is required"}
    content = args.get("content", "")
    target = Path(_safe(ctx, rel_path))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {"error": f"Failed to write {rel_path}: {exc}"}
    return {"path": rel_path, "bytesWritten": len(content.encode("utf-8"))}


async def _create_file(args: Dict[str, Any], ctx: LocalToolContext) -> Dict[str, Any]:
    rel_path = args.get("path")
    if not rel_path:
        return {"error": "path is required"}
    target = Path(_safe(ctx, rel_path))
    if target.exists() and not args.get("overwrite", False):
        return {"error": f"File already exists: {rel_path} (pass overwrite=true to replace)"}
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(args.get("content", ""), encoding="utf-8")
    except OSError as exc:
        return {"error": f"Failed to create {rel_path}: {exc}"}
    return {"path": rel_path, "created": True}


async def _create_folder(args: Dict[str, Any], ctx: LocalToolContext) -> Dict[str, Any]:
    rel_path = args.get("path")
    if not rel_path:
        return {"error": "path is required"}
    target = Path(_safe(ctx, rel_path))
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"error": f"Failed to create folder {rel_path}: {exc}"}
    return {"path": rel_path, "created": True}


async def _git(args: Dict[str, Any], ctx: LocalToolContext, git_args: List[str]) -> Dict[str, Any]:
    code, out, err = await workspace_manager.run_git(git_args, cwd=ctx.workspace_path)
    return {"exitCode": code, "stdout": out, "stderr": err}


async def _git_status(args, ctx):
    return await _git(args, ctx, ["status", "--porcelain=v1", "-b"])


async def _git_diff(args, ctx):
    git_args = ["diff"]
    if args.get("staged"):
        git_args.append("--staged")
    if args.get("path"):
        # Validate path stays inside the workspace / policy before handing to git.
        _safe(ctx, args["path"])
        git_args += ["--", args["path"]]
    return await _git(args, ctx, git_args)


async def _git_log(args, ctx):
    n = int(args.get("maxCount", 20))
    return await _git(args, ctx, ["log", f"-{n}", "--oneline", "--decorate"])


async def _git_add(args, ctx):
    paths = args.get("paths") or ["."]
    if isinstance(paths, str):
        paths = [paths]
    for p in paths:
        _safe(ctx, p)
    return await _git(args, ctx, ["add", *paths])


async def _git_commit(args, ctx):
    message = args.get("message")
    if not message:
        return {"error": "message is required"}
    return await _git(args, ctx, ["commit", "-m", message])


async def _git_checkout_branch(args, ctx):
    name = args.get("name")
    if not name:
        return {"error": "name is required"}
    git_args = ["checkout"]
    if args.get("create", True):
        git_args.append("-b")
    git_args.append(name)
    return await _git(args, ctx, git_args)


async def _edit_file(args: Dict[str, Any], ctx: LocalToolContext) -> Dict[str, Any]:
    """Exact string replacement in a file (StrReplace-style)."""
    rel_path = args.get("path")
    old = args.get("old_string")
    new = args.get("new_string")
    if not rel_path:
        return {"error": "path is required"}
    if old is None or old == "":
        return {"error": "old_string is required and must be non-empty"}
    if new is None:
        return {"error": "new_string is required"}
    replace_all = bool(args.get("replace_all", False))

    target = Path(_safe(ctx, rel_path))
    if not target.exists() or not target.is_file():
        return {"error": f"File not found: {rel_path}"}
    try:
        original = target.read_text(encoding="utf-8")
    except OSError as exc:
        return {"error": f"Failed to read {rel_path}: {exc}"}

    count = original.count(old)
    if count == 0:
        return {"error": f"old_string not found in {rel_path}"}
    if count > 1 and not replace_all:
        return {
            "error": (
                f"old_string matched {count} times in {rel_path}; "
                "pass replace_all=true or provide a more unique string"
            )
        }

    updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
    try:
        target.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return {"error": f"Failed to write {rel_path}: {exc}"}
    return {
        "path": rel_path,
        "replacements": count if replace_all else 1,
        "bytesWritten": len(updated.encode("utf-8")),
    }


async def _glob_files(args: Dict[str, Any], ctx: LocalToolContext) -> Dict[str, Any]:
    pattern = args.get("pattern")
    if not pattern:
        return {"error": "pattern is required"}
    rel_root = args.get("path", ".") or "."
    root = Path(_safe(ctx, rel_root))
    if not root.exists():
        return {"error": f"Path not found: {rel_root}"}
    workspace = Path(ctx.workspace_path).resolve()
    max_results = int(args.get("maxResults", 500))

    matches: List[str] = []
    # Support both "src/**/*.py" from workspace root and relative to path=.
    search_root = root if root.is_dir() else root.parent
    try:
        for hit in sorted(search_root.glob(pattern)):
            if not hit.exists():
                continue
            try:
                display = str(hit.resolve().relative_to(workspace))
            except ValueError:
                continue
            if display == ".agent" or display.startswith(".agent/") or "/.agent/" in f"/{display}":
                continue
            matches.append(display + ("/" if hit.is_dir() else ""))
            if len(matches) >= max_results:
                break
    except ValueError as exc:
        return {"error": f"Invalid glob pattern: {exc}"}

    return {
        "pattern": pattern,
        "path": rel_root,
        "matches": matches,
        "truncated": len(matches) >= max_results,
        "count": len(matches),
    }


_SKIP_DIR_NAMES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".agent",
}


async def _grep_files(args: Dict[str, Any], ctx: LocalToolContext) -> Dict[str, Any]:
    pattern = args.get("pattern")
    if not pattern:
        return {"error": "pattern is required"}
    rel_root = args.get("path", ".") or "."
    file_glob = args.get("glob")  # optional fnmatch against relative path
    case_insensitive = bool(args.get("caseInsensitive", False))
    max_matches = int(args.get("maxMatches", 100))
    context_lines = int(args.get("context", 0))

    try:
        regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    except re.error as exc:
        return {"error": f"Invalid regex pattern: {exc}"}

    root = Path(_safe(ctx, rel_root))
    if not root.exists():
        return {"error": f"Path not found: {rel_root}"}
    workspace = Path(ctx.workspace_path).resolve()

    files: List[Path] = []
    if root.is_file():
        files = [root]
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
            for name in filenames:
                files.append(Path(dirpath) / name)

    hits: List[dict] = []
    files_scanned = 0
    for file_path in files:
        if len(hits) >= max_matches:
            break
        try:
            rel = str(file_path.resolve().relative_to(workspace))
        except ValueError:
            continue
        if file_glob and not fnmatch.fnmatch(rel, file_glob) and not fnmatch.fnmatch(file_path.name, file_glob):
            continue
        try:
            if file_path.stat().st_size > 2_000_000:
                continue
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Skip likely-binary content.
        if "\x00" in text[:4096]:
            continue
        files_scanned += 1
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if not regex.search(line):
                continue
            start = max(0, i - context_lines)
            end = min(len(lines), i + 1 + context_lines)
            hits.append(
                {
                    "path": rel,
                    "line": i + 1,
                    "text": line[:500],
                    "context": lines[start:end] if context_lines else None,
                }
            )
            if len(hits) >= max_matches:
                break

    return {
        "pattern": pattern,
        "path": rel_root,
        "matches": hits,
        "filesScanned": files_scanned,
        "truncated": len(hits) >= max_matches,
        "count": len(hits),
    }


async def _execute_shell(args: Dict[str, Any], ctx: LocalToolContext) -> Dict[str, Any]:
    """Run a shell command with cwd=workspace. Not a full OS jail."""
    if not Config.LOCAL_SHELL_ENABLED:
        return {"error": "execute_local is disabled (LOCAL_SHELL_ENABLED=false)"}
    command = args.get("command")
    if not command or not str(command).strip():
        return {"error": "command is required"}

    from app.services import path_policy
    from app.services.hooks import run_hooks

    blocked = path_policy.check_shell_command(str(command))
    if blocked:
        return {"error": blocked, "command": command}

    timeout = args.get("timeoutSec")
    timeout = int(timeout) if timeout is not None else Config.LOCAL_SHELL_TIMEOUT_SEC
    timeout = max(1, min(timeout, 600))
    max_bytes = Config.LOCAL_SHELL_MAX_OUTPUT_BYTES

    cwd = Path(ctx.workspace_path).resolve()
    if not cwd.is_dir():
        return {"error": f"Workspace is not a directory: {ctx.workspace_path}"}

    env = os.environ.copy()
    env["PWD"] = str(cwd)

    try:
        proc = await asyncio.create_subprocess_shell(
            str(command),
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "error": f"Command timed out after {timeout}s",
                "command": command,
                "timedOut": True,
            }
    except OSError as exc:
        return {"error": f"Failed to start command: {exc}", "command": command}

    def _decode_trim(data: bytes) -> tuple[str, bool]:
        truncated = len(data) > max_bytes
        chunk = data[:max_bytes] if truncated else data
        return chunk.decode(errors="replace"), truncated

    stdout, stdout_trunc = _decode_trim(stdout_b or b"")
    stderr, stderr_trunc = _decode_trim(stderr_b or b"")
    result = {
        "command": command,
        "exitCode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdoutTruncated": stdout_trunc,
        "stderrTruncated": stderr_trunc,
        "cwd": ".",
    }
    await run_hooks(
        ctx.workspace_path,
        "afterShellExecution",
        {
            "command": str(command),
            "cwd": ".",
            "exitCode": proc.returncode,
            "stdout": stdout[:4000],
            "stderr": stderr[:4000],
        },
        matcher_subject=str(command),
    )
    return result


_TODOS_PATH = ".agent/todos.json"
_VALID_TODO_STATUSES = {"pending", "in_progress", "completed", "cancelled"}


async def _write_todos(args: Dict[str, Any], ctx: LocalToolContext) -> Dict[str, Any]:
    """Replace the structured todo list persisted in the workspace."""
    todos = args.get("todos")
    if not isinstance(todos, list):
        return {"error": "todos must be an array of {id, content, status}"}
    normalized: List[dict] = []
    for item in todos:
        if not isinstance(item, dict):
            return {"error": "each todo must be an object"}
        todo_id = str(item.get("id") or "").strip()
        content = str(item.get("content") or "").strip()
        status = str(item.get("status") or "pending").strip().lower()
        if not todo_id or not content:
            return {"error": "each todo requires non-empty id and content"}
        if status not in _VALID_TODO_STATUSES:
            return {
                "error": (
                    f"invalid status '{status}'; "
                    f"expected one of {sorted(_VALID_TODO_STATUSES)}"
                )
            }
        normalized.append({"id": todo_id, "content": content, "status": status})

    target = Path(_safe(ctx, _TODOS_PATH))
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"todos": normalized}
    try:
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        return {"error": f"Failed to write todos: {exc}"}
    return {"path": _TODOS_PATH, "count": len(normalized), "todos": normalized}


async def _write_output(args: Dict[str, Any], ctx: LocalToolContext) -> Dict[str, Any]:
    if ctx.output_backend is None:
        return {"error": "No durable output store is bound"}
    path = args.get("path")
    if not path:
        return {"error": "path is required"}
    count = await ctx.output_backend.write_text(str(path), str(args.get("content", "")))
    return {"path": path, "bytesWritten": count, "durable": True}


async def _edit_output(args: Dict[str, Any], ctx: LocalToolContext) -> Dict[str, Any]:
    """Exact string replacement in a bound durable-output file."""
    if ctx.output_backend is None:
        return {"error": "No durable output store is bound"}
    path = args.get("path")
    old = args.get("old_string")
    new = args.get("new_string")
    if not path:
        return {"error": "path is required"}
    if old is None or old == "":
        return {"error": "old_string is required and must be non-empty"}
    if new is None:
        return {"error": "new_string is required"}
    replace_all = bool(args.get("replace_all", False))

    original = await ctx.output_backend.read_text(str(path))
    count = original.count(old)
    if count == 0:
        return {"error": f"old_string not found in {path}"}
    if count > 1 and not replace_all:
        return {
            "error": (
                f"old_string matched {count} times in {path}; "
                "pass replace_all=true or provide a more unique string"
            )
        }

    updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
    bytes_written = await ctx.output_backend.write_text(str(path), updated)
    return {
        "path": path,
        "replacements": count if replace_all else 1,
        "bytesWritten": bytes_written,
        "durable": True,
    }


async def _read_output(args: Dict[str, Any], ctx: LocalToolContext) -> Dict[str, Any]:
    if ctx.output_backend is None:
        return {"error": "No durable output store is bound"}
    path = args.get("path")
    if not path:
        return {"error": "path is required"}
    return {"path": path, "content": await ctx.output_backend.read_text(str(path)), "durable": True}


async def _list_output(args: Dict[str, Any], ctx: LocalToolContext) -> Dict[str, Any]:
    if ctx.output_backend is None:
        return {"error": "No durable output store is bound"}
    result = await ctx.output_backend.list(
        str(args.get("path") or "."),
        next_token=args.get("nextToken"),
    )
    payload: Dict[str, Any] = {"bound": True, "entries": result.entries}
    if result.next_token:
        payload["nextToken"] = result.next_token
    return payload


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

_PATH_PROP = {"type": "string", "description": "Workspace-relative path"}
_OUTPUT_PATH_PROP = {
    "type": "string",
    "description": "Path relative to the bound durable output root",
}

_TOOLS: List[LocalTool] = [
    LocalTool(
        base_name="list_files",
        description="List files/folders under a workspace-relative path.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path (default '.')"},
                "recursive": {"type": "boolean", "description": "Recurse into subfolders"},
                "nextToken": {"type": "string", "description": "Output pagination token"},
            },
            "required": [],
        },
        handler=_list_files,
    ),
    LocalTool(
        base_name="read_file",
        description=(
            "Read a workspace file. Text files return content. Images/PDFs return a "
            "multimodal marker; the binary is attached for vision-capable models on "
            "the next LLM turn (not streamed as chat text)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": _PATH_PROP,
                "maxBytes": {"type": "integer", "description": "Max chars to return for text files"},
            },
            "required": ["path"],
        },
        handler=_read_file,
    ),
    LocalTool(
        base_name="write_file",
        description="Create or overwrite a file with the given content.",
        parameters={
            "type": "object",
            "properties": {"path": _PATH_PROP, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        handler=_write_file,
    ),
    LocalTool(
        base_name="create_file",
        description="Create a new file; fails if it exists unless overwrite=true.",
        parameters={
            "type": "object",
            "properties": {
                "path": _PATH_PROP,
                "content": {"type": "string"},
                "overwrite": {"type": "boolean"},
            },
            "required": ["path"],
        },
        handler=_create_file,
    ),
    LocalTool(
        base_name="create_folder",
        description="Create a folder (and parents) at a workspace-relative path.",
        parameters={
            "type": "object",
            "properties": {"path": _PATH_PROP},
            "required": ["path"],
        },
        handler=_create_folder,
    ),
    LocalTool(
        base_name="git_status",
        description="Show git working-tree status for the workspace repo.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=_git_status,
    ),
    LocalTool(
        base_name="git_diff",
        description="Show git diff. Set staged=true for the index; optional path.",
        parameters={
            "type": "object",
            "properties": {"staged": {"type": "boolean"}, "path": _PATH_PROP},
            "required": [],
        },
        handler=_git_diff,
    ),
    LocalTool(
        base_name="git_log",
        description="Show recent commit history (oneline).",
        parameters={
            "type": "object",
            "properties": {"maxCount": {"type": "integer"}},
            "required": [],
        },
        handler=_git_log,
    ),
    LocalTool(
        base_name="git_add",
        description="Stage files for commit (paths default to all).",
        parameters={
            "type": "object",
            "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
            "required": [],
        },
        handler=_git_add,
    ),
    LocalTool(
        base_name="git_commit",
        description="Commit staged changes with a message.",
        parameters={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        handler=_git_commit,
    ),
    LocalTool(
        base_name="git_checkout_branch",
        description="Create (default) or switch to a git branch.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}, "create": {"type": "boolean"}},
            "required": ["name"],
        },
        handler=_git_checkout_branch,
    ),
    LocalTool(
        base_name="edit_file",
        description=(
            "Exact string replacement in a file. Prefer this over write_file for "
            "targeted edits. Fails if old_string is missing or matches multiple "
            "times unless replace_all=true."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": _PATH_PROP,
                "old_string": {"type": "string", "description": "Exact text to find"},
                "new_string": {"type": "string", "description": "Replacement text"},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence (default false)",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
        handler=_edit_file,
    ),
    LocalTool(
        base_name="glob",
        description="Find files matching a glob pattern (e.g. '**/*.py') under the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"},
                "path": {"type": "string", "description": "Relative root (default '.')"},
                "maxResults": {"type": "integer"},
            },
            "required": ["pattern"],
        },
        handler=_glob_files,
    ),
    LocalTool(
        base_name="grep",
        description="Search file contents with a regex pattern. Optional glob filters files.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex to search for"},
                "path": {"type": "string", "description": "Relative file or folder (default '.')"},
                "glob": {"type": "string", "description": "Optional fnmatch filter (e.g. '*.py')"},
                "caseInsensitive": {"type": "boolean"},
                "maxMatches": {"type": "integer"},
                "context": {"type": "integer", "description": "Surrounding lines to include"},
            },
            "required": ["pattern"],
        },
        handler=_grep_files,
    ),
    LocalTool(
        base_name="execute",
        description=(
            "Run a shell command with cwd set to the workspace. Use for tests, "
            "builds, and CLIs. Captures stdout/stderr. Not a full OS sandbox — "
            "prefer workspace-relative paths."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeoutSec": {"type": "integer", "description": "Timeout in seconds"},
            },
            "required": ["command"],
        },
        handler=_execute_shell,
    ),
    LocalTool(
        base_name="write_todos",
        description=(
            "Replace the structured task list for this run. Use for multi-step "
            "work: keep statuses accurate (pending|in_progress|completed|cancelled). "
            "Persists to logical .agent/todos.json in the service runtime."
        ),
        parameters={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "Full todo list (replaces previous)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": sorted(_VALID_TODO_STATUSES),
                            },
                        },
                        "required": ["id", "content", "status"],
                    },
                }
            },
            "required": ["todos"],
        },
        handler=_write_todos,
    ),
    LocalTool(
        base_name="write_output",
        description="Write UTF-8 text directly to the bound durable output store.",
        parameters={
            "type": "object",
            "properties": {"path": _OUTPUT_PATH_PROP, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        handler=_write_output,
    ),
    LocalTool(
        base_name="edit_output",
        description=(
            "Exact string replacement in a bound durable-output file. Prefer this "
            "over write_output for targeted edits. Fails if old_string is missing "
            "or matches multiple times unless replace_all=true."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": _OUTPUT_PATH_PROP,
                "old_string": {"type": "string", "description": "Exact text to find"},
                "new_string": {"type": "string", "description": "Replacement text"},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence (default false)",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
        handler=_edit_output,
    ),
    LocalTool(
        base_name="read_output",
        description="Read UTF-8 text from the bound durable output store.",
        parameters={
            "type": "object",
            "properties": {"path": _OUTPUT_PATH_PROP},
            "required": ["path"],
        },
        handler=_read_output,
    ),
    LocalTool(
        base_name="list_output",
        description="List logical paths in the bound durable output store.",
        parameters={
            "type": "object",
            "properties": {
                "path": _OUTPUT_PATH_PROP,
                "nextToken": {"type": "string"},
            },
            "required": [],
        },
        handler=_list_output,
    ),
    # Hub-managed: spawns an isolated nested agent loop and returns a summary.
    LocalTool(
        base_name="task",
        description=(
            "Delegate a focused subtask to an isolated subagent with its own "
            "context window. Provide a clear prompt. Optionally set agent to a "
            "workspace agent markdown path (e.g. .cursor/agents/reviewer.md) or "
            "name. Returns only the subagent's final summary — use for research, "
            "reviews, or bounded coding work that would bloat the main thread."
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Full instructions for the subagent",
                },
                "agent": {
                    "type": "string",
                    "description": (
                        "Optional agent name or relative path to an agent .md "
                        "(e.g. reviewer or .claude/agents/reviewer.md)"
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Short label for logs/UI (optional)",
                },
            },
            "required": ["prompt"],
        },
        handler=None,
        hub_managed=True,
    ),
    # Transport-agnostic user-input primitive. Handled specially by the hub:
    # it never executes inline; it persists awaits-response state so any pod can
    # resume once the answer arrives (AG-UI or /orchestrator/resume).
    LocalTool(
        base_name="ask_user",
        description=(
            "Headless fallback for asking the user a question and pausing until "
            "they respond. IMPORTANT: if this run provides ANY front-end/UI tool "
            "for interacting with the user (e.g. one that displays a question, "
            "choices, buttons, or an approval prompt), ALWAYS use that instead — "
            "it renders real UI. Only use this tool when no such UI tool is "
            "available (e.g. a headless/API run with no live UI)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["question"],
        },
        handler=None,
        awaits_response=True,
    ),
]


class LocalToolProvider:
    """Registry of built-in local tools, namespaced and workspace-scoped."""

    def __init__(self, namespace: Optional[str] = None) -> None:
        self.namespace = namespace or Config.LOCAL_TOOLS_NAMESPACE
        self._by_prefixed: Dict[str, LocalTool] = {
            tool.prefixed_name(self.namespace): tool for tool in _TOOLS
        }
        self._base_names = {tool.base_name for tool in _TOOLS}

    @property
    def enabled(self) -> bool:
        return bool(Config.LOCAL_TOOLS_ENABLED)

    def base_names(self) -> set[str]:
        return set(self._base_names)

    def list_litellm_tools(
        self,
        *,
        exclude_base: Optional[set[str]] = None,
        mode: str = "working_copy",
        output_bound: bool = False,
    ) -> List[dict]:
        from app.services.tool_policy import blocked_local_tools

        exclude = set(exclude_base or ())
        exclude.update(blocked_local_tools(mode))
        if not output_bound:
            exclude.update({"write_output", "edit_output", "read_output", "list_output"})
        tools = []
        for tool in _TOOLS:
            if tool.base_name in exclude:
                continue
            if tool.base_name == "execute" and not Config.LOCAL_SHELL_ENABLED:
                continue
            if tool.base_name == "task" and not Config.SUBAGENT_ENABLED:
                continue
            tools.append(tool.to_litellm_tool(self.namespace))
        return tools

    def is_local_tool(self, name: str) -> bool:
        return name in self._by_prefixed

    def get(self, name: str) -> Optional[LocalTool]:
        return self._by_prefixed.get(name)

    def awaits_response(self, name: str) -> bool:
        tool = self._by_prefixed.get(name)
        return bool(tool and tool.awaits_response)

    def is_hub_managed(self, name: str) -> bool:
        tool = self._by_prefixed.get(name)
        return bool(tool and tool.hub_managed)

    def prefixed(self, base_name: str) -> str:
        return f"{base_name}_{self.namespace}"

    async def execute(self, name: str, args: Dict[str, Any], ctx: LocalToolContext) -> Dict[str, Any]:
        tool = self._by_prefixed.get(name)
        if not tool:
            return {"error": f"Local tool '{name}' not found"}
        if tool.handler is None:
            return {"error": f"Local tool '{name}' has no inline handler"}
        from app.services.tool_policy import local_tool_allowed

        if not local_tool_allowed(tool.base_name, ctx.mode):
            return {
                "error": f"Local tool '{name}' is disabled in workflow mode",
                "errorCode": "INPUT_WRITE_FORBIDDEN",
            }
        if tool.base_name.endswith("_output") and ctx.output_backend is None:
            return {"error": "No durable output store is bound"}
        try:
            logged_args = args
            if tool.base_name == "write_output" and "content" in args:
                logged_args = {
                    key: value for key, value in args.items() if key != "content"
                }
                logged_args["contentChars"] = len(str(args.get("content") or ""))
            elif tool.base_name == "edit_output":
                logged_args = {
                    key: value
                    for key, value in args.items()
                    if key not in {"old_string", "new_string"}
                }
                logged_args["oldStringChars"] = len(str(args.get("old_string") or ""))
                logged_args["newStringChars"] = len(str(args.get("new_string") or ""))
            logger.info(
                "Executing local tool %s args=%s",
                name,
                json.dumps(logged_args)[:300],
            )
            return await tool.handler(args, ctx)
        except workspace_manager.WorkspaceError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            from app.services.storage.base import StorageError

            if isinstance(exc, StorageError):
                return {"error": exc.message, "errorCode": exc.code}
            logger.error("Local tool %s failed: %s", name, exc)
            return {"error": f"Local tool '{name}' failed"}


# Shared default instance.
local_tool_provider = LocalToolProvider()
