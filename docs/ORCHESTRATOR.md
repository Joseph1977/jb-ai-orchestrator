# Orchestrator: Folder Execution

The **orchestrator** lets `jb-ai-orchestrator` execute an *AI playbook that lives in a
folder* — for example a repository containing a `.cursor/`, `.claude/`, or plain
`AGENTS.md` bundle. The service **owns all orchestration**: it provisions an
isolated workspace, chooses the right harness behavior for the folder's
orchestration type, exposes built-in coding tools (filesystem, search, shell,
git), drives the LLM tool-calling loop with context compaction, and persists
state so runs can pause for user input and resume on **any** instance.

This document is the source of truth for the harness lifecycle, orchestrator
design, HTTP API, configuration, and multi-tenant / multi-pod guarantees. It is
written for developers who need to understand not only how to call the service,
but exactly when instructions, primitives, tools, and persisted state are loaded.

---

## 1. Concepts

| Term | Meaning |
| --- | --- |
| **Folder / source** | What you want to run. One of: a **git URL** (cloned), a **shared-folder / archive URL** (`file://`, UNC, or `.zip`/`.tar.gz` over HTTP — copied/extracted), or a **local path** (copied, or used in place). |
| **Workspace** | The directory the harness operates on. It is normally an isolated sandbox at `WORKSPACES_ROOT/{orchestratorGuid}/workspace`, but an approved local path can be used in place. All local file tools are confined to the selected workspace. |
| **Orchestration type** | Which tool authored the playbook: `cursor`, `claude-code`, or `generic`. Either passed explicitly or auto-detected. |
| **Harness adapter** | Per-orchestration-type logic that decides what to inject eagerly (rules / `AGENTS.md` / `CLAUDE.md`) and what to index for **lazy** loading (skills, agents, commands). |
| **Primitive** | A cataloged capability (skill/agent/rule/command). Only its name/path/description is surfaced up front; its body is read on demand. A primitive's **loading policy** decides whether it is injected eagerly, catalogued lazily, or withheld from the model entirely. |
| **Local built-in tools** | In-process tools (filesystem, search, shell, git, `ask_user`) exposed to the LLM alongside MCP and AG-UI tools, scoped to the workspace. |
| **Context compaction** | When the prompt grows large, older turns can be summarized and older tool messages compacted. Individual oversized tool results can be offloaded under `.agent/offload/`. |
| **Orchestrator session** | A bound folder, tracked by an `Execution` row (`orchestratorGuid`). Execute/resume run against it. |

---

### Entry points and enabled harness features

All three execution APIs eventually use `ToolExecutionHub.process_request()`, but
they do not prepare the same context:

| Entry point | Workspace/harness | Local tools | MCP tools | Frontend tools | Durable await/resume |
| --- | --- | --- | --- | --- | --- |
| `/v1/orchestrator/initiate` + `/execute` | Provisions or binds a workspace; detects and re-collects its manifest on every `execute` | Yes | Yes | Optional per execute | Yes |
| `/api/ag-ui/run` | On a fresh run with `workspacePath`, binds the directory and collects its manifest; resume reuses persisted messages | Yes when bound | Yes | Per run | Yes, including batch interrupts |
| `/v1/agent/executeRequest` | No workspace and no harness discovery | No | Yes | Legacy globally bound tools only | Yes through `/resumeRun` |

### End-to-end harness lifecycle

```text
source folder
    │
    ▼
provision/bind workspace
    │
    ▼
detect adapter ──► collect manifest
                    ├─ eager context: root instructions + always-on rules
                    └─ lazy catalog: skill/agent/command/rule metadata + paths
    │
    ▼
compose one authoritative system prompt
    │
    ▼
build collision-safe Local + MCP + AG-UI tool registry
    │
    ▼
run model/tool loop
    ├─ model reads a catalog file only when needed
    ├─ hooks gate lifecycle/tool actions
    ├─ large context is summarized/compacted/offloaded
    └─ awaited input persists state and returns
             │
             ▼
       claim + resume on any instance
```

The following sections describe each stage in that order, followed by the API
reference and configuration.

---

## 2. Public session lifecycle and endpoints

All endpoints are under `/v1/orchestrator`.

```
initiate  ──►  execute  ──►  (awaitsResponse?) ──► resume ──► ... ──► close
   │              │                                   ▲
   │              └──────────── loop tool calls ──────┘
   └── provision workspace + detect type (no LLM)
```

### Workspace provisioning and ownership

| Source | Provisioning | Workspace ownership |
| --- | --- | --- |
| Local path + `inPlace: true` | Bind the existing path after `ALLOW_INPLACE_WORKSPACE` and `WORKSPACE_ALLOWED_ROOTS` checks | Caller-owned; service never deletes it |
| Local path + `inPlace: false` | Copy into `WORKSPACES_ROOT/{orchestratorGuid}/workspace` | Service sandbox |
| Git URL | Shallow clone (`--depth 1`, optional `--branch` + `--single-branch`) into the sandbox | Service sandbox |
| Archive/shared-folder URL | Safely download/copy and extract into the sandbox | Service sandbox |

Local tools receive only the selected workspace path. File operations resolve
through `resolve_within()` and path policy; shell commands use the workspace as
`cwd` but are not an OS sandbox. Provisioned workspace retention is described in
§12.

### `POST /v1/orchestrator/initiate`

Bind a folder to a session. **No LLM call.**

New-contract request (preferred):

```json
{
  "input": {
    "type": "git",
    "uri": "https://github.com/acme/playbook.git",
    "relativePath": ".",
    "materialization": "copy",
    "branch": "main"
  },
  "output": null,
  "mode": "workflow",
  "orchestrationType": "cursor",
  "systemContext": "You are the release bot.",
  "model": "gpt-4o",
  "maxToolCalls": 25
}
```

`folder` + `inPlace` remain accepted and map to a synthetic `input` binding.
`uri` is a location only — SAS, auth query parameters, and embedded userinfo
are rejected (`BINDING_URI_HAS_CREDENTIALS`). Credential fields such as
`accessToken` are not allowed on `input`/`output` (`BINDING_CREDENTIAL_NOT_ALLOWED`).
Tokens belong in the per-call `credentials` object on execute/resume/AG-UI and
are never persisted.

`relativePath` is applied **after** clone/copy, inside the materialized
workspace. It is never appended to a git/url remote. Absolute paths and `..`
are rejected (`INVALID_RELATIVE_PATH`).

Optional `branch` on **git input only** selects the ref for shallow clone
(`git clone --depth 1 --branch <branch> --single-branch`). When omitted, git
uses the remote default branch (backwards compatible). `branch` is rejected for
non-git inputs and for output bindings (`INVALID_BRANCH`). Values are trimmed,
limited to 255 characters, and must not contain leading `-`, whitespace, control
characters, `~`, `^`, `:`, `?`, `*`, `[`, `..`, `@{`, backslash, trailing `/`
or `.`, empty/hidden path components, the standalone `@` ref, or `.lock` path
components. The sanitized branch is persisted in
`config.input`; `execution.source` remains URI-only and credentials are never
stored.

**Phase 2 gate:** a non-null `output` is accepted only when
`OUTPUT_BINDINGS_ENABLED=true`; the default remains `false`, preserving
`OUTPUT_FEATURE_NOT_ENABLED`. Supported durable providers are `shared_folder`
and `azure_blob`. Azure requires a fresh `credentials.outputAccessToken` on
every execute/resume segment; credentials are never stored. The token is a SAS
credential and must remain valid for that entire segment. The binding `uri`
remains location-only and must not contain a SAS query string.

Session `mode` is `workflow` (default) or `working_copy`. Git/url inputs always
`copy`. `working_copy` + `in_place_read_only` is rejected. `workflow` is
observation-only for input even when `output` is null: local create/write/edit,
mutating git, and shell tools are hidden and execution-blocked. Use
`working_copy` when callers require writable input, or bind durable output.
This policy covers built-in local tools; arbitrary MCP write semantics remain
the responsibility of each MCP server.

When output is bound, the model receives `write_output_local`,
`read_output_local`, and `list_output_local`. Paths are logical and relative;
absolute paths, `..`, and `.agent/**` are rejected. `list_files_local` returns
separate `input` and `output` partitions. `read_file_local` never silently
redirects to output: it notes overlaps and points to `read_output_local`, with
durable output authoritative for workflow state.

