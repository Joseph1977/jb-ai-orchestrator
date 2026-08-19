# Readme restructure and root test hygiene

- **Date:** 2026-03-04
- **Branch:** `chore/docs-and-test-hygiene`
- **Target:** `on-going-dev`

## What changed

**Root test scripts.** Five `test_*.py` files and a demo script sat at the
repository root beside the real suite in `tests/`, and three of them were being
collected by pytest:

- Deleted `test_simple.py` (a license header and nothing else),
  `test_config.py` and `test_named_servers.py` (print-based scripts that caught
  their own exceptions, so they could never fail), and `demo_multi_server.py`
  (a mock print demo whose hand-written stubs contradicted the current universal
  attribution behaviour).
- Moved `test_service.py` to `scripts/smoke_service.py` and `test_agui.py` to
  `scripts/smoke_agui.py`, alongside the existing
  `scripts/orchestrator_continuity_exercise.py`. Both now honour
  `PY_MAIN_AGENT_BASE_URL`; `smoke_service.py` lost a hardcoded internal
  gateway URL and its `test_`-prefixed function names, and `smoke_agui.py` lost
  mojibake left by a Windows encoding round trip.
- Deleted the root `conftest.py`, which existed only to mark `test_service.py`
  as `live`.
- Added `tests/test_config_mcp_servers.py` (23 tests). `_parse_mcp_servers` had
  no coverage in `tests/` at all. The tests pin both supported forms, blank-name
  defaulting, numbered discovery and its stop-at-first-gap behaviour, malformed
  entry skipping, precedence, the unsupported forms, and the array form's
  rougher edges — an entry missing `url` reaching validation rather than being
  skipped, and a mixed array raising `AttributeError`.
- Added `testpaths = tests` to `pytest.ini`, which is the durable fix: a stray
  root `test_*.py` is now ignored instead of silently collected. The `live`
  marker and its exclusion are retained as a convention for future integration
  tests, though no test currently carries it.

Suite before: 445 passed, 4 deselected. After: 465 passed, 0 deselected.

**Readme.** Restructured into an open-source front door — positioning, a
differentiators section, a copy-paste quick start, a flow diagram, grouped
features, and a delivery-model comparison against Claude Code, the Cursor agent
and LangChain Deep Agents — followed by the caller-facing reference. Removed
three overlapping restatements of MCP tool attribution (including a Before/After
section about an internal change), the duplicated heartbeat and close variables,
and the multi-instance development history, which now reads as the current
guarantee. Env vars became grouped tables. Emoji headings were dropped.

Accuracy fixes verified against the code:

- The database section claimed no migrations were needed. The container image
  always runs `alembic upgrade head`, `init_db()` also issues `create_all`, and
  bare-metal runs must migrate explicitly.
- The dependency list named `litellm`, which is not a requirement — the service
  calls LiteLLM over HTTP with `httpx` — and named `pydantic`, which arrives
  transitively via FastAPI, while omitting `ag-ui-protocol`, `jsonschema`,
  `PyYAML`, `azure-storage-blob` and `requests`. It now describes what the
  service talks to and points at `src/requirements.txt`.
- Two documented MCP configuration formats do not exist in the parser: an array
  of bare URL strings, and the single-server `MCP_SERVER_URL` variable. Both are
  now documented as unsupported and pinned by tests.
- A plain `curl` command was labelled as a PowerShell example.
- Setup told readers to edit `src/.env/localhost/.env`, a gitignored path that
  does not exist on a fresh clone, without mentioning the tracked example.

**Env example.** Renamed `src/.env/localhost/.env.example-multi-server` to
`.env.example` and reworked it, because copying it produced a service that could
not start: its active `MCP_SERVER_URLS` used the unsupported bare-URL array form
and `DATABASE_URL` was missing entirely. It also carried a 64-hex-character
`LITELLM_API_KEY` and internal gateway hostnames, now placeholders. The trailing
notes describing auto-generated `mcp1`/`mcp2` server ids were replaced with the
current universal-attribution behaviour.

**Multi-server doc.** Scrubbed the historical "Testing" list naming the deleted
scripts, repaired a corrupted code fence with documentation prose spliced into
an env example, removed the duplicated "Step 1: Collection" heading and the
`xxx` placeholders, and corrected the legacy-format and backward-compatibility
claims. Updated the renamed example path in `DOCUMENTATION_INDEX.md` and
`DOCS_ORGANIZATION_SUMMARY.md`.

**Process memory.** `dev-memory.json` claimed a `RUN_MIGRATIONS_ON_START`
variable controls container migrations; no such variable exists. Corrected
alongside the readme, and the resume anchor was still pointing at
`chore/ban-output-attribution`.

**Review corrections.** Review of the first draft of this change found further
problems, fixed here:

