# Select model and tool budget on resume

**Date:** 2026-09-06
**Branch / PR:** `feature/resume-model-tool-budget` / pending

## What changed

Orchestrator, AG-UI, and legacy direct-agent resume requests can now select the
model and per-segment tool budget. Resolution remains compatibility-first:
request override, persisted await snapshot, initiated session config where
available, then the existing model or tool-hub default.

Effective overrides persist in the next await snapshot. AG-UI uses the same
resolved model for hook-permission replay and the resumed segment. Its OpenAPI
model default is now null so omission preserves the snapshot instead of
implicitly overriding it with `gpt-3.5-turbo`.

## Why

Previously, a resumed segment was pinned to model and budget values in its
snapshot. Callers could change configuration for fresh runs but had no way to
move an existing awaiting session to that configuration.

## Migration and compatibility

No database migration is required. Callers that omit the new fields retain
snapshot-first behavior. Cross-provider resume replays persisted history
without translation. A zero tool budget remains valid, matching execute, and
returns structured `MAX_TOOL_CALLS` exhaustion without executing a tool.

## Verification

- Focused resume, AG-UI, lifecycle, tool-counter, and subagent tests: 95 passed.
- Full orchestrator suite: 784 passed.
- A live AG-UI resume with `maxToolCalls: 0` emitted exactly:
  `MAX_TOOL_CALLS: The workflow run stopped after reaching its tool limit. The session and any pending question remain available; try again.`
- Retrying the same preserved interrupt with `maxToolCalls: 50` completed with
  `RESUME_OVERRIDE_OK`.
- Closing the verification thread removed its runtime and copied workspace.