The caller's output URI plus `relativePath` is authoritative. The backend applies
`relativePath` once when it builds the segment-scoped output backend; tool paths
are relative to that effective root and must not repeat the prefix. Every durable
write uses `write_output_local`—input-writing tools are never an alternate output
route. The storage engine can construct an output backend without input, but a
workspace-free AG-UI run does not create a local tool context and therefore does
not expose output tools; output-only is not currently a supported caller workflow.
Unbound workflow sessions cannot invent or claim a persistence location.

Shared-folder writes use a same-directory temporary file followed by
`os.replace`; concurrent writers are last-writer-wins. A process crash can
leave a `.output-*` temporary file for operator cleanup. Azure operations are
asynchronous, bounded, and list results may include `nextToken`.

Each session also gets a service-owned `runtime_path` under
`WORKSPACES_ROOT/{id}/runtime` (orchestrator) or
`WORKSPACES_ROOT/threads/{sha256(thread_id)}/runtime` (AG-UI). Logical
`.agent/**` (offload, todos) maps there and is excluded from input
discovery/listing. The same `runtimePath` is restored on AG-UI resume and
copied into `task_local` subagents.

Prompt templates live in `src/app/prompts/` (`{{placeholder}}`, strict
substitution, no URI/credential placeholders). Fresh executable runs receive a
binding block containing only mode, provider type, logical prefix, and tool
names. Bind-only AG-UI calls do not create a backend or inject this block.

Legacy request (still valid):

```json
{
  "folder": "https://github.com/acme/playbook.git",
  "orchestrationType": "cursor",
  "systemContext": "You are the release bot.",
  "model": "gpt-4o",
  "maxToolCalls": 25,
  "inPlace": false
}
```

Response:

```json
{
  "success": true,
  "orchestratorGuid": "…uuid…",
  "orchestrationType": "cursor",
  "detected": true,
  "confidence": 90,
  "workspacePath": "/…/workspace",
  "sourceKind": "git_url",
  "agents": [ { "name": "reviewer", "path": ".cursor/agents/reviewer.md", "description": "…", "kind": "agent" } ],
  "skills": [ … ],
  "rules":  [ … ],
  "commands": [ … ],
  "notes": [ "Cloned … (shallow) into workspace" ]
}
```

### `POST /v1/orchestrator/execute`

Run a prompt against a session.

```json
{
  "orchestratorGuid": "…uuid…",
  "prompt": "Review the open changes and summarize risks.",
  "model": "gpt-4o",                 // optional; overrides session default
  "maxToolCalls": 25,                // optional
  "tools": ["search_docs"],          // optional; restrict MCP/AG-UI tools
  "threadId": "t-1",                 // optional; enables an AG-UI channel
  "runId": "r-1",                    // optional
  "frontendTools": [ … ]             // optional; AG-UI tool schemas for this run
}
```

`tools` filters only MCP and AG-UI tools. When a workspace is bound, all enabled
local `*_local` tools are added independently and cannot currently be restricted
per request.

Returns either a completed result, or when input is required:

```json
{
  "success": true,
  "awaitsResponse": true,
  "executionGuid": "…",
  "executionStatus": "awaiting_response",
  "stateGuid": "…",
  "pendingToolCallIds": ["call_abc"],
  "interrupts": [{
    "id": "call_abc",
    "toolCallId": "call_abc",
    "reason": "tool_awaiting_response",
    "message": "Awaiting response for tool 'Ask-Choice'",
    "metadata": {
      "source": "AGUI",
      "functionName": "Ask-Choice",
      "arguments": { "question": "Choose a path", "options": ["Short", "Detailed"] }
    }
  }]
}
```

Failed execute/resume results may include a stable `errorCode`. Provider
failures use `QUOTA`, `RATE_LIMIT`, `AUTH`, or `UNAVAILABLE`; raw provider
response bodies are never returned.

Root instruction failures carry two distinct codes, because they send an
operator to different fixes. `ROOT_INSTRUCTIONS_TOO_LARGE` means the file loaded
but exceeds the eager budget — split it, or raise
`HARNESS_EAGER_BUDGET_CHARS`. `ROOT_INSTRUCTIONS_UNREADABLE` means it could not
be read at all: permissions, I/O, or a path the workspace opener refuses such as
a symlink or a non-regular file. Either way execute fails rather than running
against stale eager context, and `initiate` reports the same code for the same
condition, so a caller sees one error whichever call first reaches it.

### `POST /v1/orchestrator/resume`

Continue a run that awaited input. Works from **any** instance.

```json
{
  "orchestratorGuid": "…uuid…",
  "stateGuid": "…uuid…",
  "toolCallId": "call_abc",
  "result": { "answer": "yes, proceed" },
  "error": null
}
```

### `GET /v1/orchestrator/{orchestratorGuid}`

Returns `{ status, result, error, awaitsResponse, stateGuid }`. While the
execution is `awaiting_response`, it also returns `interrupts` and
`pendingToolCallIds`, rebuilt from the latest persisted `LLMState` with the same
canonical serializer used by execute and resume. This lets a stateless caller
reconstruct Ask-* and hook-permission interactions after a remount. Terminal
statuses never replay stale interrupts. The payload contains interaction
metadata, not transient credentials.

### `POST /v1/orchestrator/{orchestratorGuid}/close`

Close an orchestrator session lifecycle. Returns **200** when fully closed or
**202** when close is still in progress (active run not yet terminal or heartbeat
still fresh). Idempotent: retries reconcile to **200** once blocking runs finish
or go stale.

```json
{
  "status": "closed",
  "alreadyClosed": false,
  "discardedHolds": 1,
  "runtimeDeleted": true,
  "workspaceDeleted": true,
  "workspaceDeletedCount": 1
}
```

Orchestrator close owns its service sandbox: it removes orchestrator runtime
(`.agent/**`), deletes the provisioned workspace under
`WORKSPACES_ROOT/{orchestratorGuid}`, and terminalizes non-completed executions
as `failed` with `session_closed`. Completed execution status is preserved.
Active runs are marked `closing`, holds are discarded, and pod-local tasks are
cancelled when possible; the database is the source of truth across pods.

See §12 for thread close, cleanup scope, and retry semantics.

---

## 3. Harness discovery and file lifecycle

`collect_manifest()` turns a workspace into two kinds of context:

1. **Eager context** — authoritative root instructions and always-on rules whose
   bodies are inserted into the initial system prompt.
2. **Lazy catalog** — names, relative paths, and short descriptions for skills,
   agents, commands, and additional rules. Their bodies are not read yet.

### Adapter selection

If `orchestrationType` is explicit, that adapter is used and the manifest reports
`detected: false`. Otherwise adapters score the workspace. The highest score wins;
equal scores keep the first registered adapter in stable order:
`cursor`, `claude-code`, then `generic`.

| Adapter | Detection signal | Eager context, in order | Adapter-specific catalog |
| --- | --- | --- | --- |
| `cursor` | `.cursor/`, `.cursor/rules/`, `.cursorrules` | Root `AGENTS.md`, otherwise `.cursor/AGENTS.md`; then `.cursor/rules/*.mdc` carrying `alwaysApply: true`, then `.cursorrules` | `.cursor/skills/*`, `.cursor/agents/*`, `.cursor/commands/*`; rules that are not always-on |
| `claude-code` | `.claude/`, `CLAUDE.md` | `CLAUDE.md`, otherwise `.claude/CLAUDE.md`; then root `AGENTS.md`; then `.claude/rules/**` without `paths` | `.claude/skills/*`, `.claude/agents/*`, `.claude/commands/*`, path-scoped rules |
| `generic` | Root or dotted-directory `AGENTS.md`, otherwise fallback | Root `AGENTS.md`, otherwise the first `.*/AGENTS.md`; then README context | Generic `skills/**`, `agents/**`, `commands/**`, and `rules/**` |

After adapter-specific collection, the common scanner deduplicates paths and
recognizes all of these conventions:

