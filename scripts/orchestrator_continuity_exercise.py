#!/usr/bin/env python3
# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Exercise shared-folder input/output continuity through the live orchestrator API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional


DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create host-visible input and OUTPUT folders under the shared mount, "
            "then initiate, execute, close, re-initiate with the same output, "
            "execute again, and verify continuity."
        ),
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--host-shared-root",
        type=Path,
        default=os.getenv("WORKSPACES_HOST_DIR"),
        help="Host folder mounted into the orchestrator (or WORKSPACES_HOST_DIR).",
    )
    parser.add_argument(
        "--shared-uri-root",
        default="/workspaces",
        help="The same shared mount path as seen by the orchestrator.",
    )
    parser.add_argument(
        "--exercise-name",
        default=".continuity-exercise",
        help="Safe child folder created below the shared root.",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Perform live calls. Without this flag, only print the planned flow.",
    )
    parser.add_argument(
        "--simulate-execute",
        action="store_true",
        help="Still call execute, but ask for read-only verification and create output markers locally.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional execute model override.",
    )
    parser.add_argument("--close-retries", type=int, default=10)
    parser.add_argument("--close-delay-sec", type=float, default=0.5)
    return parser.parse_args()


def _request(
    method: str,
    url: str,
    payload: Optional[dict[str, Any]] = None,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        status = exc.code
    try:
        parsed = json.loads(body) if body else {}
    except json.JSONDecodeError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def _safe_exercise_name(raw: str) -> str:
    value = raw.strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise RuntimeError("exercise name must be one safe folder segment")
    return value


def _prepare_workspace(host_root: Path, exercise_name: str) -> tuple[Path, Path]:
    root = host_root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError("host shared root does not exist")
    exercise = root / _safe_exercise_name(exercise_name)
    input_dir = exercise / "input"
    output_dir = exercise / "OUTPUT"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "AGENTS.md").write_text(
        "# Continuity exercise\n"
        "Input is read-only. Durable files must be written with write_output_local.\n",
        encoding="utf-8",
    )
    (input_dir / "immutable.txt").write_text("input-immutable\n", encoding="utf-8")
    return input_dir, output_dir


def _shared_uri(host_root: Path, path: Path, shared_uri_root: str) -> str:
    relative = path.resolve().relative_to(host_root.expanduser().resolve())
    return f"{shared_uri_root.rstrip('/')}/{relative.as_posix()}"


def _initiate(base_url: str, input_uri: str, output_uri: str) -> str:
    status, body = _request(
        "POST",
        f"{base_url.rstrip('/')}/v1/orchestrator/initiate",
        {
            "input": {
                "type": "shared_folder",
                "uri": input_uri,
                "relativePath": ".",
                "materialization": "in_place_read_only",
            },
            "output": {
                "type": "shared_folder",
                "uri": output_uri,
                "relativePath": ".",
            },
            "mode": "workflow",
        },
    )
    guid = body.get("orchestratorGuid")
    if status >= 400 or not body.get("success") or not guid:
        raise RuntimeError(f"initiate failed with HTTP {status}")
    return str(guid)


def _execute(
    base_url: str,
    orchestrator_guid: str,
    *,
    filename: str,
    model: Optional[str],
    simulate: bool,
) -> None:
    if simulate:
        prompt = "Read immutable.txt and report its exact content. Do not write any file."
    else:
        prompt = (
            "Read immutable.txt. Then use write_output_local to write "
            f"{filename} as JSON with exactly "
            f'{{"input":"input-immutable","segment":"{filename}","continuity":true}}.'
        )
    payload: dict[str, Any] = {
        "orchestratorGuid": orchestrator_guid,
        "prompt": prompt,
    }
    if model:
        payload["model"] = model
    status, body = _request(
        "POST",
        f"{base_url.rstrip('/')}/v1/orchestrator/execute",
        payload,
    )
    if status >= 400 or not body.get("success"):
        raise RuntimeError(f"execute failed with HTTP {status}")


