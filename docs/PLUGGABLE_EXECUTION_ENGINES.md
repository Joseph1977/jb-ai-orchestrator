# Pluggable Execution Engines

## Status

- Status: proposed architecture and execution plan
- Scope: `jb-ai-orchestrator` only
- Explicitly unchanged: `jb-web-code`
- Default engine after implementation: the current internal tool loop
- First external engine: OpenCode
- Provider strategy: keep the LiteLLM HTTP gateway for the internal engine and prefer it for OpenCode, without making OpenCode architecturally dependent on LiteLLM

## Decision

`jb-ai-orchestrator` remains the product control plane. It owns workspace lifecycle, playbook discovery, policy, persistence, AG-UI, resume, and public API behavior. The model/tool execution loop becomes a replaceable execution engine.

An environment variable selects the default engine, but each persisted run also records the engine that created it. A later environment change must never cause an OpenCode run to resume through the internal engine, or the reverse.

```text
Public API / AG-UI / workspace lifecycle / policy
                        |
                        v
                 ExecutionEngine
                  /            \
      InternalExecutionEngine   OpenCodeExecutionEngine
                 |                       |
            LiteLLM gateway      LiteLLM or native providers
```

## Why

The internal engine is the source of the orchestrator's differentiated behavior: durable pause/resume, AG-UI frontend tools, policy hooks, progressive playbook loading, context compaction, and workspace lifecycle. Keeping it as the reference implementation protects those capabilities.

OpenCode has a broader contributor base and a faster-moving coding-agent execution loop. Integrating it behind a stable contract lets the orchestrator benefit from that evolution without surrendering its own lifecycle, persistence, and user-experience features.

The current code already has a pluggable playbook-discovery layer, but it does not have a pluggable execution layer:

- `src/app/services/harness/` discovers Cursor, Claude Code, and generic playbooks.
- `src/app/services/tool_hub.py` implements the tool loop directly.
- `src/app/services/mcp_agent_service.py` calls the LiteLLM HTTP gateway directly.
- AG-UI streaming, interrupts, tool state, and resume behavior are embedded in that internal loop.

These are separate concerns and should remain separate.

## What this adds

### 1. Execution engine contract

Add an `ExecutionEngine` protocol responsible for one run segment. It receives a normalized segment request and returns either a completed segment or a durable await snapshot.

The contract covers:

- normalized messages and system prompt
- available local, MCP, and AG-UI tools
- streaming event callbacks
- cancellation and deadlines
- workspace and permission context
- usage, model, and error metadata
- capability reporting
- versioned completion and await results

The initial result schema is version 1 and is stored with the run so future engines can evolve without ambiguously interpreting old snapshots.

### 2. Internal engine adapter

Wrap the current `ToolExecutionHub.process_request()` behavior as `InternalExecutionEngine`. This is a structural extraction, not a behavior rewrite. Existing orchestrator, AG-UI, and legacy-agent flows continue to reach the same implementation through the new boundary.

### 3. Model client boundary

Extract LiteLLM HTTP access from `MCPAgentService` behind a `ModelClient` protocol. `LiteLLMModelClient` preserves the current `/v1/chat/completions` calls, streaming, timeouts, deadlines, and error mapping.

This boundary serves the internal engine and tests. It does not force every execution engine to use LiteLLM.

### 4. OpenCode execution engine

Add `OpenCodeExecutionEngine` as an adapter over an OpenCode sidecar/server. It translates the orchestrator's segment contract into OpenCode operations and translates OpenCode output back into the orchestrator's events and results.

OpenCode runs initially advertise a reduced capability set. They do not claim AG-UI interrupt parity until the compatibility tests prove it.

### 5. OpenCode playbook discovery

Add an OpenCode discovery adapter to the existing harness registry. This allows OpenCode instruction, skill, command, agent, and rule layouts to become the orchestrator's normal manifest format.

Discovery support and execution-engine support ship independently. Discovery support does not by itself execute anything through OpenCode.

### 6. Engine selection and persistence

Add configuration for the default engine and the OpenCode sidecar connection. Persist the resolved engine identity and contract version in run state.