```text
.agents/skills/**/SKILL.md   .cursor/skills/**/SKILL.md
.claude/skills/**/SKILL.md   .codex/skills/**/SKILL.md
.cursor/commands/*.md        .claude/commands/*.md
.cursor/agents/*.md          .claude/agents/*.md
.cursor/rules/**/*.mdc       .claude/rules/**/*.md
skills/**/*.md               agents/**/*.md
commands/**/*.md             rules/**/*.md
nested AGENTS.md             nested CLAUDE.md
```

A `<root>/skills` directory is recognized **anywhere** in the tree, so category
folders (`.cursor/skills/shipping/land-it/SKILL.md`) and monorepo packages
(`apps/web/.cursor/skills/deploy-web/SKILL.md`) are both discovered.

**Ordering contract.** Discovery visits files in **lexicographic order by path
component**, comparing `a/c.md` before `a.md` because the component `a` sorts
before `a.md`. This is the order Python's `sorted()` produces over the matching
paths; it is stable across platforms and is what the per-kind ceiling keeps when
it truncates.

The walk is depth-first over each directory's name-sorted entries, descending as
each directory is met, which yields that order while holding only the entries
along the current path — O(depth × directory width), independent of how many
files the tree contains. An explicit stack rather than recursion, so a tree
deeper than the interpreter's stack limit does not abort discovery.
`node_modules`, `.git`, `.venv`, `dist`, `build` and similar are pruned *before*
descent. A directory or entry that cannot be read is logged and skipped rather
than failing discovery. A missing optional path (`ENOENT`) is normal and logs
only at debug; every other filesystem error remains a warning.

**Workspace containment.** A workspace is untrusted input, and the walker is not
the boundary: adapters enumerate their own locations, root instructions are
read without walking anything, and hooks are reloaded when tools execute.
Workspace file access therefore goes through one provider-neutral,
workspace-bound opener.

Paths are held as validated components relative to the workspace — absolute
paths, `..`, empty components and embedded separators are rejected before any
syscall. The opener then walks those components one at a time relative to a
directory descriptor anchored on the workspace, opening each with `O_NOFOLLOW`.
The open *is* the check, so there is no resolve-then-reopen window to race, and
because `O_NOFOLLOW` constrains only the final component of the path it is
given, feeding components in one at a time is what extends the guarantee to
symlinked *parents* as well as symlinked leaves. A symlinked discovery root is
covered by the same rule.

Consequences worth knowing:

- **Symlinks are refused, not resolved.** A link is skipped even when its target
  is inside the workspace. Resolving and comparing is the raceable pattern this
  design removes. Each normalized path warns once per collection even when
  several discovery passes encounter it, and the manifest receives one
  count-only note when any symlinks were encountered and skipped. Failures
  beneath an already-identified symlink log at debug rather than repeating the
  same warning.
- **Only regular files are read.** The leaf is `fstat`-checked, and opened with
  `O_NONBLOCK`, so a FIFO planted in a workspace cannot block discovery before
  it is rejected.
- **One descriptor at a time.** The opener re-anchors per operation rather than
  holding a descriptor per level, so correctness does not depend on the process
  descriptor limit. The cost is O(depth) extra opens.
- **Rejected means absent.** A file that cannot be read safely is skipped and
  logged, never catalogued from its filename — an entry the model cannot read is
  worse than no entry, because it will try. A *readable* file with no
  frontmatter still gets its filename/prose fallback.
- **Fail closed.** Where the platform lacks `os.open(dir_fd=)` or
  `os.scandir(fd)`, the reader refuses to construct rather than falling back to
  path-based opens.
- **Entries are classified while their directory is open.** The reader returns
  immutable name/type facts, not `DirEntry` objects tied to a descriptor it has
  already closed. This keeps traversal correct on NFS and other filesystems that
  report `DT_UNKNOWN` and need a live descriptor for `is_file` / `is_dir`.

`DiscoveryContext` owns the reader lifecycle. Adapters enter it with `with`; its
exit path transfers the unique symlink count to manifest notes and closes the
reader on both success and failure. New adapters therefore cannot accidentally
omit diagnostics or leak the workspace descriptor by forgetting manual cleanup.

**Discovery ceiling.** At most 200 primitives per kind are kept as a safety
ceiling. Every catalog entry, from shared discovery and adapter enumeration
alike, goes through one candidate transaction that deduplicates, checks the
ceiling, then reads — in that order. So a path already catalogued is never
mistaken for overflow, and the 201st candidate is refused on count alone without
being opened. Once a kind has confirmed overflow its remaining roots are not
walked at all, and when every kind is capped discovery stops entirely. The
omission is noted on the manifest and logged; a workspace holding exactly 200 of
a kind reports no omission.

### Loading policy

Adapters translate their own conventions into one vendor-neutral policy, so the
renderer holds no harness-specific knowledge:

| Policy | Eager | In catalog | Cursor source | Claude source |
| --- | --- | --- | --- | --- |
| `EAGER` | yes | no | `alwaysApply: true`, `.cursorrules` | `.claude/rules/**` without `paths` |
| `SCOPED` | no | yes, with its paths | `globs` | `paths` |
| `MODEL_DISCOVERABLE` | no | yes | `description`, no globs | default |
| `EXPLICIT_ONLY` | no | no | none of the three | `disable-model-invocation` |

`EXPLICIT_ONLY` entries are withheld from the catalog rather than labelled,
since a label does not stop the model selecting them. **Commands are exempt**:
they are user-invoked by nature and the model must know they exist to answer
`/help`. Eager entries stay in the API summary but are not listed in the
catalog, since their bodies are already in the prompt.

Plain `.md` under `.cursor/rules/` is ignored, matching Cursor: without
frontmatter there is no activation to honour.

### Eager loading rules

- Root `AGENTS.md` and `CLAUDE.md` are one **mandatory allocation** of the eager
  budget. If their combined size exceeds it, collection fails explicitly. They
  are never partially included.
- The eager budget defaults to 24,000 characters and is configurable through
  `HARNESS_EAGER_BUDGET_CHARS`. An unusable value is logged and ignored rather
  than stopping the service booting.
- Optional eager rules take what remains and are included **whole or not at
  all**; omissions are noted on the manifest and logged. A half-injected rule is
  worse than an absent one, because the model cannot tell the rest is missing.
- Other eager files are capped at 6,000 characters each. Content a clip does not
  invalidate, such as a README, is truncated with a marker; rule bodies never
  are.
- Every eager and root-instruction read is bounded **at the file handle**, at
  most `cap + 1` characters. Reading a file in full and slicing afterwards is
  not a cap: the file is already resident by the time the check runs.
- Section order is meaningful and follows the adapter table above.
- Hook files and Claude settings may be noted during discovery. Their reads use
  the same workspace-bound opener, are capped at 64,000 characters, and reject
  unsafe, malformed, excessively nested, or oversized inputs whole. Enabled hooks are
  executed later at their matching lifecycle events; discovery itself does not
  run them.

### Catalog construction and progressive disclosure

Metadata is read **once per file**, bounded at the file handle. Two independent
limits apply: frontmatter is parsed against an 8,192-character window, and the
prose fallback is taken from the first 4,096 characters after the frontmatter
block. Both are separate from the 6,000-character eager content read.

For each primitive, the manifest stores only:

- `name` — frontmatter `name`, the parent directory for `SKILL.md`, or filename;
- `path` — normalized relative path with forward slashes, used for deduplication;
- `description` — frontmatter `description`, otherwise the first heading or
  substantive line. Frontmatter is parsed with `yaml.safe_load`, so block
  scalars (`>`, `|`, and their chomping variants) resolve to their text;
- `kind` — `skill`, `agent`, `command`, or `rule`.

Only whitelisted keys are retained, normalized to scalars or immutable string
tuples: `name`, `description`, `alwaysApply`, `disable-model-invocation`,
`globs`, `paths`. Scope fields are validated by shape without recursion, because
YAML aliases load cheaply but expand astronomically under any structural walk —
289 characters of frontmatter reaches roughly 43 million nodes.

A file that cannot be parsed degrades its own catalog entry and never aborts
discovery. This covers `yaml.YAMLError` and `RecursionError` alike; the latter
is a `RuntimeError` and deeply nested sequences reach it at around 1,000
characters. Frontmatter whose closing delimiter falls outside the window, or an
unterminated leading HTML comment, yields no description rather than leaking
`---` or comment text.

