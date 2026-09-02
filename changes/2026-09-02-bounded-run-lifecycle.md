# Bound model calls and run segments

- **Date:** 2026-09-02
- **Branch / PR:** `fix/bounded-run-lifecycle` (jb-ai-orchestrator)
- **What changed:** Model calls now have an absolute wall-clock deadline and a
  completion-token cap, while the complete model/tool segment has a larger
  deadline. Timeout, output-limit, conflict, and run-tracking failures are
  structured. Cancellation keeps the run claim until the worker stops, logs
  resistant cancellation, and cancels the worker if its heartbeat loop fails.
  Workspace and binding preparation run under the same heartbeat and segment
  guard on both orchestrator and AG-UI paths.
  Claim creation conditionally terminalizes heartbeat-stale crash/orphan rows
  across both execution and thread conflict scopes before checking for a fresh
  conflict.
- **Why:** An upstream stream could trickle forever while the independent
  heartbeat kept its run fresh, permanently blocking every later message on the
  same session with `RUN_CONFLICT`.
- **Migration / breaking note:** No database migration. Defaults are a 240-second
  model deadline, 270-second segment deadline, 4096 completion tokens, and a
  5-second slow-cancellation diagnostic threshold. A model `length` finish is
  now an `OUTPUT_LIMIT` failure instead of a successful partial result.
