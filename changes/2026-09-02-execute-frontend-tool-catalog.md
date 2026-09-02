# Execute names its frontend interaction tools

- **Date:** 2026-09-02
- **Branch / PR:** `fix/execute-frontend-tool-catalog` (jb-ai-orchestrator)
- **What changed:** `/v1/orchestrator/execute` now composes its authoritative
  system prompt through `build_authoritative_system_content()`, adding the
  frontend interaction guidance and the deduplicated `frontendTools` names after
  the harness prompt and its resolved run-binding block. The AG-UI relay already
  did this; both channels now state the same contract.
- **Why:** Execute passed the tool schemas to the model but never named the
  tools in the prompt. A workflow step that requires invoking a matching
  interaction tool when one is available had nothing authoritative to match
  against, and a multiple-choice question came back as plain Markdown instead of
  an interaction hold. Because a resumed run replays its persisted system
  message, the omission also survived every resume of that run.
- **Limitation:** The catalog raises adherence; it cannot guarantee a tool call.
  Callers that depend on structured interaction must still handle plain
  assistant text.
- **Migration / breaking note:** No migration. The prompt gains sections only
  when the caller sends `frontendTools`; requests without them are unchanged.
  Runs already interrupted keep their persisted prompt, so the catalog reaches
  them only on the next fresh execute.