def _close_with_retry(
    base_url: str,
    orchestrator_guid: str,
    *,
    retries: int,
    delay_sec: float,
) -> None:
    url = f"{base_url.rstrip('/')}/v1/orchestrator/{orchestrator_guid}/close"
    for attempt in range(1, retries + 1):
        status, body = _request("POST", url, {})
        if status == 200 and body.get("status") == "closed":
            return
        if status != 202:
            raise RuntimeError(f"close failed with HTTP {status}")
        if attempt < retries:
            time.sleep(delay_sec)
    raise RuntimeError("close remained in progress beyond the retry limit")


def _write_simulated_marker(output_dir: Path, filename: str) -> None:
    (output_dir / filename).write_text(
        json.dumps(
            {
                "input": "input-immutable",
                "segment": filename,
                "continuity": True,
            }
        ),
        encoding="utf-8",
    )


def _verify(input_dir: Path, output_dir: Path) -> None:
    if (input_dir / "immutable.txt").read_text(encoding="utf-8") != "input-immutable\n":
        raise RuntimeError("input directory was mutated")
    for filename in ("session-a.json", "session-b.json"):
        artifact = output_dir / filename
        if not artifact.is_file():
            raise RuntimeError(f"durable output {filename} is missing")
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        if payload.get("continuity") is not True:
            raise RuntimeError(f"durable output {filename} has unexpected content")


def _run_segment(
    *,
    base_url: str,
    input_uri: str,
    output_uri: str,
    output_dir: Path,
    filename: str,
    model: Optional[str],
    simulate: bool,
    close_retries: int,
    close_delay_sec: float,
) -> str:
    guid = _initiate(base_url, input_uri, output_uri)
    try:
        _execute(
            base_url,
            guid,
            filename=filename,
            model=model,
            simulate=simulate,
        )
        if simulate:
            _write_simulated_marker(output_dir, filename)
    finally:
        _close_with_retry(
            base_url,
            guid,
            retries=close_retries,
            delay_sec=close_delay_sec,
        )
    return guid


def main() -> int:
    args = _parse_args()
    if args.host_shared_root is None:
        raise RuntimeError("--host-shared-root or WORKSPACES_HOST_DIR is required")

    host_root = args.host_shared_root.expanduser().resolve()
    input_dir, output_dir = _prepare_workspace(host_root, args.exercise_name)
    input_uri = _shared_uri(host_root, input_dir, args.shared_uri_root)
    output_uri = _shared_uri(host_root, output_dir, args.shared_uri_root)

    print("Orchestrator continuity exercise")
    print(f"  host input:  {input_dir}")
    print(f"  host output: {output_dir}")
    print(f"  agent input: {input_uri}")
    print(f"  agent output:{output_uri}")
    print(f"  mode:        {'live' if args.run else 'dry-run'}")
    if not args.run:
        print("Dry-run complete. Add --run to call the live orchestrator.")
        return 0

    guid_a = _run_segment(
        base_url=args.base_url,
        input_uri=input_uri,
        output_uri=output_uri,
        output_dir=output_dir,
        filename="session-a.json",
        model=args.model,
        simulate=args.simulate_execute,
        close_retries=args.close_retries,
        close_delay_sec=args.close_delay_sec,
    )
    if not (output_dir / "session-a.json").is_file():
        raise RuntimeError("first durable output disappeared after close")

    guid_b = _run_segment(
        base_url=args.base_url,
        input_uri=input_uri,
        output_uri=output_uri,
        output_dir=output_dir,
        filename="session-b.json",
        model=args.model,
        simulate=args.simulate_execute,
        close_retries=args.close_retries,
        close_delay_sec=args.close_delay_sec,
    )
    if guid_a == guid_b:
        raise RuntimeError("re-initiate did not return a new orchestrator id")
    _verify(input_dir, output_dir)
    print("continuity checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