The rendered catalog is bounded by a total character budget — 12,000 for the
main prompt, 4,000 for subagents — shared across kinds by max-min fair
allocation. Anything dropped is declared in the catalog itself, noted on the
manifest, and logged.

The rendered catalog is headed **Available capabilities (NOT loaded yet)** and
explicitly tells the model to read a referenced file before applying it. Lazy
loading is therefore model-driven:

```text
catalog says: release-check (.cursor/skills/release-check/SKILL.md)
    │
    ├─ irrelevant to the task ──► never loaded
    │
    └─ selected by the model ───► read_file_local(path)
                                   └─ full instructions enter history as a tool result
```

There is no hidden server-side skill loader and no automatic slash-command
expansion. `enrich_system_prompt()` intentionally leaves the prompt unchanged;
skills, commands, rules, and agent files enter context only when read with a
filesystem tool. Supporting files referenced by a skill are loaded the same way.

### When manifests are collected

| Flow | Collection timing |
| --- | --- |
| Orchestrator `initiate` | Collect once to validate the playbook and return discovered primitives. No model call. |
| Orchestrator `execute` | One normal harness scan per segment start; workspace instruction/catalog changes are visible. |
| Orchestrator `resume` | Do not re-scan; patch the persisted system message's tagged run-binding block only. |
| Fresh AG-UI run with `workspacePath` | Collect once while building the fresh run's system prompt. Discovery failure is best-effort and the run can proceed. |
| AG-UI resume | Do not re-scan; patch the persisted system message's tagged run-binding block only. |
| `task_local` subagent | Re-collect for the shared workspace; include a catalog capped at 4,000 characters. |
| `/v1/agent/executeRequest` | Never collect; this path has no harness workspace. |

An AG-UI bind-only request with `workspacePath` still performs discovery before
the no-message short-circuit.

Adapters live in `src/app/services/harness/`. To add one, implement `type_id`,
`detect()`, and `collect()`, then register it in `registry.py`.

---

## 4. System prompt lifecycle

`render_system_prompt()` produces the basic harness prompt in this order:

1. caller-provided base context, when present;
2. eager orchestration context from the manifest;
3. the lazy primitive catalog.

The orchestrator path passes `systemContext` as the base. The AG-UI path then
builds one authoritative system message in this order:

1. rendered harness prompt;
2. AG-UI `context[]` values;
3. frontend interaction guidance and the deduplicated frontend tool names for
   this run;
4. incoming client system messages inside `MANDATORY UI CONTRACT` markers.

Incoming non-system messages keep their native user/assistant/tool roles and
structured tool-call IDs. Client system messages are folded into the single
authoritative system message instead of being duplicated. A fresh AG-UI run
without `workspacePath` has no harness section, but can still receive client
context and frontend guidance.

`execute` composes the same interaction section from `frontendTools`: the
rendered harness prompt (with its run-binding block already resolved), then the
frontend interaction guidance and the deduplicated tool names. Both channels
therefore state the same contract. Passing the tool schemas alone left the
model free to answer a structured question in prose, which broke workflow steps
that require a matching interaction tool. The catalog raises adherence; it does
not force a tool call, so a caller that depends on one still has to handle
plain text.

Resumed runs do not rebuild this prompt. The persisted message list is the
authoritative snapshot for the interrupted run, which prevents instruction or
catalog changes midway through a pause/resume cycle.

---

## 5. Tool registry and execution loop

### Registry composition

Before the first model call, `build_tool_registry()` combines:

1. local tools allowed for this workspace and subagent depth;
2. MCP tools discovered from every configured server;
3. AG-UI tools provided for this run.

The registry keeps canonical model-visible names and collision-safe internal
aliases such as `{base}__{provider}__{server}`. Dispatch resolves the alias back
to its provider and original name. On resume, the registry is rebuilt from
persisted LiteLLM schemas and AG-UI records; a different service instance does
not need the original process memory.

Special filtering rules:

- nested subagents cannot see `task_local` or `ask_user_local`;
- when an interactive frontend tool can collect user input, the headless
  `ask_user_local` fallback is suppressed;
- conflicting MCP tools can be removed in favor of local built-ins;
- `task_local` is managed by the hub because it launches a nested loop.

### `ToolExecutionHub.process_request()`

All execution paths converge on the same loop:

```text
fresh run
  ├─ sessionStart hook
  ├─ beforeSubmitPrompt hook
  └─ build messages + registry
        │
        ▼
while segment tool-call budget remains
  ├─ preCompact hook → optional summarization → tool-message compaction
  ├─ attach any pending multimodal file content
  ├─ call LiteLLM (stream when AG-UI streaming is enabled)
  ├─ append the structured assistant message
  ├─ no tool calls? ──► sessionEnd → completed
  └─ for each tool call in the model's batch
       ├─ resolve registry entry
       ├─ execute Local, AG-UI, or MCP provider
       ├─ run local-tool hooks/policy checks when the provider is Local
       ├─ serialize/offload the result and append a role=tool message
       └─ response required? ──► persist full state → return interrupt
```

One model turn may contain multiple tool calls. Completed calls are retained
before the loop returns for any calls that require input, and all unresolved
calls are saved in `pending_tools`. `segment_tool_call_count` enforces the
current request budget; `total_tool_call_count` survives resumes for reporting.

`sessionStart` confirmation requests cannot pause before a run exists and are
treated as a blocked start. Tool-level `permission: ask` creates a generic
`hook_permission` interrupt; approval executes the deferred tool during resume.
`sessionEnd` runs on completion, failure, or budget exhaustion.

---

## 6. Tool reference

For an orchestrator run the LLM sees three tool sources through one unified list:

1. **Local built-in tools** (namespaced with `LOCAL_TOOLS_NAMESPACE`, default
   `local`, producing names such as `read_file_local`) — see table below.
2. **MCP tools** from all configured remote MCP servers (unchanged).
3. **AG-UI tools** — only when `frontendTools` is provided for the run.

The request-level `tools` list filters MCP and AG-UI sources only. It does not
remove local built-ins from a workspace-backed run. Lifecycle hooks around
individual tool execution currently apply to local tools; MCP and AG-UI tools do
not pass through the same local hook wrapper.

### Local tool reference

Prefixed names below assume the default namespace `local` (e.g. `read_file` → `read_file_local`).

| Base name | Prefixed name | Purpose |
| --- | --- | --- |
| `list_files` | `list_files_local` | List files/folders (optional recursive). |
| `read_file` | `read_file_local` | Read text file contents (lazy-load skills/rules). |
| `write_file` | `write_file_local` | Create or overwrite a file. |
| `edit_file` | `edit_file_local` | Exact string replacement (`old_string` → `new_string`; use `replace_all` for multi-match). |
| `create_file` | `create_file_local` | Create a file; fails if it exists unless `overwrite=true`. |
| `create_folder` | `create_folder_local` | `mkdir -p` under the workspace. |
| `glob` | `glob_local` | Find paths matching a glob (e.g. `**/*.py`). |
| `grep` | `grep_local` | Regex search over file contents (optional `glob` filter, context lines). |
| `execute` | `execute_local` | Run a shell command with `cwd` = workspace (see shell notes). |
| `write_todos` | `write_todos_local` | Replace structured todo list (persists `.agent/todos.json`). |
| `task` | `task_local` | Spawn an isolated subagent; returns a summary (see §7). |
| `git_status` | `git_status_local` | `git status --porcelain`. |
| `git_diff` | `git_diff_local` | `git diff` (optional staged / path). |
| `git_log` | `git_log_local` | Recent oneline history. |
| `git_add` | `git_add_local` | Stage paths. |
| `git_commit` | `git_commit_local` | Commit with message. |
| `git_checkout_branch` | `git_checkout_branch_local` | Create or switch branch. |
| `ask_user` | `ask_user_local` | Pause for human input (DB-backed await). |

File tools resolve paths through `workspace_manager.resolve_within` so `..` /
absolute escapes are rejected. `execute` is **not** a full OS jail: it only sets
`cwd` to the workspace (see below).

### Shell tool (`execute_local`)

- Enabled when `LOCAL_SHELL_ENABLED=true` (default). When disabled, the tool is
  omitted from the LiteLLM tool list entirely.
