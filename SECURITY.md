# Security policy

## Reporting a vulnerability

Please report security issues privately, not as a public issue or pull request.

- Preferred: open a [private vulnerability report](https://github.com/Joseph1977/jb-ai-orchestrator/security/advisories/new)
  through GitHub.
- Alternative: email <4public@benraz.com> with `SECURITY` in the subject.

Please include what you were running (commit or tag), the configuration that
mattered, what you observed, and what an attacker gains. A minimal reproduction
helps more than anything else.

This is a personal open-source project, not a staffed product. Expect an
acknowledgement within about a week. Please allow 90 days before public
disclosure, and tell us if you intend to disclose sooner so the timeline can be
agreed rather than discovered.

## What this service assumes

**The orchestrator performs no authentication or authorization of its own.**
Every endpoint is open to anyone who can reach the port. This is deliberate —
it is a component intended to sit behind a gateway that authenticates callers,
terminates TLS, and enforces tenancy — but it means the deployment topology *is*
the security boundary.

Treat the ability to call this API as equivalent to the trust you place in the
workspace it is pointed at, because a caller who controls the bound workspace
controls the instructions the model follows.

Consequences worth stating plainly:

- A playbook is executable input. Rules, skills and hooks discovered in a bound
  workspace shape the system prompt and, when hooks are enabled, run as shell
  commands. Binding a workspace you do not trust is equivalent to running its
  code.
- Prompt injection is not solved here, and no harness solves it. Content
  reaching the model through files or tool results can attempt to redirect the
  run. Constrain what the run can reach rather than relying on the model to
  refuse.
- Model providers receive workspace content. Whatever the run reads may be sent
  to whichever provider LiteLLM is configured to use.

## Defaults

The shipped defaults are chosen to be safe rather than convenient, and several
require an explicit opt-in before they do anything:

| Setting | Default | Why |
|---|---|---|
| `CORS_ALLOWED_ORIGINS` | empty | No cross-origin browser call is accepted until an origin is named |
| `CORS_ALLOW_CREDENTIALS` | `false` | Refused outright in combination with `*` |
| `LOCAL_SHELL_ENABLED` | `false` | Command execution is opt-in |
| `HOOKS_ENABLED` | `false` | Workspace-supplied hooks are shell commands |
| `ALLOW_INPLACE_WORKSPACE` | `false` | In-place binding writes to caller-owned paths |
| `OUTPUT_BINDINGS_ENABLED` | `false` | Durable output writes outside the sandbox |
| `BIND_ADDR` | `127.0.0.1` | Published Compose ports do not leave the host |

Those are the code defaults: they apply when nothing overrides them, and they
are what an embedding application inherits. The tracked
`src/.env/docker/.env.example` is a different thing — a working configuration
for the local quickstart — and it deliberately relaxes two of them:

- `ALLOW_INPLACE_WORKSPACE=true` with `WORKSPACE_ALLOWED_ROOTS=/workspaces`,
  because reading playbooks from a mounted folder is the point of the Docker
  path. The containment comes from the allowed root, which confines binding to
  the single directory the operator chose to mount.
- `CORS_ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173`, the
  origins the bundled `ag-ui-demo` runs on. Without them the demo builds and
  starts but every browser request to the service is refused, which reads as a
  broken quickstart rather than a policy decision. Both origins are loopback,
  and the published ports are bound to loopback, so nothing off this machine
  gains access.

Keeping the code default empty while the example names an origin is the point:
a deployment that does not use this file — an embedding application, or a
container built from your own configuration — starts closed and has to say
which origin it serves. Delete both relaxations from your copy of the file for
anything that is not the local demo.

Startup refuses to continue on two combinations that cannot be made safe:
`CORS_ALLOWED_ORIGINS=*` together with `CORS_ALLOW_CREDENTIALS=true`, and
`ALLOW_INPLACE_WORKSPACE=true` with an empty `WORKSPACE_ALLOWED_ROOTS`. It also
refuses to start while any configuration value still contains an unreplaced
`__PLACEHOLDER__`.

`DOCS_ENABLED` defaults to `true` so evaluation is frictionless. Set it to
`false` for anything reachable beyond your own machine; it withdraws `/swagger`
and `/openapi.json` together.

## Hardening a real deployment

1. Put an authenticating reverse proxy in front of it. Nothing else on this list
   matters as much.
2. Leave `BIND_ADDR` at loopback, or bind to a private interface the proxy alone
   can reach.
3. Set `DOCS_ENABLED=false`.
4. Keep `LOCAL_SHELL_ENABLED` and `HOOKS_ENABLED` off unless a specific workflow
   needs them, and then constrain them with `SHELL_COMMAND_ALLOWLIST`,
   `SHELL_COMMAND_DENYLIST` and `PATH_DENYLIST`.
5. Confine every path callers may bind with `WORKSPACE_ALLOWED_ROOTS`, and keep
   `PATH_POLICY_ENABLED=true`.
6. Give the container a dedicated Postgres role, and run it as a non-root user
   with a read-only root filesystem where your platform allows it.
7. Set per-request `maxToolCalls` and rely on `RUN_SEGMENT_DEADLINE_SEC` so one
   run cannot consume the deployment.

## Scope

In scope: authentication and authorization bypass in anything that does claim to
enforce it, workspace containment escapes, cross-conversation or cross-user data
leakage, credential disclosure in logs or error responses, and denial of service
that one run can inflict on another.

Out of scope: the absence of built-in authentication, which is documented above;
anything requiring a workspace or configuration the operator already trusts;
and model output quality, including a model being talked into an unhelpful
answer without crossing a containment boundary.
