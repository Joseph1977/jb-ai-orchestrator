# Documentation Index

This folder contains detailed documentation for the AI Agent / harness microservice.

**Note**: The main project README is located in the root directory at [../readme.md](../readme.md).

## Available documentation

### Implementation guides

- **[ORCHESTRATOR.md](./ORCHESTRATOR.md)** — Comprehensive folder-execution harness source of truth
  - `/v1/orchestrator/initiate|execute|resume|close` API + session lifecycle
  - `/api/ag-ui/threads/{threadId}/close` and non-destructive `/abandon`
  - Input/output bindings, Phase 2 durable output providers/tools, runtime_path, prompt templates
  - Phase 3: three-root separation, segment run-binding refresh, input recovery, multi-pod run lifecycle
  - Close endpoints (**200** closed / **202** closing), heartbeat config, cleanup scope
  - Phase 4 caller demo (`ag-ui-demo/`): optional bindings (workflow input required; plain chat none), close retry, protocol sandbox
  - Cursor / Claude Code / generic adapter detection and manifest collection timing
  - Eager `AGENTS.md`/`CLAUDE.md`/rules loading and progressive skill/agent/command catalogs
  - Authoritative system-prompt composition and typed history
  - Local + MCP + AG-UI registry construction and complete model/tool loop
  - Built-in local and durable-output tools, workflow mutator policy, subagents, git, and `ask_user`
  - Multimodal image/PDF reads via LiteLLM content parts
  - Project hook execution (`permission: ask` + session/prompt hooks)
  - Path/shell allow-deny policy + `WORKSPACE_ALLOWED_ROOTS`
  - Offload, summarization, compaction, streaming, and batch/pod-agnostic resume
  - Migration/backfill (`20250816_phase3_lifecycle`), code map, configuration keys, and roadmap
- **[MULTI_SERVER_IMPLEMENTATION.md](./MULTI_SERVER_IMPLEMENTATION.md)** — Multiple MCP servers with universal tool attribution
  - Named server configuration options and examples
  - Architecture changes and UX improvements with universal tool naming
  - Universal tool attribution algorithm (ALL tools get server names)
  - API response format changes with complete server attribution

## Main documentation

- **[readme.md](../readme.md)** — Project overview, API surface, env vars, AG-UI flow

## Quick navigation

### For developers

1. Start with [readme.md](../readme.md) for project overview and endpoints
2. Read [ORCHESTRATOR.md](./ORCHESTRATOR.md) for the complete harness and file lifecycle
3. Read [MULTI_SERVER_IMPLEMENTATION.md](./MULTI_SERVER_IMPLEMENTATION.md) for MCP multi-server details

### For DevOps / deployment

1. Config keys in [readme.md](../readme.md) §Configuration — core, lifecycle, workspaces and bindings, tools, context management, hooks and policy
2. Full orchestrator/shell/compaction knobs in [ORCHESTRATOR.md](./ORCHESTRATOR.md) §11
3. Example env: `src/.env/localhost/.env.example`
4. Binding + close caller example: [ag-ui-demo/README.md](../ag-ui-demo/README.md)
5. Standalone Docker: `docker-compose.yml` — `ALLOW_INPLACE_WORKSPACE=true`,
   `WORKSPACE_ALLOWED_ROOTS`, and `OUTPUT_BINDINGS_ENABLED=false` by default; web
   deployments that send non-null output opt in to `OUTPUT_BINDINGS_ENABLED=true`
   (see `src/.env/docker/.env`)