- Runs via the system shell with `cwd` = the session workspace.
- Captures stdout/stderr up to `LOCAL_SHELL_MAX_OUTPUT_BYTES`.
- Times out after `timeoutSec` (per call) or `LOCAL_SHELL_TIMEOUT_SEC` (default).
- **Security model:** trust boundaries are “workspace cwd + timeout + output
  cap,” not a container/jail. Prefer workspace-relative paths; do not treat this
  as multi-tenant hard isolation for untrusted prompts.

### Conflict handling / local-first

- Local tools are explicitly suffixed (e.g. `read_file_local`) so they can never
  be confused with a remote MCP tool of the same base name.
- Dispatch order is **local → AG-UI → MCP**, so local always wins.
- When `FILTER_MCP_TOOLS_CONFLICTING_WITH_LOCAL=true` (default), any MCP tool
  whose base name matches a local built-in (e.g. a remote `read_file`) is dropped
  from the tool list so the model only sees the local version.

### `ask_user` and pausing

`ask_user` is a transport-agnostic "pause for input" primitive. It never executes
inline; instead the loop persists an `LLMState` row (`awaiting_response`) and
returns `awaitsResponse`. This works whether or not a live UI is attached:

- **No UI:** the caller answers via `POST /v1/orchestrator/resume`.
- **AG-UI attached** (`threadId` present): a tool-call event is also emitted so the
  UI can render the prompt; the answer arrives via `/api/ag-ui/run` with canonical
  `resume[]`. Legacy `state.toolResponse` remains compatibility-only. Both paths
  settle the same persisted state.

---

## 7. Subagents (`task_local`)

`task_local` is hub-managed (not an in-process file op). It starts a **nested**
`ToolExecutionHub.process_request` with:

- Fresh message history (only the subagent prompt + system role text)
- Same workspace `local_context`, with `subagent_depth + 1`
- A newly collected workspace manifest and progressive-disclosure catalog capped
  at 4,000 characters
- No AG-UI frontend tools (subagents cannot pause for HITL)
- Local tools excluding `task` and `ask_user` (no re-delegation / no pause)
- Optional agent role file via `agent` (`reviewer`, `.cursor/agents/reviewer.md`,
  `.claude/agents/….md`, or `agents/….md`); the selected role body is loaded into
  the subagent system context
- Cap of `SUBAGENT_MAX_TOOL_CALLS` and max nesting `SUBAGENT_MAX_DEPTH`

The parent only receives a JSON summary (`success`, `summary`, `tool_calls_made`,
…). Because it is a fresh nested loop, session and prompt hooks run again.
Disable with `SUBAGENT_ENABLED=false`.

---

## 8. Context management

Long coding runs produce large tool payloads (grep dumps, test logs, file reads).
`src/app/services/context_compaction.py` manages them at two points:

1. **Immediately after each tool execution: offload oversized results.** If a
   result exceeds `TOOL_RESULT_OFFLOAD_CHARS`, its full JSON is written to
   logical `.agent/offload/{tool_call_id}.txt` (physical file under the
   service-owned `runtime_path`) and the message history keeps a short pointer
   (`offloaded: true`, `path`, `preview`). That pointer **is** the `role=tool`
   result sent on the next LiteLLM call (full `messages` history). The model can
   call `read_file_local` on that path if it needs more detail. Offload never
   uses an output provider. Reads of an offload path
   are marked `alreadyOffloaded` so dereferencing a pointer cannot recursively create
   another pointer.
2. **Before each LiteLLM call: manage the whole prompt.**
   - Run the `preCompact` hook with the message count and estimated size. A deny
     result skips context management for that pass.
   - When the configured character/token budget is exceeded and
     `CONTEXT_SUMMARIZATION_ENABLED=true`, summarize older user/assistant turns.
     The summary is a system message marked `[context_summary]`; the most recent
     `CONTEXT_COMPACTION_KEEP_RECENT_TURNS` remain verbatim, and tool messages
     from the summarized region remain in history.
   - If the prompt remains over budget, shrink/offload older `role=tool` messages
     while preserving the most recent
     `CONTEXT_COMPACTION_KEEP_RECENT_TOOL_MSGS`.

Without a workspace (`local_context` absent), oversized results are truncated
in-place instead of written to disk.

`CONTEXT_COMPACTION_TOKENS` takes precedence when greater than zero; the default is
currently character-based for backward compatibility. `.agent/offload/` is ignored
runtime state, not workflow source, and should be cleaned only when no resumable state
still references it.

## 9. Token streaming

When `LLM_STREAMING_ENABLED=true` (default) **and** the run has an AG-UI channel
(`threadId` / `agui_context`), LiteLLM is called with `stream=true`. Content
deltas are published as `TEXT_MESSAGE_START` / `CONTENT` / `END` during the loop.
The hub sets `text_streamed: true` on the result so `/api/ag-ui/run` does not
re-emit the final assistant text as a second one-shot message.

Orchestrator HTTP calls without AG-UI context still use blocking completions.

---

## 10. Pause, resume, and deployment guarantees

This service is designed to be run as a horizontally-scaled, stateless
microservice — including one deployment shared by several applications.

### What is persisted on await

When any tool needs a response, the process does not wait in memory. It stores an
`LLMState.state_payload` containing the message history, LiteLLM tool schemas,
AG-UI tool records and forwarded calls, local/workspace settings, tool counters,
and all `pending_tools`. Fresh workspace-backed AG-UI runs also include the
harness manifest summary for interrupt/snapshot metadata; orchestrator awaits
currently store this field as `null`. The service then returns an
interrupt/`awaitsResponse` result and closes the request.

Resume atomically claims the state (`awaiting_response` → `pending`), applies the
matching response, and either:

- completes the claim after continuing successfully;
- persists another awaiting state if more input is needed; or
- restores/rolls back the claim when continuation fails.

Claims left `pending` beyond `RESUME_CLAIM_TIMEOUT_SEC` are restored so another
instance can retry.

### Resume contracts

- **AG-UI:** canonical `resume[]` can resolve or cancel several interrupts in one
  request. Unresolved entries remain in `pending_tools`.
- **Orchestrator:** `/v1/orchestrator/resume` accepts one `toolCallId` and result
  per request.
- **Direct agent API:** `/v1/agent/resumeRun` uses
  `executionGuid`/`stateGuid`/`toolCallId`.
- **Hook permission:** the response targets the interrupt's permission ID; an
  approval executes the deferred tool exactly once.
- **Thread reset:** a fresh executable AG-UI run discards stale awaiting holds
  and cleans only unreferenced offload files; todos and other runtime state are
  preserved. Resume preserves runtime; thread close removes runtime.
  `POST /api/ag-ui/threads/{threadId}/abandon` explicitly discards active
  awaiting/pending holds only (non-destructive hold discard for current web
  compatibility). `POST /api/ag-ui/threads/{threadId}/close` performs full
  thread lifecycle close (see §12).

### Deployment guarantees

- **Per-run tool isolation.** AG-UI tool definitions are taken from the
  `frontendTools` passed to each request, not a global cache. Two users (or two
  apps) running concurrently cannot see or clobber each other's tools.
- **Workspace isolation.** Provisioned sessions get a sandbox under
  `WORKSPACES_ROOT/{orchestratorGuid}`; approved in-place runs bind an existing
  path instead. File tools are path-guarded to the selected workspace (traversal
  via `..`/absolute paths is rejected). Shell commands use that directory as
  `cwd` but are not a full OS jail (see §6).
- **Pod-agnostic pause/resume.** There are **no in-memory waiters**. Any await
  (AG-UI or `ask_user`) persists to `LLMState`. AG-UI locates a resume hold by
  tool/interrupt ID; orchestrator and direct-agent clients load the supplied
  `stateGuid`. Each path then atomically claims the same database state, so a
  different pod can continue without sticky sessions.
- **Backward compatible.** The existing caller → `/api/ag-ui/run` contract
  remains the chat path; that path uses the same DB-backed await/resume model.
