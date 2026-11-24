#!/usr/bin/env python3
"""Capture and compare a repository state around an untrusted subagent run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 2
RECOVERY_MARKER = re.compile(
    rb"^Recovery: skill-integrity-failed "
    rb"phase=(detected|skill-restored|repo-checked) "
    rb"baseline=[0-9a-f]{64} "
    rb"current=([0-9a-f]{64}|unavailable) "
    rb"failure=(none|symlink|special|unreadable|traversal|digest|pre-capture|planted-marker) "
    rb"commit=([0-9a-f]{40,64}|none) "
    rb"pre-head=([0-9a-f]{40,64}|unknown) "
    rb"repo-check=(not-applicable|pending|passed|failed) "
    rb"prior=(none|commit-check-failed) "
    rb"guard=(\S+) guard-sha=([0-9a-f]{64}|none) "
    rb"commit-manifest=(\S+) commit-manifest-sha=([0-9a-f]{64}|none)$"
)


class GuardError(RuntimeError):
    """Raised when a complete, trustworthy snapshot cannot be produced."""


def _git(repo: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise GuardError(f"git {' '.join(args)} failed: {message}")
    return result.stdout


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise GuardError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _normalize_allowed(repo: Path, value: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        absolute = Path(os.path.abspath(candidate))
    else:
        absolute = Path(os.path.abspath(repo / candidate))
    try:
        relative = absolute.relative_to(repo)
    except ValueError as exc:
        raise GuardError(f"allowed path is outside repository: {value}") from exc
    if relative == Path("."):
        raise GuardError("repository root cannot be the allowed path")
    return relative.as_posix()


def _normalized_recovery_hashes(path: Path) -> list[str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GuardError(f"cannot read recovery status {path}: {exc}") from exc
    lines = data.splitlines(keepends=True)
    marker_indexes = [
        index
        for index, line in enumerate(lines)
        if line.rstrip(b"\r\n").startswith(b"Recovery:")
    ]
    if len(marker_indexes) != 1:
        raise GuardError(f"{path} must contain exactly one recovery marker")
    index = marker_indexes[0]
    marker = lines[index].rstrip(b"\r\n")
    matched = RECOVERY_MARKER.fullmatch(marker)
    if not matched:
        raise GuardError(f"{path} has a malformed skill-integrity recovery marker")
    if matched.group(3) in {b"pre-capture", b"planted-marker"} and (
        matched.group(1) != b"detected" or matched.group(6) != b"failed"
    ):
        raise GuardError(f"{path} has a nonterminal terminal-failure marker")
    normalized = b"".join(lines[:index] + lines[index + 1 :])
    candidates = {_sha256_bytes(normalized)}
    if normalized.endswith(b"\r\n"):
        candidates.add(_sha256_bytes(normalized[:-2]))
    elif normalized.endswith(b"\n"):
        candidates.add(_sha256_bytes(normalized[:-1]))
    return sorted(candidates)


def _entry(path: Path, relative: str, *, normalize_recovery: bool = False) -> dict[str, Any]:
    try:
        details = path.lstat()
    except OSError as exc:
        raise GuardError(f"cannot stat {path}: {exc}") from exc

    mode = stat.S_IMODE(details.st_mode)
    if stat.S_ISREG(details.st_mode):
        result = {
            "path": relative,
            "type": "file",
            "mode": mode,
            "sha256": _sha256_file(path),
        }
        if normalize_recovery:
            result["recovery_sha256_candidates"] = _normalized_recovery_hashes(path)
        return result
    if stat.S_ISLNK(details.st_mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise GuardError(f"cannot read symlink {path}: {exc}") from exc
        return {
            "path": relative,
            "type": "symlink",
            "mode": mode,
            "target": target,
        }
    if stat.S_ISDIR(details.st_mode):
        return {"path": relative, "type": "directory", "mode": mode}
    return {
        "path": relative,
        "type": "special",
        "mode": mode,
        "device": details.st_rdev,
        "size": details.st_size,
        "mtime_ns": details.st_mtime_ns,
    }


def _walk(
    repo: Path, allowed: str, recovery_status: str | None = None
) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}

    def visit(directory: Path, prefix: str) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise GuardError(f"cannot scan {directory}: {exc}") from exc

        for child in children:
            relative = f"{prefix}/{child.name}" if prefix else child.name
            if relative == ".git" or relative.startswith(".git/"):
                continue
            if relative == allowed:
                continue

            path = Path(child.path)
            item = _entry(
                path,
                relative,
                normalize_recovery=relative == recovery_status,
            )
            entries[relative] = item
            if item["type"] == "directory":
                visit(path, relative)

    visit(repo, "")
    return entries


def _resolve_git_path(repo: Path, value: bytes) -> Path:
    path = Path(value.decode().strip())
    if not path.is_absolute():
        path = repo / path
    return path.resolve(strict=False)


def _snapshot_control_path(path: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    key = str(path)
    if not os.path.lexists(path):
        entries[key] = {"path": key, "type": "missing"}
        return entries

    def visit(current: Path) -> None:
        current_key = str(current)
        item = _entry(current, current_key)
        entries[current_key] = item
        if item["type"] != "directory":
            return
        try:
            children = sorted(os.scandir(current), key=lambda child: child.name)
        except OSError as exc:
            raise GuardError(f"cannot scan Git control path {current}: {exc}") from exc
        for child in children:
            visit(Path(child.path))

    visit(path)
    return entries


def _git_control(repo: Path) -> dict[str, Any]:
    git_dir = _resolve_git_path(repo, _git(repo, "rev-parse", "--absolute-git-dir"))
    common_dir = _resolve_git_path(repo, _git(repo, "rev-parse", "--git-common-dir"))
    hooks_output = _git(repo, "config", "--path", "--get", "core.hooksPath", check=False)
    hooks_path = (
        _resolve_git_path(repo, hooks_output)
        if hooks_output.strip()
        else (common_dir / "hooks").resolve(strict=False)
    )

    control_paths = {common_dir, git_dir, hooks_path}
    git_marker = repo / ".git"
    try:
        marker_mode = git_marker.lstat().st_mode
    except OSError as exc:
        raise GuardError(f"cannot stat Git marker {git_marker}: {exc}") from exc
    if not stat.S_ISDIR(marker_mode):
        control_paths.add(git_marker)
    entries: dict[str, dict[str, Any]] = {}
    for path in sorted(control_paths, key=str):
        entries.update(_snapshot_control_path(path))

    return {
        "git_dir": str(git_dir),
        "common_dir": str(common_dir),
        "hooks_path": str(hooks_path),
        "effective_config_sha256": _sha256_bytes(
            _git(repo, "config", "--null", "--list", "--show-origin", "--show-scope")
        ),
        "hooks_entries": _snapshot_control_path(hooks_path),
        "entries": entries,
    }


def capture(
    repo_value: str, allowed_value: str, recovery_status_value: str | None = None
) -> dict[str, Any]:
    repo = Path(repo_value).resolve(strict=True)
    if not (repo / ".git").exists():
        raise GuardError(f"not a Git working tree: {repo}")

    allowed = _normalize_allowed(repo, allowed_value)
    recovery_status = (
        _normalize_allowed(repo, recovery_status_value)
        if recovery_status_value is not None
        else None
    )
    if recovery_status == allowed:
        raise GuardError("recovery status cannot be the subagent-owned allowed path")
    head = _git(repo, "rev-parse", "--verify", "HEAD").decode().strip()
    branch_result = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    branch = (
        branch_result.stdout.decode().strip()
        if branch_result.returncode == 0
        else f"DETACHED@{head}"
    )

    return {
        "version": MANIFEST_VERSION,
        "repo": str(repo),
        "allowed": allowed,
        "git": {
            "head": head,
            "branch": branch,
            "index_sha256": _sha256_bytes(_git(repo, "ls-files", "--stage", "-z")),
            "flags_sha256": _sha256_bytes(_git(repo, "ls-files", "-v", "-z")),
            "refs_sha256": _sha256_bytes(
                _git(
                    repo,
                    "for-each-ref",
                    "--format=%(refname)%00%(objectname)%00%(symref)%00",
                )
            ),
            "control": _git_control(repo),
        },
        "entries": _walk(repo, allowed, recovery_status),
    }


def _canonical_json(manifest: dict[str, Any]) -> bytes:
    return (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def write_manifest(manifest: dict[str, Any], output_value: str) -> str:
    output = Path(output_value)
    output.parent.mkdir(parents=True, exist_ok=True)
    data = _canonical_json(manifest)
    try:
        output.write_bytes(data)
    except OSError as exc:
        raise GuardError(f"cannot write manifest {output}: {exc}") from exc
    return _sha256_bytes(data)


def load_manifest(path_value: str, expected_sha256: str) -> dict[str, Any]:
    path = Path(path_value)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GuardError(f"cannot read baseline {path}: {exc}") from exc
    actual = _sha256_bytes(data)
    if actual != expected_sha256:
        raise GuardError(
            f"baseline integrity failure: expected {expected_sha256}, got {actual}"
        )
    try:
        manifest = json.loads(data)
    except json.JSONDecodeError as exc:
        raise GuardError(f"invalid baseline JSON: {exc}") from exc
    if manifest.get("version") != MANIFEST_VERSION:
        raise GuardError(f"unsupported manifest version: {manifest.get('version')}")
    return manifest


def compare(
    before: dict[str, Any], after: dict[str, Any], *, semantic_git: bool = False
) -> dict[str, Any]:
    before_entries = before["entries"]
    after_entries = after["entries"]
    before_paths = set(before_entries)
    after_paths = set(after_entries)

    def entries_match(before_entry: dict[str, Any], after_entry: dict[str, Any]) -> bool:
        candidates = after_entry.get("recovery_sha256_candidates")
        if candidates is None:
            return before_entry == after_entry
        normalized_after = dict(after_entry)
        normalized_after.pop("recovery_sha256_candidates", None)
        normalized_after["sha256"] = before_entry.get("sha256")
        return before_entry.get("sha256") in candidates and before_entry == normalized_after

    changed = sorted(
        path
        for path in before_paths & after_paths
        if not entries_match(before_entries[path], after_entries[path])
    )
    git_changes: dict[str, Any] = {}
    if semantic_git:
        for key in ("head", "branch", "index_sha256", "flags_sha256", "refs_sha256"):
            if before["git"][key] != after["git"][key]:
                git_changes[key] = {
                    "before": before["git"][key],
                    "after": after["git"][key],
                }
        before_control = before["git"]["control"]
        after_control = after["git"]["control"]
        for key in ("hooks_path", "effective_config_sha256", "hooks_entries"):
            if before_control[key] != after_control[key]:
                git_changes[f"control.{key}"] = {
                    "before": before_control[key],
                    "after": after_control[key],
                }
        return {
            "same": not git_changes
            and not (after_paths - before_paths)
            and not (before_paths - after_paths)
            and not changed,
            "git": git_changes,
            "added": sorted(after_paths - before_paths),
            "removed": sorted(before_paths - after_paths),
            "changed": changed,
        }

    for key in before["git"]:
        if before["git"][key] == after["git"][key]:
            continue
        if key != "control":
            git_changes[key] = {
                "before": before["git"][key],
                "after": after["git"][key],
            }
            continue

        before_control = before["git"]["control"]
        after_control = after["git"]["control"]
        before_control_entries = before_control["entries"]
        after_control_entries = after_control["entries"]
        before_control_paths = set(before_control_entries)
        after_control_paths = set(after_control_entries)
        metadata = {
            metadata_key: {
                "before": before_control[metadata_key],
                "after": after_control[metadata_key],
            }
            for metadata_key in before_control
            if metadata_key != "entries"
            and before_control[metadata_key] != after_control[metadata_key]
        }
        git_changes["control"] = {
            "metadata": metadata,
            "added": sorted(after_control_paths - before_control_paths),
            "removed": sorted(before_control_paths - after_control_paths),
            "changed": sorted(
                path
                for path in before_control_paths & after_control_paths
                if before_control_entries[path] != after_control_entries[path]
            ),
        }
    return {
        "same": not git_changes
        and not (after_paths - before_paths)
        and not (before_paths - after_paths)
        and not changed,
        "git": git_changes,
        "added": sorted(after_paths - before_paths),
        "removed": sorted(before_paths - after_paths),
        "changed": changed,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--repo", required=True)
    capture_parser.add_argument("--allow", required=True)
    capture_parser.add_argument("--output", required=True)

    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--repo", required=True)
    compare_parser.add_argument("--allow", required=True)
    compare_parser.add_argument("--baseline", required=True)
    compare_parser.add_argument("--baseline-sha256", required=True)
    compare_parser.add_argument("--recovery-status")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "capture":
            digest = write_manifest(capture(args.repo, args.allow), args.output)
            print(digest)
            return 0

        before = load_manifest(args.baseline, args.baseline_sha256)
        after = capture(args.repo, args.allow, args.recovery_status)
        if before["repo"] != after["repo"] or before["allowed"] != after["allowed"]:
            raise GuardError("baseline repo or allowed path does not match compare request")
        result = compare(before, after, semantic_git=args.recovery_status is not None)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if result["same"] else 1
    except GuardError as exc:
        print(f"repo_guard: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
