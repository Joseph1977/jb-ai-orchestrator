# Discovery observability without warning noise

**Date:** 2026-08-30
**Branch / PR:** `fix/discovery-observability` (PR pending) → `on-going-dev`

## What changed

- Missing optional discovery paths (`ENOENT`) and failures beneath a confirmed
  symlink now log at debug level. Unrelated filesystem failures remain warnings,
  including permission, stale-handle, type, descriptor-limit, and I/O failures.
- `WorkspaceReader` records normalized symlink paths once per collection.
  Repeated discovery passes no longer repeat the same warning, and downstream
  failures caused by an already-rejected symlink log at debug.
- A directly symlinked hook file retains an `unreadable or unsafe` diagnostic.
  Hook probes beneath a symlinked ancestor do not create cascade diagnostics.
- A completed manifest receives one count-only note when symlinks were
  encountered and skipped; individual paths remain in operator logs. A
  collection with no symlinks gets no note.
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

- Focused Python 3.11 workspace-containment and hook suite: 81 passed.
- Full Python 3.11 suite: 697 passed.