- **Web session workspace bridge.** `POST /api/ag-ui/run` accepts optional
  `workspacePath` + `inPlace` (legacy) and new-contract `input`, `output`,
  `mode`, and `credentials`. `workspacePath` remains a compatibility shim.
  Non-null `output` follows the Phase 2 feature gate. New-contract AG-UI
  `input` supports `shared_folder` copy and `workflow + in_place_read_only`;
  `working_copy + in_place_read_only` remains invalid. `git` / `url` return
  `AGUI_INPUT_NOT_SUPPORTED`.
  Legacy `workspacePath` / `inPlace` is unchanged. On await, sanitized bindings
  and `runtimePath` are persisted (never tokens). Logical `.agent/**` maps to
  the service-owned runtime directory, not the caller folder. AG-UI resume
  restores that `runtimePath` (or recreates it from the stable thread digest).

---

## 11. Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `WORKSPACES_ROOT` | `<tmp>/jb-agent-workspaces` | Root for per-execution sandboxes and `runtime/`. |
| `OUTPUT_BINDINGS_ENABLED` | `false` | Operator gate for Phase 2 durable output bindings/tools. |
| `OUTPUT_READ_MAX_BYTES` | `5242880` | Maximum UTF-8 output read size. |
| `OUTPUT_WRITE_MAX_BYTES` | `5242880` | Maximum UTF-8 output write size. |
| `OUTPUT_LIST_MAX_ENTRIES` | `1000` | Per-page/list cap for durable output. |
| `AZURE_BLOB_TIMEOUT_SEC` | `60` | Timeout for each Azure Blob operation. |
| `PROMPTS_DIR` | `src/app/prompts` | YAML instruction templates. |
| `ALLOW_INPLACE_WORKSPACE` | `false` | Allow `inPlace=true` to operate on a local path without copying. |
| `GIT_CLONE_TIMEOUT_SEC` | `120` | Timeout for `git clone` and archive downloads. |
| `MAX_WORKSPACE_MB` | `0` (off) | Soft cap on provisioned workspace size. |
| `LOCAL_TOOLS_ENABLED` | `true` | Expose the built-in local tools. |
| `LOCAL_TOOLS_NAMESPACE` | `local` | Suffix marking local tools (`read_file_local`). |
| `FILTER_MCP_TOOLS_CONFLICTING_WITH_LOCAL` | `true` | Drop remote MCP tools whose base name collides with a local built-in. |
| `LOCAL_SHELL_ENABLED` | `true` | Expose `execute_local`; when `false`, tool is hidden from the model. |
| `LOCAL_SHELL_TIMEOUT_SEC` | `60` | Default shell timeout (seconds). |
| `LOCAL_SHELL_MAX_OUTPUT_BYTES` | `100000` | Cap on captured stdout/stderr per stream. |
| `CONTEXT_COMPACTION_ENABLED` | `true` | Enable offload + history compaction. |
| `TOOL_RESULT_OFFLOAD_CHARS` | `8000` | Offload a tool result when its JSON exceeds this many chars. |
| `CONTEXT_COMPACTION_CHARS` | `120000` | Compact older tool messages when total history exceeds this. |
| `CONTEXT_COMPACTION_TOKENS` | `0` (off) | When greater than zero, use a token budget instead of the character budget. |
| `CONTEXT_COMPACTION_KEEP_RECENT_TOOL_MSGS` | `8` | Recent tool messages left untouched during compaction. |
| `CONTEXT_COMPACTION_KEEP_RECENT_TURNS` | `4` | Recent user/assistant turns retained verbatim during summarization. |
| `CONTEXT_SUMMARIZATION_ENABLED` | `true` | Summarize older user/assistant turns before tool-message compaction. |
| `CONTEXT_SUMMARIZATION_MODEL` | Run model | Optional model override for the summarization pass. |
| `SUBAGENT_ENABLED` | `true` | Expose `task_local` nested agent tool. |
| `SUBAGENT_MAX_TOOL_CALLS` | `8` | Max tool calls inside one subagent run. |
| `SUBAGENT_MAX_DEPTH` | `2` | Max nesting depth for `task_local`. |
| `LLM_STREAMING_ENABLED` | `true` | Stream tokens to AG-UI when a UI channel is present. |
| `LITELLM_REQUEST_TIMEOUT_IN_SEC` | `300` | HTTP idle/network guard for each LiteLLM request. It is not the total stream lifetime. |
| `LITELLM_MODEL_DEADLINE_SEC` | `240` | Absolute deadline for one LiteLLM call, including full SSE consumption. |
| `LITELLM_MAX_COMPLETION_TOKENS` | `4096` | Completion-token cap sent to LiteLLM; `0` disables it. A `length` finish reason fails with `OUTPUT_LIMIT`. |
| `HOOKS_ENABLED` | `true` | Run project hooks from `.cursor/hooks.json` / Claude hook files. |
| `HOOKS_FAIL_CLOSED` | `false` | If a hook script errors/times out, block the action when `true`. |
| `HOOKS_TIMEOUT_SEC` | `30` | Default per-hook subprocess timeout. |
| `HOOKS_MAX_OUTPUT_BYTES` | `100000` | Cap on hook stdout/stderr. |
| `PATH_POLICY_ENABLED` | `true` | Enforce path/shell allow-deny lists (empty lists = no extra filters). |
| `PATH_DENYLIST` | _(empty)_ | Comma-separated globs denied under the workspace (e.g. `.env,**/*.pem`). |
| `PATH_ALLOWLIST` | _(empty)_ | If set, only matching relative paths are allowed (deny still wins). |
| `WORKSPACE_ALLOWED_ROOTS` | _(empty)_ | Absolute roots permitted for `inPlace` / AG-UI `workspacePath` (e.g. `/app/sessions`). |
| `RESUME_CLAIM_TIMEOUT_SEC` | `300` | Restore stale in-progress resume claims to awaiting state so another instance can retry. |
| `RUN_HEARTBEAT_INTERVAL_SEC` | `5` | Interval for `ExecutionRun` heartbeat updates while a segment is active. Clamped to ≥1. |
| `RUN_HEARTBEAT_STALE_SEC` | `300` | Runs with no heartbeat newer than this are stale for close or claim reconciliation. Clamped to > interval. |
| `RUN_SEGMENT_DEADLINE_SEC` | `270` | Absolute deadline for the entire model/tool segment. Clamped to ≥1. |
| `RUN_CANCELLATION_WARN_SEC` | `5` | Structured diagnostic threshold for a worker that is slow to stop after cancellation. Clamped to ≥1. |
| `CLOSE_WAIT_TIMEOUT_SEC` | `10` | Max wait after marking close before returning **202** `closing`. Clamped to ≥1. Retry close or call reconcile after **202**. |
| `SHELL_COMMAND_DENYLIST` | _(empty)_ | Comma-separated regexes; matching `execute_local` commands are blocked. |
| `SHELL_COMMAND_ALLOWLIST` | _(empty)_ | If set, command must match at least one regex. |
| `MULTIMODAL_ENABLED` | `true` | Attach images/PDFs from `read_file_local` as LiteLLM content parts. |
| `MULTIMODAL_MAX_BYTES` | `5242880` (5 MiB) | Max file size to attach. |
| `MULTIMODAL_MODEL_ALLOWLIST` | _(empty)_ | If set, only model ids containing these substrings get attachments. |
| `MULTIMODAL_MODEL_DENYLIST` | `gpt-3.5,gpt-3.5-turbo,text-embedding,…` | Model-id substrings that never get attachments. |
| `MULTIMODAL_VISION_MARKERS` | `gpt-4o,claude-*,gemini,…` | When allowlist is empty, model must match one of these to attach. |

### Multimodal file reads

`read_file_local` on images (png/jpg/gif/webp/…) or PDFs returns a **small JSON
marker** (`multimodal: true`) — not base64 in the tool string. Before the next
LiteLLM call the hub appends a `user` message with `image_url` (images) or
`file` (PDFs) data-URI parts. Attachment is skipped for non-vision models
(heuristic on model id) or when the file exceeds `MULTIMODAL_MAX_BYTES`.

### Hooks (executed)

When `HOOKS_ENABLED=true`, the harness loads command hooks from:

1. `.cursor/hooks.json` (Cursor schema), else
2. `.claude/hooks.json` / `.claude/settings.json` (Claude nested or flat schema)

