# Align the recoverable tool-limit message

**Date:** 2026-09-06
**Branch / PR:** `fix/max-tool-calls-message-alignment` / PR #14
**Target:** `on-going-dev`

## What changed

AG-UI now uses the same `MAX_TOOL_CALLS` message as the web gateway, including
the promise that a pending question remains available. A source comment records
the cross-repository invariant, and an intent-level test pins the
pending-question recovery wording independently of the full literal.

This entry supersedes the shorter AG-UI wording recorded in
`2026-09-05-max-tool-calls-error.md`; that shipped record remains unchanged.

## Why

The gateway and AG-UI surfaces had drifted even though the public web
documentation says AG-UI prefixes the same safe text with the structured code.
The longer wording also reflects the resume lifecycle: exhaustion restores the
existing pending interaction rather than closing the session.

## Migration and compatibility

No migration or configuration change is required. The structured error code and
HTTP behavior are unchanged; only the public AG-UI text is aligned.

## Verification

- Focused AG-UI lifecycle tests: 46 passed.
- Full orchestrator suite: 768 passed.
- Full gateway suite: 207 passed.
- With both local caps set to `1`, plain AG-UI emitted exactly:
  `MAX_TOOL_CALLS: The workflow run stopped after reaching its tool limit. The session and any pending question remain available; try again.`
- Selected-workflow execute and resume emitted the exact bare text:
  `The workflow run stopped after reaching its tool limit. The session and any pending question remain available; try again.`
- The resumed segment retained the observed cap of `1`, exhausted cleanly after
  one local tool call, and restored the same pending interaction. A second
  answer attempt was accepted and exhausted cleanly again; the session remained
  active and awaiting response, with no execution left `RUNNING`.
- After restoring local settings, a fresh selected-workflow session used the
  normal cap (`50`) and completed after two local tool calls.
- Frontend tests and build were skipped because no frontend source contains
  these messages.