- The MCP parsing description was itself inaccurate. It described the numbered
  form's forgiving behaviour as if it applied to both forms. In fact an array
  entry missing `url` survives parsing and is rejected later by
  `validate_config()`, an array mixing objects and bare strings raises
  `AttributeError` rather than a configuration error, and an unsupported array
  falls through to the numbered variables when those are set — so "leaves the
  server list empty" only holds when no numbered variables exist. The readme and
  `MULTI_SERVER_IMPLEMENTATION.md` now carry a per-input table, and tests pin
  every row. The parser itself is unchanged; hardening the mixed-array crash into
  a clear configuration error is left as a separate change.
- The quick start prepared `src/.env/localhost/.env` and then told readers to run
  Docker, which reads `src/.env/docker/.env` instead — so the container path was
  never actually configured. Split into explicit Docker and local paths, each
  preparing the file it reads, verified against `docker-compose.yml`
  (`env_file: ./src/.env/docker/.env`, `ENV=docker`).
- Section pointers were wrong: migration and backfill is `ORCHESTRATOR.md` §12,
  not §13 (§13 is the schema summary), and configuration is §11, not §7 (§7 is
  subagents). Corrected in the readme, `dev-memory.json` and
  `DOCS_ORGANIZATION_SUMMARY.md`. `DOCUMENTATION_INDEX.md` still named readme
  sections this change had removed.
- The comparison table said Deep Agents keeps its checkpointer "in your process",
  which understates LangGraph: checkpointers and stores can be external and
  durable. Reworded to "application-selected checkpointer or store", with the
  distinction restated as what ships built in here rather than what others lack.
- Both smoke checks could exit 0 while failing. `smoke_service.py` printed health
  and `/getTools` errors without raising, and required a tool call for a question
  answerable without one. It also hardcoded `google_search_tool` and
  `brave_search_tool`, as `smoke_agui.py` hardcoded `weather_general` — names no
  deployment is guaranteed to have, and an unknown name fails tool selection for
  the whole run. Tool names now come from `/getTools`, or from
  `PY_MAIN_AGENT_SMOKE_TOOLS` and `PY_MAIN_AGENT_SMOKE_MCP_TOOL`; the model is
  `PY_MAIN_AGENT_SMOKE_MODEL`, defaulting to `gpt-4o` rather than the retired
  `gpt-3.5-turbo`.
- The comment above the array branch in `src/app/config.py` still advertised
  "array of strings" support the parser does not implement.

A second review pass then found the remaining documentation edges:

- The quick start still said a bare URL array fails at startup, contradicting the
  table further down: it is ignored, so it only fails when no numbered variables
  exist. Qualified, and an empty `MCP_SERVER_URLS=[]` — which falls through the
  same way — was added to both tables and to the tests.
- Local setup started the service before mentioning migrations. `alembic upgrade
  head` is now part of the local command sequence, with the detail that matters
  for copy-paste: it runs from the repository root, where `alembic.ini` lives, and
  `ENV` selects which `src/.env/<env>/.env` supplies `DATABASE_URL`.
- The logging list in `MULTI_SERVER_IMPLEMENTATION.md` still claimed conflict
  resolutions are logged; universal attribution means there are none to log.
- `smoke_service.py`'s tool check is now named for what it verifies — that a
  request naming specific tools is accepted — rather than implying it asserts a
  tool was invoked, which is a model choice and not a property of the service.
- The claim that the readme got shorter is gone: it is 744 lines against the
  original 732. Reordering and correcting it was the point; the length goal was
  not met and `dev-memory.json` now says so.

Not addressed: `smoke_agui.py` still waits a fixed 250 ms before publishing to the
global SSE listener, so it could flake on a slow machine. That predates this
change and is a script-only concern.

No CI workflow is included; that was a deliberate decision, not an omission.

## Why

The readme read as internal engineering notes: the feature list led with the
health-check endpoint and buried folder orchestration, the quick start was at
the bottom and was really a third copy of the MCP env formats, and several
documented behaviours no longer matched the code. Separately, root-level test
scripts made it unclear where tests live, and three of them ran in the suite
while asserting nothing.

## Migration or breaking notes

No API, schema, or configuration behaviour changed.

- `src/.env/localhost/.env.example-multi-server` is now `.env.example`. Existing
  local `.env` files are untouched.
- Anyone invoking `python test_agui.py` or `python test_service.py` should use
  `python scripts/smoke_agui.py` and `python scripts/smoke_service.py`.
- Tracked environment files still contain a live-looking LiteLLM key
  (`src/.env/docker/.env`) and internal hostnames. That remediation is
  deliberately not in this change; it is recorded under `open_items` in
  `dev-memory.json` and must be resolved before publication. The key is already
  in git history, so it needs rotating regardless.
