# Documentation organization

## Layout

```
/
├── readme.md                          # Project overview, APIs, env vars
└── docs/
    ├── DOCUMENTATION_INDEX.md         # Navigation index
    ├── ORCHESTRATOR.md                # Folder harness / coding tools (source of truth)
    ├── MULTI_SERVER_IMPLEMENTATION.md # Multi-MCP servers
    └── DOCS_ORGANIZATION_SUMMARY.md   # This file
```

## What to update when

| Change | Update |
| --- | --- |
| Harness discovery, workspace containment, or hook behavior | `docs/ORCHESTRATOR.md` §3/§11 + caller-visible summary in `readme.md` |
| New local tool or shell/compaction behavior | `docs/ORCHESTRATOR.md` (§5–§6 tools, §8 context management) + `readme.md` §Configuration |
| New orchestrator env var | `docs/ORCHESTRATOR.md` §11, `readme.md` §Configuration, `src/.env/localhost/.env.example` |
| MCP multi-server behavior | `docs/MULTI_SERVER_IMPLEMENTATION.md` + `readme.md` MCP sections |
| New top-level doc | Add a link in `docs/DOCUMENTATION_INDEX.md` |
