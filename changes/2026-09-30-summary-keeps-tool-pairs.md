# Summarising old turns no longer orphans tool results

- **Date:** 2026-09-30
- **Branch:** `sync/generic-engine-clean`

## What changed

`context_compaction._summarize_old_turns` replaces older dialogue with one
summary block and keeps the older tool results. It kept them without the
assistant messages that made the calls, so every kept result was orphaned. It
now keeps each assistant message that made tool calls together with its results,
in order, and drops a result whose call is not in the history.

## Why

Providers that validate tool history reject an orphaned result with "messages
with role 'tool' must be a response to a preceding message with 'tool_calls'",
and the run ends. Providers that do not validate history accepted the broken
order silently.

## Migration / breaking

None. A summarised history is slightly larger than before, by the assistant
messages that made the kept calls.

Tests: `tests/test_context_compaction.py` checks a summarised history against
the required tool-call order, with the cut between a call and its result, and
checks that a result with no call is dropped. Both fail without the fix.