Events wired today: `sessionStart`, `sessionEnd`, `beforeSubmitPrompt`,
`preToolUse`, `postToolUse`, `postToolUseFailure`, `beforeShellExecution`,
`afterShellExecution`, `beforeReadFile`, `afterFileEdit`, and `preCompact`.
Hooks receive JSON on stdin and may deny via `{"permission":"deny"}` (or
`continue: false`), or request human confirmation via `{"permission":"ask"}`.
AG-UI exposes a generic `hook_permission` interrupt (no synthetic frontend tool call);
resume with `{"decision":"approve"|"deny","reason":"..."}`. Direct orchestrator
clients use the equivalent persisted resume path.
Script paths are resolved relative to the hooks config dir.
Hook configuration is reopened through the provider-neutral workspace reader at
execution time. Symlinks and non-regular files are refused, input is bounded to
64,000 characters, and rejected configuration executes no hooks. Collection
surfaces the rejection in manifest notes; execution-time loading logs it and
continues with no hooks.

A hook file that is itself a symlink retains an `unreadable or unsafe`
diagnostic because it directly redirects executable configuration. When an
ancestor directory is the rejected symlink, the one symlink warning and
count-only manifest note replace downstream hook-probe warnings.

This containment applies to reading the configuration, not to sandboxing what a
hook may run: hook commands are arbitrary shell by definition and may name
absolute paths. Manifest notes are a discovery-time snapshot, not an execution
contract; runtime loading intentionally re-reads the configuration, so safe
workspace changes made after collection take effect on the next hook event.

### Path policy (multi-tenant)

`resolve_within` still blocks `..` escape. With `PATH_POLICY_ENABLED=true`:

- File tools honor `PATH_DENYLIST` / `PATH_ALLOWLIST`.
- `git_add` / `git_diff` path args are validated the same way.
- In-place workspace binding is limited to `WORKSPACE_ALLOWED_ROOTS` when set
  (recommended for compose: `/app/sessions`).
- `execute_local` is filtered by `SHELL_COMMAND_*` lists **and**
  `beforeShellExecution` hooks.

---

## 12. Phase 3: segment bindings, lifecycle, and close

Phase 3 adds segment-scoped binding refresh, stateless multi-pod run tracking,
and explicit close endpoints. There is **no TTL worker**; caller and
`ag-ui-demo` migration to canonical close remains **Phase 4**.

### Three roots stay separate

Every workspace-backed session has three independent roots:

| Root | Location | Session close behavior |
| --- | --- | --- |
| **Input** | Provisioned sandbox or caller in-place path | Thread close never deletes in-place caller input. Orchestrator close deletes service-owned input sandboxes only. |
| **Runtime** | `WORKSPACES_ROOT/{id}/runtime` or `WORKSPACES_ROOT/threads/{sha256(threadId)}/runtime` | Removed on orchestrator or thread close. Fresh AG-UI runs may prune unreferenced offload files only. |
| **Output** | Durable provider (`shared_folder`, `azure_blob`) when bound | **Never** cleaned by session lifecycle; caller/provider owns retention. |

### Segment start: run-binding block and harness scan

At the start of each executable segment (`execute`, fresh AG-UI run, or
`resume`):

1. **Run-binding block** — `refresh_segment_run_binding()` replaces, collapses,
   or appends the tagged block (`<!-- run-binding:start -->` … `end -->`) in the
   authoritative system message **exactly once**. Malformed or nested markers fail
   with `RUN_BINDING_AMBIGUOUS`.
2. **Harness scan** — `execute` and fresh AG-UI runs with a bound workspace
   perform one normal `collect_manifest()` scan. **Resume** patches the persisted
   system message only; it does not re-scan the workspace.
3. **Credentials and backends** — Persisted `config` holds sanitized binding
   identity (no tokens). Each segment accepts fresh `credentials` on the request;
   output backends and local tools are rebuilt from persisted bindings plus those
   transient tokens.

### Input workspace recovery per segment

`ensure_input_workspace()` runs before each segment:

- **Existing service-owned sandbox** — If `execution.workspace_path` exists, use
  it; no input token required for the default copy/git/url sandbox.
- **Missing service-owned sandbox** — Reprovision from persisted `config.input`,
  then apply `relativePath`. Git inputs restore the persisted `branch` when set.
- **In-place caller input** — Bind the existing path when present; if missing,
  fail with `INPUT_WORKSPACE_MISSING` (never recreate in-place paths).
- **Azure input** — Unsupported (output-only for `azure_blob`). AG-UI new-contract
  `input` supports `shared_folder` only; git/url return `AGUI_INPUT_NOT_SUPPORTED`.

### Stateless multi-pod lifecycle

| Component | Role |
| --- | --- |
| `thread_sessions` | Full `thread_id` + SHA-256 `thread_key` (same digest as runtime path). |
| `execution_runs` | One row per executable segment; heartbeat, `active`/`closing`/`completed`/`failed`. |
| Eager execution records | AG-UI creates an `Execution` row at run start (not only on await). |
| `RunRegistry` | Pod-local task cancellation optimization only; not required for correctness. |
| DB | Source of truth for close, resume, and concurrency. |

Concurrent segments for the same execution or thread are rejected (**409**) with
`RUN_CONFLICT`. Before that conflict check, the claim transaction conditionally
terminalizes crash-orphaned rows whose heartbeat is older than
`RUN_HEARTBEAT_STALE_SEC`; fresh rows continue to block. The stale predicate is
rechecked in the terminal update, so a concurrent fresh heartbeat wins rather
than being overwritten.

Each model call has an absolute `LITELLM_MODEL_DEADLINE_SEC` in addition to the
HTTP idle timeout, and every request carries
`LITELLM_MAX_COMPLETION_TOKENS`. The larger `RUN_SEGMENT_DEADLINE_SEC` covers
workspace/binding preparation and the complete model/tool loop. On expiry,
close, or heartbeat-loop failure, the owner cancels and awaits the worker
before terminalizing the run row. It never releases a claim while provisioning,
tool calls, file writes, or model work remain live.
Model and segment expiry return `TIMEOUT`; truncated model output returns
`OUTPUT_LIMIT`. Heartbeat failure returns `RUN_LIFECYCLE_FAILED` rather than
being mislabeled as a timeout.

The default deployment ordering is model **240s**, segment **270s**, caller
**300s**, and reverse proxy **310s** or more. This leaves time for worker
cancellation and DB finalization before the caller closes its connection.

`RUN_HEARTBEAT_STALE_SEC` must exceed `RUN_HEARTBEAT_INTERVAL_SEC` (enforced at
load). After a **202** `closing` response, retry the close endpoint or wait for
reconciliation once runs finish or go stale (within `CLOSE_WAIT_TIMEOUT_SEC` the
first call may return **202**; a later retry reaches **200** `closed`).

### Close endpoints and cleanup scope

| Endpoint | Scope |
| --- | --- |
| `POST /v1/orchestrator/{guid}/close` | Orchestrator execution: runtime + service-owned workspace. |
| `POST /api/ag-ui/threads/{threadId}/close` | Thread: cancel/discard holds, delete thread runtime, remove **AG-UI-owned** service sandboxes only. |

**Thread close never removes:** orchestrator-owned workspaces, in-place caller
input, or durable output.

**Orchestrator close never removes:** in-place caller input or durable output.

`/abandon` remains a non-destructive hold discard for current web compatibility.

Response fields: `status` (`closed`|`closing`), `alreadyClosed`, `discardedHolds`,
`runtimeDeleted`, `workspaceDeleted`, `workspaceDeletedCount`. HTTP **200** when
`closed`, **202** when `closing`.

**Caller retry contract:** clients (e.g. `ag-ui-demo`, production web gateways)
should re-POST the same close URL on **202** with bounded attempts/backoff until
**200** `closed` or a non-retryable error. The service does not implement client
session TTL — callers own when to close and retain durable output.

### Phase 4 caller adoption (demo / docs)

