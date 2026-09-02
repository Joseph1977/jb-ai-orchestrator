# Status interrupt replay

- **Date:** 2026-09-01
- **Branch / PR:** `feature/pending-interrupt-restore` / PR #7
- **What changed:** Awaiting orchestrator status responses now replay canonical
  pending interrupts and tool-call IDs from the latest persisted state.
- **Why:** Stateless callers need the interaction schema after browser remount
  so users can continue an existing hold without relying on a live SSE socket.
- **Migration / breaking note:** No migration. The response fields are additive;
  terminal status responses continue to omit pending interactions.