Initial configuration:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ORCHESTRATOR_ENGINE` | `internal` | Default engine: `internal` or `opencode` |
| `OPENCODE_BASE_URL` | empty | OpenCode sidecar HTTP endpoint |
| `OPENCODE_REQUEST_TIMEOUT_SEC` | `300` | Sidecar request timeout |
| `OPENCODE_MODEL` | empty | Optional OpenCode model override |
| `OPENCODE_PROVIDER` | `litellm` | Preferred provider mode: `litellm` or `native` |

Missing OpenCode configuration produces a startup or request validation error only when `ORCHESTRATOR_ENGINE=opencode`. Internal-engine deployments remain valid without any OpenCode process.

## How the boundaries work

### Playbook discovery

`HarnessAdapter` answers: "What instructions and capabilities does this workspace contain?"

Its output remains a `HarnessManifest`, which the orchestrator renders into eager context plus a progressive catalog. OpenCode joins the existing Cursor, Claude Code, and generic adapters at this layer.

### Model access

`ModelClient` answers: "How does the internal engine call a model?"

`LiteLLMModelClient` remains the production implementation. Another OpenAI-compatible gateway can replace it later by configuration or a new client implementation. The orchestrator does not need the LiteLLM Python package.

### Execution

`ExecutionEngine` answers: "Who runs the tool loop for this segment?"

`InternalExecutionEngine` uses the orchestrator's tools, policy, compaction, hooks, and `ModelClient`. `OpenCodeExecutionEngine` delegates execution to OpenCode while the orchestrator retains lifecycle and API ownership.

### AG-UI and resume

The orchestrator continues to own AG-UI HTTP and SSE behavior. Engines report normalized events; controllers translate those events to AG-UI.

Durable resume requires the engine's await snapshot to contain enough state to continue on any replica. For the internal engine this is the current persisted message, tool, and interrupt state. For OpenCode it must be either:

- a complete snapshot translated into the orchestrator's versioned contract, or
- an explicit OpenCode session reference plus all data required to reattach, protected by the same tenant and workspace controls.

The second option is accepted only for capabilities OpenCode actually supports. A stored OpenCode session reference is not treated as equivalent to the internal engine's current Postgres resume snapshot.

## LiteLLM strategy

LiteLLM remains part of the internal architecture because the current tool loop depends on its OpenAI-compatible HTTP API and stable tool/streaming behavior. Replacing that HTTP endpoint with another compatible gateway is a model-client concern.

For OpenCode:

1. Prefer configuring OpenCode to call the same LiteLLM gateway. This keeps provider credentials, model aliases, routing, and operational controls in one place.
2. Allow OpenCode's native provider configuration when a deployment explicitly chooses it.
3. Keep provider selection inside the OpenCode adapter boundary so future engines can make their own provider choices.
4. Leave `jb-web-code` and its existing LiteLLM usage unchanged.

`OPENCODE_PROVIDER=litellm` is the recommended default because it avoids operating two unrelated provider configurations.

## Capability gating

Every engine reports capabilities. Controllers use them before starting a run and return a clear validation error when a requested flow is unsupported.

Initial capability groups:

| Capability | Internal | OpenCode initial release |
| --- | --- | --- |
| Headless segment execution | yes | yes |
| Workspace file operations | yes | yes, through the adapter |
| MCP tools | yes | compatibility-tested subset |
| Streaming assistant output | yes | yes |
| AG-UI frontend-tool interrupts | yes | no |
| Durable cross-replica resume | yes | limited to supported snapshot mode |
| Hooks and permission asks | yes | compatibility-tested subset |
| Subagents | yes | deferred |
| Context compaction parity | yes | deferred |

This prevents an environment switch from silently degrading workflow behavior.

## Public compatibility

The first implementation preserves all existing public request and response behavior when `ORCHESTRATOR_ENGINE=internal`.

No `jb-web-code` changes are required because:

- engine selection is server-side orchestrator configuration
- existing orchestrator and AG-UI endpoints remain in place
- unsupported OpenCode capabilities fail inside the orchestrator with explicit errors
- AG-UI event translation remains the orchestrator's responsibility

A future per-request or per-tenant engine override can be added to the orchestrator contract after the server-level switch is stable. It is not required for the first delivery.

## Implementation plan

### Phase 0 — Baseline and contract tests

- Add characterization tests around current segment completion, streaming, tool calls, AG-UI awaits, cancellation, errors, and resume.
- Record the current persisted-state fields that resume depends on.
- Define version 1 execution request, event, result, and await-snapshot schemas.
- Add architecture tests that keep engine-specific imports behind their adapters.

Exit criteria: the current behavior is protected before files are moved.

### Phase 1 — Extract the internal engine

- Introduce `ExecutionEngine` and `InternalExecutionEngine`.
- Move orchestration entry points to an engine resolver.
- Keep `ToolExecutionHub` behavior intact behind the adapter.
- Persist `engine=internal` and `engineContractVersion=1` with new run state.
- Teach resume to reject an engine mismatch.

Exit criteria: all existing tests pass and internal runs are explicitly identified.

### Phase 2 — Extract model access

- Define `ModelClient`.
- Move LiteLLM HTTP, timeout, streaming, and error behavior from `MCPAgentService` into `LiteLLMModelClient`.
- Keep MCP connection and tool-listing responsibilities separate from model completion.
- Preserve every current LiteLLM environment variable.

Exit criteria: internal runs use the new client with no API or provider changes.

### Phase 3 — Add OpenCode discovery

- Research and fixture the supported OpenCode project layout.
- Implement `OpenCodeAdapter` beside the existing harness adapters.
- Map its files into the existing loading policies and manifest structures.
- Add detection, explicit selection, budget, and malformed-file tests.

Exit criteria: OpenCode playbooks can drive the internal engine without an OpenCode runtime.

### Phase 4 — Add the OpenCode sidecar adapter

- Add configuration and health checking.
- Implement session start, prompt submission, event streaming, cancellation, and completion.
- Translate OpenCode tool activity into orchestrator events.
- Support the headless capability set first.
- Persist the OpenCode engine identity and any reattach data required by the supported resume mode.

Exit criteria: a headless OpenCode run completes through existing orchestrator APIs.

### Phase 5 — Establish parity gates

- Add contract tests for MCP tools, permissions, interrupts, resume, compaction, and subagents.
- Enable each OpenCode capability only after its tests pass.
- Document unsupported behavior and its caller-visible error.
- Add operational logging that identifies engine, contract version, model route, and capability denials.

Exit criteria: capability claims match tested behavior.

### Phase 6 — Production rollout

- Deploy OpenCode as an optional sidecar only in environments that select it.
- Run internal and OpenCode engines in parallel on representative workspaces.
- Compare output, duration, token/accounting data, failure rates, and resume behavior.
- Keep internal as the default until OpenCode meets the target capability set.
- Add rollback by returning `ORCHESTRATOR_ENGINE` to `internal`; persisted engine identity protects active runs.

## Testing strategy

- Unit tests cover adapter translation, capability negotiation, configuration validation, and engine mismatch handling.
- Contract tests run both engines against the same segment scenarios where capabilities overlap.
- Resume tests restart from persisted snapshots and verify replica independence.
- Failure tests cover sidecar downtime, timeouts, malformed events, partial streams, and cancellation.
- Security tests cover workspace containment, credential redaction, tenant isolation, and tool permission enforcement.
- Regression tests cover the current orchestrator, AG-UI, and legacy agent paths on the internal engine.

## Risks and controls

| Risk | Control |
| --- | --- |
| OpenCode session semantics differ from Postgres resume | Persist engine identity and require a versioned snapshot contract |
| AG-UI interrupts are embedded in the internal loop | Keep them internal-only until parity tests pass |
| Tool names and permissions differ | Translate through explicit capability and tool maps; reject unmapped required tools |
| Provider configuration drifts | Prefer one LiteLLM gateway for both engines |
| Sidecar upgrades change behavior | Pin and health-check the OpenCode version; run contract tests before rollout |
| Extraction changes internal behavior | Land extraction separately with characterization tests before adding OpenCode |

## Delivery slices

1. Documentation and contract decision — this document.
2. Internal engine extraction and persisted engine identity.
3. LiteLLM model-client extraction.
4. OpenCode playbook adapter.
5. Headless OpenCode execution adapter.
6. Capability-by-capability parity expansion.

Each slice is independently reviewable and leaves `ORCHESTRATOR_ENGINE=internal` as a safe fallback.

## Acceptance criteria

- Existing internal-engine behavior passes without changes to `jb-web-code`.
- `ORCHESTRATOR_ENGINE=internal` is the default.
- `ORCHESTRATOR_ENGINE=opencode` routes eligible new runs to OpenCode.
- Resume always uses the engine recorded for that run.
- Unsupported OpenCode capabilities fail explicitly.
- LiteLLM remains the internal model gateway and the recommended OpenCode provider route.
- OpenCode native providers are possible without changing orchestrator API code.
- Playbook discovery and execution-engine selection are independently tested.
- Engine contract versioning permits future engines without reinterpreting old state.