| Topic | Contract |
| --- | --- |
| Generic three-root separation | Input, runtime, and output roots stay independent; close never deletes in-place caller input or durable output |
| Generic output-only bindings | The binding/storage layer can represent `input: null` with output for forward compatibility, but workspace-free AG-UI does not expose local output tools; this is not a supported caller workflow |
| Demo/web binding policy (Phase 4) | Fresh runs send `input` (+ optional `output`, `mode: workflow`) only when workflow input is configured; plain chat sends no bindings; resume omits binding changes |
| Demo/web output rule | Web and `ag-ui-demo` intentionally require workflow input before they send output; output without input is invalid caller configuration |
| Local `shared_folder` output | No credentials; optional with input when `OUTPUT_BINDINGS_ENABLED=true` and URI under `WORKSPACE_ALLOWED_ROOTS` |
| Close vs abandon | **Close** (`POST .../close`): lifecycle teardown, **200**/**202** retry; never deletes in-place input or durable output. **Abandon** (`POST .../abandon`): non-destructive hold discard only |
| Production web | The calling application remains the production caller; `ag-ui-demo` is a protocol sandbox only |

Caller-side migration for optional bindings and thread close is documented in
`ag-ui-demo/README.md`. No orchestrator web-specific behavior is added in Phase 4.

### Migration and backfill

The container image runs `alembic upgrade head` before serving. Bare-metal
deployments must run it explicitly (head revision
`20250816_phase3_lifecycle`). The migration also adopts databases previously
initialized with SQLAlchemy `create_all`, including partially present Phase 3
tables and indexes:

- Widen `llm_states.thread_id` to `TEXT`; add `thread_key`, `thread_sessions`,
  `execution_runs`, execution `origin` and close timestamps.
- Backfill `thread_key` from `thread_id` or `config.runtimePath` digest where
  possible; derive historical AG-UI associations from runtime path when safe.
- **No** rewrite of existing `executions.config` JSON.

Provisioning failure still uses `workspace_manager.cleanup()` best-effort.

---

## 13. Persistence schema (summary)

The `executions` table carries the orchestration binding:

- `source` — the folder reference given to `initiate`.
- `workspace_path` — the provisioned sandbox path.
- `orchestration_type` — resolved harness type.
- `origin` — `orchestrator` or `agui`.
- `close_requested_at` / `closed_at` — Phase 3 lifecycle stamps.
- `config` — JSON: sanitized `input` / `output` (no tokens), `mode`,
  `runtimePath`, `legacyWritableInPlace`, plus `systemContext`, initiation-time
  `eagerContext` snapshot, `model`, `maxToolCalls`, `inPlace`, and `sourceKind`.
  Older rows without `input` get a synthetic binding at load time.

`llm_states` stores the serialized loop state for pause/resume, indexed by
`tool_call_id`, `thread_id`, and `thread_key`. The state payload is a
continuation snapshot: messages, schemas, pending calls, counters, workspace
binding, and provider records needed to rebuild the loop on another instance.

`execution_runs` and `thread_sessions` support Phase 3 close and concurrency.

---

## 14. End-to-end API example

```bash
# 1) Bind a repo (auto-detect type)
curl -s localhost:8000/v1/orchestrator/initiate \
  -H 'content-type: application/json' \
  -d '{"folder":"https://github.com/acme/playbook.git"}'
# -> { "orchestratorGuid": "G", "orchestrationType": "cursor", ... }

# 2) Run a coding-style prompt (grep / edit / shell / todos / task available)
curl -s localhost:8000/v1/orchestrator/execute \
  -H 'content-type: application/json' \
  -d '{"orchestratorGuid":"G","prompt":"Plan with write_todos, delegate a review via task to reviewer, then fix findings."}'
# -> completed, or awaitsResponse + stateGuid

# 3) If it asked a question, resume (from any instance)
curl -s localhost:8000/v1/orchestrator/resume \
  -H 'content-type: application/json' \
  -d '{"orchestratorGuid":"G","stateGuid":"S","toolCallId":"call_abc","result":{"answer":"proceed"}}'

# 4) Close the session when finished (200 closed, or 202 closing — retry)
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/v1/orchestrator/G/close \
  -X POST -H 'content-type: application/json'
```

AG-UI thread close:

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/api/ag-ui/threads/my-thread/close \
  -X POST -H 'content-type: application/json'
```

### Local continuity exercise (shared-folder input + output)

`scripts/orchestrator_continuity_exercise.py` is a safe-by-default helper for
local adoption testing. By default it prints the planned flow only; pass
`--run` to hit a live orchestrator and provide the host folder mounted at
`/workspaces` via `--host-shared-root` or `WORKSPACES_HOST_DIR`. It provisions
in-place shared-folder input
and durable shared-folder output, optionally simulates execute output writes
(`--simulate-execute`) when no LLM is configured, closes each session with 202
retry handling, re-initiates with the same output root, and verifies input
immutability plus durable output continuity. Artifacts land under
`.continuity-exercise/` (gitignored).

```bash
python scripts/orchestrator_continuity_exercise.py --help
python scripts/orchestrator_continuity_exercise.py \
  --host-shared-root ../workflows \
  --run --simulate-execute
```

---

## 15. Code map and roadmap

| Module | Responsibility |
| --- | --- |
| `src/app/models/bindings.py` | Input/output location contract, session mode, transient credentials |
| `src/app/services/binding_contract.py` | Validate/sanitize bindings, URI denylist, `relativePath` confinement |
| `src/app/services/runtime_paths.py` | Service-owned `.agent` runtime; SHA-256 thread identity |
| `src/app/services/prompt_loader.py` | YAML `{{placeholder}}` templates; leftover mustache fails closed |
| `src/app/prompts/` | Binding and offload instruction templates |
| `src/app/services/workspace_manager.py` | Source classification, provisioning, path confinement, cleanup primitive |
| `src/app/services/workspace_io.py` | Provider-neutral descriptor-bound reads, path validation, entry classification |
| `src/app/services/harness/base.py` | Manifest types, eager caps, metadata extraction, common primitive discovery |
| `src/app/services/harness/{cursor,claude,generic}.py` | Adapter scoring and adapter-specific eager/catalog collection |
| `src/app/services/harness/registry.py` | Adapter selection, manifest collection, progressive-disclosure catalog, basic system prompt |
| `src/app/services/agui_messages.py` | Typed AG-UI conversion and authoritative AG-UI system prompt composition |
| `src/app/services/tool_registry.py` | Canonical tool names, collision aliases, provider resolution, resume reconstruction |
| `src/app/services/tool_hub.py` | Main model/tool loop, subagents, hooks, batching, await snapshots |
| `src/app/services/context_compaction.py` | Offload, `preCompact`, summarization, and tool-message compaction |
| `src/app/services/execution_state_service.py` | Execution/LLM-state persistence, atomic resume claims, stale-claim recovery |
| `src/app/services/binding_runtime.py` | Segment run-binding refresh, input workspace recovery, segment output backend |
| `src/app/services/run_lifecycle.py` | Thread sessions, execution runs, heartbeat, concurrency, close rejection |
| `src/app/services/run_registry.py` | Pod-local active-run task registry and cancellation |
| `src/app/services/session_close_service.py` | Close coordinator, wait/reconcile, sandbox/runtime cleanup |
| `src/app/controllers/orchestrator_controller.py` | Initiate/execute/resume/status/close HTTP contract |
| `src/app/controllers/ag_ui_controller.py` | AG-UI fresh run, interrupt/resume, snapshots, thread close/abandon |
| `alembic/versions/20250816_phase3_lifecycle_persistence.py` | Thread/run lifecycle schema and backfill |

**Present (phase 1 + 2 + 3 + harden + multimodal):** filesystem + `edit_file`,
`glob`/`grep`, `execute` shell, `write_todos`, `task` subagents, git helpers,
`ask_user`, context offload/compaction, AG-UI token streaming, Cursor/Claude/generic
harness adapters, hook execution (including `ask` + session/prompt hooks), path /
shell allow-deny policy, **multimodal image/PDF reads via LiteLLM content parts**,
**Phase 3 segment binding refresh, multi-pod run lifecycle, and close endpoints**.

**Still open (Phase 4+):**

| Gap | Notes |
| --- | --- |
| TTL / background sweeper | No automatic time-based workspace eviction; callers use close or operator cleanup. |
| Web / demo migration | Phase 4 complete: callers and `ag-ui-demo` use retryable `/close` for terminal lifecycle; `/abandon` remains hold recovery only. |
| Broader hook coverage | Tab/app hooks (`beforeTabFileRead`, `workspaceOpen`) and MCP-specific hook events not wired (IDE-oriented; low priority for this harness). |
| Context diagnostics | No endpoint currently reports the exact loaded eager files, catalog, tools, and context budget for a run. |
