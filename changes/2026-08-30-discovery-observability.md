# Discovery observability without warning noise

**Date:** 2026-08-30
**Branch / PR:** `fix/discovery-observability` (PR pending) → `on-going-dev`

## What changed

- Missing optional discovery directories (`ENOENT`) now log at debug level.
  Every other directory or entry `OSError` remains a warning, including
  permission, stale-handle, type, descriptor-limit, and I/O failures.
- `WorkspaceReader` records normalized symlink paths once per collection.
  Repeated discovery passes no longer repeat the same warning.
- A completed manifest receives one count-only note when symlinks were skipped;
  individual paths remain in operator logs. A collection with no symlinks gets
  no note.
- `DiscoveryContext` is now the structural lifecycle boundary: entering it
  begins collection, and exiting always transfers diagnostics and closes the
  workspace reader, including when an adapter raises.
- Contributor and documentation ownership guidance now matches the active
  `on-going-dev` integration flow and shipped hook/path-policy behavior.

## Why

Healthy workspaces commonly omit provider-specific rule, skill, or command
directories. Warning for every absent optional root buried genuine discovery
failures. Conversely, strict symlink rejection was repeated by each overlapping
walk and was visible only to operators. Per-collection deduplication preserves
the security signal while making both logs and caller-visible diagnostics
useful for arbitrary workflow layouts.

## Migration / breaking

None. Discovery results and strict symlink rejection are unchanged. Only
diagnostic severity, repetition, and manifest notes change.

## Verification

- Focused Python 3.11 discovery, hook, and adapter suite: 97 passed.
- Full Python 3.11 suite: 689 passed.
