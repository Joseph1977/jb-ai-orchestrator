# Agent harness landscape: Claude Code, OpenCode, Cursor, Deep Agents, and JB

Current as of **17 September 2026**. This document consolidates the comparison
in the project `readme.md` with the June 2026
[Composio Claude Code vs OpenCode article](https://composio.dev/content/claude-code-vs-open-code)
and current product documentation.

This is a product-strategy comparison, not a caller-facing contract.
`docs/ORCHESTRATOR.md` and `readme.md` remain authoritative for shipped JB
behaviour.

JB cells describe **`jb-ai-orchestrator` engine behaviour** unless they
explicitly name **`jb-web-code`**. Authentication, web-session retention, and
selected-workflow relay behaviour belong to the gateway layer.

## Executive conclusion

These products overlap, but they are not interchangeable:

| Product | Primary shape | Best fit |
| --- | --- | --- |
| **Claude Code** | Anthropic-managed coding agent across terminal, desktop, IDE, web, and SDK surfaces | Developers wanting a polished Claude-native experience, strong sandboxing, and a mature extension marketplace |
| **OpenCode** | Open-source local coding agent with TUI, desktop, IDE, and local server surfaces | Developers wanting a transparent, customisable local harness and broad provider choice |
| **Cursor Agent** | Model-flexible coding agent integrated into the Cursor IDE, CLI, and cloud execution | Developers wanting editor-native code intelligence, managed model optimisation, and local or cloud agents |
| **Deep Agents** | Embeddable Python agent harness built on LangGraph | Application teams wanting a library and middleware stack they compose themselves |
| **JB Orchestrator** | Provider-agnostic HTTP execution service with durable lifecycle, playbook discovery, and AG-UI | Applications needing one horizontally scalable service to execute many playbooks, resume persisted pauses, and restore session runnability after process loss |

The decisive JB distinction is not merely “supports many models.” It is the
separation of three independently replaceable layers:

1. **JB Orchestrator owns execution**: lifecycle, isolation boundaries,
   persistence, tool dispatch, interrupts, and playbook interpretation.
2. **LiteLLM owns provider normalisation**: the provider community adds and
   maintains model integrations without coupling them to JB releases.
3. **The model runtime remains operator-selected**: hosted APIs, enterprise
   gateways, or local models through Ollama can use the same JB contract.

LiteLLM is therefore an extensibility boundary, not a vendor lock. Operators
can point JB at another OpenAI-compatible chat-completions gateway without
changing playbooks or JB code. Replacing that HTTP contract with a
non-compatible direct provider requires LLM-client code, but not a redesign of
the lifecycle, workspace, tool, or workflow layers.

## How to read the comparison

- **Shipped** means the capability exists in the product today.
- **Partial** means it exists with a material limitation described in the cell.
- **Configurable** means the framework supplies the mechanism but the adopter
  must assemble or operate it.
- **Out of scope** means the capability belongs to another product layer, not
  that the product failed to implement its own purpose.

No single “winner” is meaningful across all rows. A local developer tool, an
embeddable library, and a multi-tenant execution service optimise for different
jobs.

## Strategic architecture

| Dimension | Claude Code | OpenCode | Cursor Agent | Deep Agents | JB Orchestrator |
| --- | --- | --- | --- | --- | --- |
| Product boundary | Managed coding product | Open local agent and server | Managed IDE, CLI, and cloud agent | Python library | Self-hosted HTTP microservice |
| Harness source | Closed product | Open source | Closed product | Open source | Open source, Apache-2.0 |
| Execution location | Local, hosted, or managed surfaces | Local sidecar by default | Local or isolated cloud VM | Application-selected backend | Operator deployment; horizontally scalable replicas |
| Primary user | Individual developer | Individual developer | Individual developer or team | Application developer | Calling application or gateway |
| Unit of work | Local project/session | Local project/session | Workspace/session/goal | Agent invocation or graph run | Bound `Execution` session or AG-UI thread; each HTTP segment is an `ExecutionRun` attempt |
| State ownership | Product-managed session and local transcripts | Disk-backed local session state | Product-managed session/checkpoints | LangGraph checkpointer/store chosen by caller | Postgres lifecycle, await snapshots, and run claims; service runtime files; optional external durable output |
| Resume topology | Local transcripts and vendor-managed hosted continuity | Disk-backed local server/session | Local resume and vendor-managed cloud agents | Configurable with LangGraph infrastructure | Postgres-backed await state; any replica can atomically claim resume, with 409 conflict and stale-claim recovery |
| Untrusted playbook as input | No; operates in the developer project | No; operates in the developer project | No; operates in the developer workspace | Caller-defined | Orchestrator `initiate`: git (including private HTTPS with transient `credentials.inputAccessToken`), archive, copy, or gated in-place binding; AG-UI accepts only `shared_folder` input |
| Durable output separate from input | Project edits | Project edits | Workspace edits/checkpoints | Backend-defined | **Configurable and off by default**: shared-folder or Azure Blob output when `OUTPUT_BINDINGS_ENABLED=true` |
| Application UI protocol | SDK/product-specific surfaces | Local HTTP server API | Product surfaces and SDK | Framework streaming for caller applications | Orchestrator REST, AG-UI SSE, legacy agent JSON, per-run frontend tools, and durable interrupts |

### Architecture judgment

- **OpenCode and JB are both inspectable and operator-controlled.** OpenCode is
  the more direct choice for modifying a local coding-agent loop; JB is the
  stronger shape for sharing an execution engine across applications and
  replicas.
- **Deep Agents is the most composable library.** JB supplies more lifecycle
  behaviour as a ready service instead of asking each adopter to assemble it.
- **Claude Code and Cursor provide the broadest managed developer experience.**
  That convenience intentionally trades away control over the complete hosted
  harness.

## Models, providers, and vendor independence

| Capability | Claude Code | OpenCode | Cursor Agent | Deep Agents | JB Orchestrator |
| --- | --- | --- | --- | --- | --- |
| Model families | Claude models, including supported enterprise hosting and gateways | 75+ providers through AI SDK/Models.dev | Multiple models hosted by providers, trusted partners, Cursor, or eligible customer-owned accounts, depending on surface | Any model integration accepted by the framework | Any model exposed by LiteLLM; initiate can store the session default and execute/resume can override it |
| Local models | No native Ollama/local-model path | Shipped | No localhost or air-gapped inference: local Chat/Agent can use BYOK or a compatible customer endpoint, but final prompt building still routes through Cursor's backend | Caller can provide a local integration such as Ollama | Shipped path: JB → LiteLLM HTTP → Ollama, or another LiteLLM-configured model |
| Enterprise/internal endpoint | Claude through Bedrock, Foundry, Google Cloud, supported gateways, and self-hosted environments | Custom OpenAI-compatible provider or plugin | Local Chat/Agent supports BYOK, Azure OpenAI, AWS Bedrock, and compatible gateways reachable from Cursor's backend; not Tab, Auto, Cloud/Background Agents, CLI, API, or SDK | Caller integration | LiteLLM route can target internal gateways or locally hosted models |
| Provider integration ownership | Anthropic | OpenCode and provider ecosystem | Cursor | LangChain/provider packages and adopter | **LiteLLM community, independently from JB** |
| Harness/provider coupling | Tight, intentionally Claude-tuned | Provider abstraction inside OpenCode | Cursor tunes the harness for supported models | Model passed into the library | **Explicit service boundary between JB and LiteLLM** |
| Per-run model selection | Claude aliases/models | Provider/model selection | User-selected supported model or eligible BYOK model in local Chat/Agent | Caller parameter | Optional `model` on execute, resume, AG-UI, and legacy paths; orchestrator sessions use persisted initiate/await defaults when omitted |
| Exact deployment control | CLI versions can be pinned; hosted harness/model revisions remain vendor-controlled | Operator can pin OpenCode and config; remote provider aliases can still drift | Local Chat/Agent may use customer-owned provider accounts through Cursor's backend; other surfaces use Cursor-supplied/routed models, and product prompt construction remains Cursor-controlled | Adopter pins application dependencies and chosen runtime | Operator pins JB image, LiteLLM version/config, and local model artifacts; remote provider aliases can still drift |
| Session-enforced execution profile | Session selects a Claude model, but users cannot freeze hosted model/harness internals | Configurable model/provider, without guaranteeing an immutable remote runtime | User-selected for local Chat/Agent, including eligible BYOK models; other surfaces remain product-managed | Caller can persist and validate a profile in graph state | **Partial**: initiate-time `model` and `maxToolCalls` are persisted and await snapshots preserve overrides; JB does not reject drift in provider route, playbook/harness revision, or policy |
| Replace provider layer without changing workflow contract | Claude-compatible enterprise gateways only | Provider system is part of the harness | **Constrained**: local Chat/Agent can use supported BYOK or compatible customer endpoints routed through Cursor's backend; other surfaces use Cursor-supplied/routed models | Yes, through application composition | **Partial**: config-only for another OpenAI-compatible gateway; a non-compatible direct provider needs LLM-client code, while playbook and lifecycle contracts can remain unchanged |

### Correct interpretation

JB is **not less provider-agnostic than OpenCode**. Both allow broad provider
choice, but their abstractions sit in different places:

- OpenCode integrates providers into a developer-facing agent product.
- JB delegates provider churn to LiteLLM and keeps the orchestration contract
  independent.
- JB and LiteLLM can evolve in parallel. A new LiteLLM or OpenAI-compatible
  provider route normally needs only gateway configuration, provided its
  messages and tool calls satisfy JB's existing client contract.
- Ollama proves that the boundary supports private, locally hosted models rather
  than only public vendor APIs.

The remaining JB gap is **reproducibility**, not lock-in. Extending the existing
persisted model/tool-budget configuration into an optional enforced execution
profile would let the service reject accidental changes to provider route,
playbook/harness revision, and policy between segments. Even an enforced profile
cannot freeze a remote vendor alias; exact model reproducibility requires an
immutable provider version or a pinned local artifact.

The Composio article's claim that OpenCode avoids model regressions because its
models are open-weight is too broad. OpenCode also connects to proprietary and
hosted providers, whose aliases and serving stacks may change. The claim is
only reliable when the operator controls and pins the actual model artifact and
runtime.

## Reliability and pinning are different dimensions

The earlier draft called OpenCode “stronger in reliability/pinning.” That was
not an accurate overall conclusion. It combined four separate concerns:

| Reliability dimension | OpenCode | JB Orchestrator | Accurate conclusion |
| --- | --- | --- | --- |
| Harness/configuration control | Operator can pin the installed OpenCode version and configuration | Operator can pin JB image, LiteLLM version, configuration, and deployment | Both are operator-controlled |
| Exact model reproducibility | Possible only when the actual model artifact/runtime is pinned; hosted aliases can drift | Same; a pinned Ollama artifact is reproducible, while hosted aliases can drift | Neither harness can freeze an external provider by itself |
| Session and run durability | Disk-backed local sessions and child sessions | Postgres await snapshots, atomic claims, run heartbeats, stale resume-claim recovery, stale run-orphan reconciliation, and cross-replica resume | JB supplies the stronger ready-made distributed lifecycle contract |
| Profile drift enforcement | Provider/model is configurable | Initiate and await state persist model/tool budget, but later overrides are accepted | JB could add optional drift rejection; this is reproducibility hardening, not missing persistence |

JB persists paused interactions and coordinates stale resume claims and
abandoned run claims through Postgres. This supports cross-replica resume and
recovery without making either mechanism dependent on the original process;
the exact lifecycle transitions remain documented in `docs/ORCHESTRATOR.md`.

## Instructions, memory, and context

| Capability | Claude Code | OpenCode | Cursor Agent | Deep Agents | JB Orchestrator |
| --- | --- | --- | --- | --- | --- |
| Root instructions | `CLAUDE.md`, imports, hierarchy, auto-memory | `AGENTS.md`; `CLAUDE.md` fallback is version-dependent and removed in documented v2 migration | Rules, nested `AGENTS.md`, user/team/project scopes | `AGENTS.md` supplied through memory configuration | Cursor and Claude Code adapters discover their layouts; generic discovers root/dotted `AGENTS.md` and README fallback, not `CLAUDE.md` |
| Scope outside project | User, managed, and parent scopes | Global and project config | Team, user, and project rules | Caller-controlled | Intentionally bounded to the supplied playbook/workspace plus caller context |
| Skills | On-demand and invocable | On-demand | Auto-discovered or explicitly invoked | Skills directories through middleware | Progressive catalog; content read on demand with workspace-safe tools |
| Commands | Slash commands and plugin commands | Custom commands | Skills can act as explicit slash commands | Application-defined | Discovered command catalog; no HTTP slash-command interpreter |
| Context compaction | Tool-result clearing and summarisation; `/compact` and diagnostics | Configurable pruning and summary with disk-backed session data; official docs do not promise an immutable pre-compaction tape | Managed context and file checkpoints | Summarisation and offload middleware | Oversized-result offload, older-turn summarisation, tool-message compaction, and `preCompact` hook |
| Trigger awareness | Product knows Claude windows | Reads provider metadata | Managed per supported model | Model/profile configuration | Token threshold available; character threshold remains default |
| Raw history retained outside active prompt | CLI JSONL transcripts and product session records; active context still compacts | Disk-backed session/messages, but immutable pre-compaction retention is not an official guarantee | Managed conversation plus file checkpoints; checkpointing is not a raw message tape | Checkpointer/store can retain state | **Partial**: continuation snapshots are durable but compact over time; no append-only raw tape, though offload files may retain full tool payloads until runtime cleanup |
| Context diagnostics | `/context` and product diagnostics | Session/server visibility | Product UI | Caller instrumentation | **Missing** `/context`-style endpoint for loaded files, catalog, tools, and budgets |
| Prompt-cache layout | Claude-optimised cache structure | Provider-dependent normalisation | Managed model-specific optimisation | Prompt-caching middleware/profile | LiteLLM/provider may cache, but JB does not explicitly segment stable and dynamic prompt sections |

JB's refusal to load user-global instructions outside the bound workspace is an
isolation feature for a shared service, not a compatibility defect. Importing a
host user's hidden memory into another caller's run would violate tenancy.
Harness discovery runs on each orchestrator execute and each fresh AG-UI
workspace binding; resume reuses and patches the persisted system prompt rather
than rescanning the workspace.

## Tools, extensions, and delegation

| Capability | Claude Code | OpenCode | Cursor Agent | Deep Agents | JB Orchestrator |
| --- | --- | --- | --- | --- | --- |
| Built-in coding tools | Files, search, shell, web, code intelligence | Files, search, shell, web, experimental LSP | Files, semantic search, shell, web, browser, code intelligence | Filesystem, tools, optional execute/REPL | Workspace-safe file/search/edit, shell, git, todos, image/PDF attachments through `read_file_local`, and output tools when bindings are enabled |
| MCP | Local and remote, with managed settings | Local and remote with OAuth and per-agent controls | Local, project, and team MCP | First-class MCP support plus caller tools | Multiple named servers, per-server fault isolation, and suffixed names when multiple or non-default servers are configured |
| Application-supplied tools | SDK/MCP | Custom tools/plugins/server | MCP and extension surfaces | First-class caller tools | **Per-run AG-UI frontend tools** on current orchestrator/AG-UI paths; legacy `/v1/agent/*` retains a global tool cache |
| Tool permissions | Rich allow/ask/deny modes and managed policy | Global/per-agent wildcard permission object | Modes, sandbox policy, hooks, enterprise rules | Tool interrupts and filesystem permissions through middleware | Default workflow mode hides/blocks input mutation; path/shell policy and local-hook permission interrupts exist, but no equivalent unified tool-wide language |
| Hook coverage | Broad lifecycle, tool, HTTP, prompt, agent, and MCP hooks | Plugins can intercept session, permissions, tools, shell environment, and compaction | Broad local agent, shell, file, MCP, subagent, and compaction hooks; cloud support is a subset | Middleware stack is replaceable/extendable | Hooks are off by default; wired session/prompt/compaction and local-tool events exist, while MCP/AG-UI bypass that wrapper and some discovered events remain unwired |
| Plugin packaging | Mature packages and marketplaces | npm/local TypeScript modules and community ecosystem | Plugins package rules, skills, agents, commands, MCP, and hooks | Python packaging/application composition | No plugin store; playbook bundles and MCP are the preferred portable boundaries |
| Subagents | Built-in and custom agents, background work, agent teams | Primary/subagents with inspectable child sessions | Built-in and custom subagents, including background/cloud execution | Synchronous and async subagents with isolated contexts | `task_local` nested loop with role discovery, bounded depth, and summary returned to parent |
| Subagent persistence | Product-managed | Child sessions are first-class and inspectable | Product-managed | Checkpointer and async facilities are configurable | **Partial**: child transcript is not a first-class durable run |
| Subagent HITL | Permissions/hooks are available, with product-specific limits | Per-agent permissions/questions, not a documented durable subagent interrupt contract | Local agent environment can prompt; cloud agents run without local Run Mode prompts | Per-subagent `interrupt_on` configuration with a checkpointer | **Missing**: subagents cannot durably pause for human input |
| Parallel tool work | Product supports parallel agents/teams | Subagents and server sessions | Parallel subagents/cloud agents | Async subagents | Model may request a batch, but JB currently executes calls serially |

JB hooks cover session/context events and local-tool execution. Extending one
consistent policy envelope across every tool provider remains a material
governance gap.

## Lifecycle, human interaction, and application integration

| Capability | Claude Code | OpenCode | Cursor Agent | Deep Agents | JB Orchestrator |
| --- | --- | --- | --- | --- | --- |
| Interactive approvals | Terminal, IDE, desktop, and web UI | TUI/desktop permission prompts | IDE/CLI permission UI | Application renders LangGraph interrupts | AG-UI frontend tools, `ask_user`, and hook-permission interrupts |
| Durable await | Product session-dependent | Local server session | Managed session/cloud agent | Requires checkpointer | Shipped in Postgres; process does not wait in memory |
| Batch interrupt resolution | Product-specific | Permission endpoint | Product-specific | Application-defined | **Partial**: AG-UI `resume[]` resolves/cancels a batch; orchestrator REST resume accepts one `toolCallId` per request |
| Resume after process loss | Local transcripts or vendor-managed hosted continuity | Disk-backed local session recovery | Local resume or vendor-managed cloud continuity | Configurable infrastructure | Postgres-backed paused interactions, stale-claim recovery, and run-orphan reconciliation work across replicas |
| Concurrent-run protection | Product-managed | Local server-managed | Product-managed | Graph/checkpointer-dependent | Per-execution and per-thread claims, heartbeat, 409 conflict, cooperative cancellation, and stale-run reconciliation |
| Failure semantics | Product-specific | Session remains available | Product-specific | Graph/application-defined | Failed segment attempts are recorded without permanently disabling the enclosing orchestrator session or AG-UI thread; only explicit lifecycle close is terminal |
| Close semantics and cleanup | Local project remains | Local project remains | Workspace remains | Backend-defined | Explicit 200/202 close retry contract; active holds are discarded, runtime/sandboxes removed, completed state and durable output preserved |
| Streaming | Native product streams | Native TUI/server events | Native IDE/CLI streams | Framework-provided streams rendered by the application | Orchestrator AG-UI can provide token SSE when streaming is enabled; orchestrator REST is blocking JSON. `jb-web-code` plain chat relays tokens, while selected workflows emit an SSE burst after JSON completion |
| Structured frontend tools | Product-owned UI | Product-owned UI | Product-owned UI | Caller-defined | Unique per-run schemas let any caller provide its own interaction components |

## Security, isolation, and governance

| Capability | Claude Code | OpenCode | Cursor Agent | Deep Agents | JB Orchestrator |
| --- | --- | --- | --- | --- | --- |
| Filesystem boundary | Local permission rules and protected paths | Project/external-directory permissions | Local sandbox policy or cloud VM | Declarative filesystem permissions and virtual backends | Descriptor-bound, symlink-safe workspace containment with configured allowed roots |
| Shell isolation | OS-level filesystem/network sandbox available | Parsed permissions; no equivalent native OS jail by default | Local sandbox configuration or isolated cloud VM | Optional sandbox backend; host execution is also possible | **Partial**: shell can be disabled and policy-limited, but `cwd` confinement is not an OS jail |
| Tool-wide policy | Permissions apply across built-ins and MCP | Permission patterns cover built-ins, custom, and MCP tools | Hooks include MCP and shell events | Middleware can cover caller tools | **Gap**: project hook enforcement currently covers local tools rather than every tool provider |
| Authentication | Anthropic/product identity | User/provider credentials | Cursor identity and team controls | Application-owned | Deliberately none in the engine; gateway is the trust boundary |
| End-user tenancy | Managed product | Primarily local user | Managed product/team | Application-owned | Workspace/run isolation exists, but the engine trusts its service caller and does not validate end-user identity on lifecycle IDs |
| Enterprise web layer | Product supplied | Desktop/server ecosystem | Product supplied | Application-built | `jb-web-code` adds optional Azure AD, gateway-scoped per-user sessions, workflow UI, and CopilotKit/AG-UI; local mode uses a shared development identity |
| Secret handling | Managed settings and credential helpers | Provider and MCP credential stores | Managed/local configuration | Application and sandbox responsibility | Transient input/output tokens are accepted per request and never persisted or logged; private HTTPS git uses `inputAccessToken` through a git auth header, while service-level credentials and managed SSH/deploy-key support are not shipped |
| Policy defaults | Safe permission prompts and sandbox options | Configurable; broad tools enabled by default | Product modes and organisation policy | Caller chooses middleware | Shell, hooks, in-place workspaces, and output bindings are off by default; the path-policy framework is on but adds no regex restrictions until configured |

For a shared execution service, JB's descriptor-based file containment and
run-to-run isolation are stronger than relying on a project working directory.
Its important remaining security gap is shell isolation. Command parsing and
allow/deny rules reduce mistakes but cannot replace an OS or container boundary.

## Operations, observability, and scale

| Capability | Claude Code | OpenCode | Cursor Agent | Deep Agents | JB Orchestrator |
| --- | --- | --- | --- | --- | --- |
| Deployment ownership | Anthropic/product distribution; managed installations can constrain versions | Operator installs local product/server | Cursor/product distribution | Application team | Operator-owned container/database; image runs Alembic before serving and bare-metal deployments must run migrations |
| Horizontal scaling | Hosted surface is vendor-managed | Local server oriented | Cloud service vendor-managed | Application/LangGraph deployment | Stateless resume through Postgres claims; live SSE/event fan-out remains per replica |
| Queue/scheduler | Product-managed | Local session server | Managed cloud agents/goals | Application-defined | No durable job queue; request-driven bounded segments |
| Persistence | Product-managed/local transcripts | Disk-backed local session data | Product-managed | Configurable checkpointer/store | Durable Postgres lifecycle and await state, plus service runtime files and optional external output |
| Cross-process event fan-out | Product-managed | Local server | Product-managed | Application-defined | **Partial**: correctness is durable, but dashboard SSE fan-out is process-local |
| Metrics/tracing | Product telemetry | Plugin/integration dependent | Product telemetry | LangSmith and application instrumentation available | **Gap**: structured logs and usage fields exist; no first-party metrics/tracing endpoint |
| Cost accounting | Subscription/API usage | Provider billing | Subscription/usage model | Application/provider | LiteLLM/provider usage; no JB-native allocation ledger |
| Lifecycle retention | Product policy | Local data controls | Product policy | Backend policy | **Gap (engine)**: no TTL sweeper; callers/operators close sessions. `jb-web-code` separately has an idle web-session cleanup job |

## JB capabilities that should remain the centre of gravity

1. **Provider abstraction as a separate service boundary.** JB focuses on
   orchestration while LiteLLM's community focuses on model/provider churn.
2. **A deployable lifecycle contract.** Initiate, execute, await, resume, fail,
   retry, cancel, and close are service behaviour rather than application glue.
3. **Replica-independent durable interaction.** Postgres-backed pauses and
   claim recovery do not depend on the original process.
4. **Playbook portability.** Cursor and Claude Code layouts are interpreted
   directly, with a generic `AGENTS.md`/README fallback for other trees.
5. **Untrusted-workspace containment.** Bounded discovery, safe path opens,
   symlink refusal, and separate input/runtime/output roots protect tenants.
6. **Application-owned UX.** AG-UI and per-run frontend tools let web, mobile,
   and enterprise callers render interaction without coupling the engine to one
   interface.
7. **Failure-safe sessions.** A failed segment records the attempt without
   permanently disabling the enclosing execution.
8. **Durable outputs.** When the operator enables output bindings, workflow
   input can remain read-only while results go to caller-owned storage.

## Gaps and recommendations

Priorities follow the engine's governing test: absence is most urgent when it
lets one run damage the system or interfere with another run. Behavioural
preferences remain caller-owned and off by default.

### P0 — isolation and policy consistency

| ID | Recommendation | Why it matters | Scope |
| --- | --- | --- | --- |
| R1 | Add an OS-level or container sandbox for `execute_local` | Shell `cwd`, parsing, timeouts, and deny rules are not a security boundary | Engine |
| R2 | Apply one explicit policy/hook envelope across local, MCP, and AG-UI tools | Consistent enforcement prevents one tool provider from bypassing controls applied to another | Engine |
| R3 | Add structured shell-command classification | AST/tree-sitter-style parsing improves policy accuracy for nested shells and compound commands; it complements rather than replaces R1 | Engine |

### P1 — reproducibility, audit, and diagnosis

| ID | Recommendation | Why it matters | Scope |
| --- | --- | --- | --- |
| R4 | Extend persisted session configuration into an optional enforced execution profile | Freeze expected model route, provider alias, JB revision, playbook revision, and policy profile; require an explicit override for drift | Engine/API |
| R5 | Preserve an immutable raw event/message tape outside compacted model context | Supports audit, debugging, replay, and subagent inspection without re-inflating prompts | Engine/storage |
| R6 | Add a `/context`-style diagnostic view | Expose loaded instructions, catalogs, tools, budgets, offloads, model route, and compaction decisions with secret redaction | Engine/API |
| R7 | Add metrics and tracing | Measure segment latency, provider calls, tool calls, conflicts, stale claims, compaction, and abandoned interactions | Operations |
| R8 | Make token-budget compaction the default or derive a model-window fraction | Absolute token budgets already exist; the character default is less portable across small local and very large hosted contexts | Engine |
| R9 | Add session/workspace retention policy and a sweeper | Prevent abandoned durable state from accumulating indefinitely | Operations |

### P2 — performance and extensibility

| ID | Recommendation | Why it matters | Scope |
| --- | --- | --- | --- |
| R10 | Structure prompts for provider cache reuse | Stable harness/rule prefixes can reduce cost and latency without coupling JB to Claude | Engine |
| R11 | Execute proven-independent read-only tool calls concurrently | Reduces latency and context churn; mutation order must remain deterministic | Engine |
| R12 | Persist subagents as inspectable child runs and allow bounded HITL | Brings delegation under the same lifecycle, audit, and recovery guarantees as parent runs | Engine/storage |
| R13 | Add deployment, playbook, and request MCP scopes with explicit precedence | Supports enterprise defaults and least privilege without adopting hidden host-user state | Engine/configuration |
| R14 | Add focused adapters when real playbook layouts require them | OpenCode, Codex, or Copilot-specific discovery should be small adapters backed by fixtures, not changes to the core loop | Harness |
| R15 | Add service-level private-git credentials and managed SSH/deploy-key authentication | Private HTTPS cloning with transient request tokens already ships; add deploy-time credentials so callers need not submit tokens, plus an explicit SSH authentication contract | Workspace provisioning |

### P3 — caller and web-product improvements

| ID | Recommendation | Why it matters | Scope |
| --- | --- | --- | --- |
| R16 | Stream selected-workflow model deltas end to end | `jb-web-code` currently turns a blocking workflow response into an SSE burst | Gateway/web |
| R17 | Complete output browse/reopen, ZIP download, and artifact retention controls | Durable filesystem output exists, but the web product lacks artifact discovery/export and output retention policy; web-session TTL already exists | Gateway/web |
| R18 | Add durable resume-history outbox/replay | Prevent a successful upstream resume from losing its gateway history row on a later database failure | Gateway |
| R19 | Unify live and restored interrupt UX and timeline persistence | Lock the composer for live holds and merge post-unlock CopilotKit messages into gateway history without another Continue | Web |

## Features not recommended for the JB engine

| Feature | Reason |
| --- | --- |
| First-party TUI, desktop shell, or theme engine | `jb-web-code`, AG-UI clients, and other callers own presentation |
| Official in-process plugin marketplace | MCP and signed playbook bundles are cleaner trust boundaries; a marketplace belongs in a gateway or catalogue service |
| Host-user global memory imported into every run | Violates workspace containment and can leak one user's context into another tenant |
| One globally preferred model or planner/executor pair | Contradicts provider neutrality; optional per-request routing is appropriate |
| Hidden behavioural limits applied to every workflow | The engine should enforce infrastructure safety, not silently rewrite product policy |
| Automatic skill-body injection without workflow or model intent | Progressive disclosure limits context cost and prompt-injection surface |

## Recommended delivery sequence

1. **R1–R3:** close shell and policy escape paths before expanding untrusted
   playbook access.
2. **R4–R9:** make execution reproducible, inspectable, measurable, and
   operationally bounded.
3. **R10–R15:** improve cost, latency, delegation, provider governance, and
   enterprise input.
4. **R16–R19:** finish the user-facing experience in `jb-web-code`.

This order deliberately does not prioritise a TUI, marketplace, or model-specific
behaviour. Those would copy the surface of developer tools while neglecting
JB's stronger service-level differentiation.

## Sources

### JB repositories

- `readme.md` — features, comparison, security model, configuration, and known
  limitations
- `docs/ORCHESTRATOR.md` — lifecycle, tools, harness adapters, persistence,
  workspace containment, hooks, context management, and roadmap
- `jb-web-code/README.md`, `docs/ARCHITECTURE.md`,
  `docs/FRONTEND_TOOLS.md`, and `docs/OPEN_ISSUES.md` — gateway, browser UX,
  authentication, AG-UI integration, and product gaps

### External product documentation

- [Claude Code features](https://code.claude.com/docs/en/features-overview),
  [permissions](https://code.claude.com/docs/en/permissions),
  [sessions](https://code.claude.com/docs/en/sessions),
  [prompt caching](https://code.claude.com/docs/en/prompt-caching), and
  [sandboxing](https://code.claude.com/docs/en/sandboxing)
- [OpenCode overview](https://opencode.ai/docs/),
  [providers](https://opencode.ai/docs/providers/),
  [permissions](https://opencode.ai/docs/permissions/),
  [plugins](https://opencode.ai/docs/plugins/), and
  [server](https://opencode.ai/docs/server/). OpenCode v1/v2 instruction
  differences are checked against the
  [v2 migration guide](https://opencode.ai/v2/docs/migrate-v1/)
- [Cursor Agent](https://cursor.com/docs/agent/overview),
  [models](https://cursor.com/docs/models),
  [bring your own API key](https://cursor.com/help/models-and-usage/api-keys),
  [rules](https://cursor.com/docs/rules),
  [hooks](https://cursor.com/docs/hooks), and
  [subagents](https://cursor.com/docs/subagents), including the documented
  distinction between local Run Modes and cloud execution. Custom
  OpenAI-compatible gateway scope is also described in
  [OpenAI's Cursor guidance](https://help.openai.com/en/articles/20001506-using-openai-models-in-cursor)
- [Deep Agents overview](https://docs.langchain.com/oss/python/deepagents/overview),
  [customisation](https://docs.langchain.com/oss/python/deepagents/customization),
  [subagents](https://docs.langchain.com/oss/python/deepagents/subagents), and
  [backends](https://docs.langchain.com/oss/python/deepagents/backends)
- [Composio: OpenCode vs Claude Code](https://composio.dev/content/claude-code-vs-open-code),
  published 11 June 2026. Pricing, model names, and subjective rankings are
  treated as a dated editorial snapshot rather than authoritative product facts.
