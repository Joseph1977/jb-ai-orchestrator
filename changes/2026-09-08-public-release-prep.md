# Public release preparation

- **Date** — 2026-09-08
- **Branch** — `feature/public-release-prep`

## What changed

**Shipped defaults are now closed rather than convenient.** CORS was hardcoded
to allow every origin *with* credentials and could not be configured at all; it
is now `CORS_ALLOWED_ORIGINS` plus `CORS_ALLOW_CREDENTIALS`, defaulting to
sending no CORS headers. `DOCS_ENABLED` withdraws `/swagger` and
`/openapi.json` together. `LOCAL_SHELL_ENABLED` and `HOOKS_ENABLED` default to
`false`. Every published Compose port binds to `127.0.0.1` unless `BIND_ADDR`
widens it.

**Three configurations are refused at startup** instead of running:
`CORS_ALLOWED_ORIGINS=*` combined with `CORS_ALLOW_CREDENTIALS=true`;
`ALLOW_INPLACE_WORKSPACE=true` with an empty `WORKSPACE_ALLOWED_ROOTS`, which
had permitted binding any path in the container; and any configuration value
still containing an unreplaced `__PLACEHOLDER__`. The placeholder check is
unanchored, so it catches markers embedded in composite values such as a
connection string, and reports variable names without their contents.

**No credential ships in a tracked file.** `LITELLM_MASTER_KEY` lost its
`sk-1234` fallback, which would have been the same known key on every
deployment; `scripts/ensure-compose-env.sh` and its PowerShell counterpart
generate one per machine and create both git-ignored environment files from
their tracked examples. `src/.env/docker/.env` is now `.env.example` and the
runtime file is ignored, so no tracked file is ever hand-edited with real
values.

Review found three more of the same kind. `LITELLM_API_KEY` still defaulted to
`sk-1234` in `config.py` and is now empty and required; the LiteLLM admin UI
password still fell back to that string and no longer does; and
`POSTGRES_PASSWORD` shipped as `postgres` in `.env.example` with a matching
Compose fallback, on a port published to the host. The launchers now generate
it too, and Compose declares both secrets with `:?` so running it directly
fails naming the variable instead of falling back.

**Configured URLs no longer reach the logs verbatim.** Malformed
`MCP_SERVER_URLS` values were logged in full, as were the configured MCP URLs,
the LiteLLM base URL, the URL in the MCP fetch-failure path, and — found by the
second pass — the LiteLLM completion URL, which was logged raw on *every* model
call rather than only at startup. Endpoints routinely carry userinfo or a query
token, and logs travel. All of them pass through `redact_url()`, which keeps
scheme, host, port and path; a value that does not parse as a URL is reported by
length only.

The coverage is behavioural: tests execute `validate_config()`,
`MCPAgentService.__init__` and a real failing connection with credentials in the
URL, and assert the secret is absent while the host survives. An AST guard over
logger calls supplements them for call sites no test reaches yet, judging the
expressions a call substitutes rather than the wording of its message; it is
what found the per-call leak.

**The bundled demo can reach the quickstart service.** CORS shipped empty in the
Docker configuration while `ag-ui-demo` serves on `http://localhost:5173`, so
the demo built and started and then had every browser request refused. The
Docker example now lists both loopback origins for the demo's port while the
code default stays empty, so anything not built from that file still starts with
no origin allowed. `SECURITY.md` and the demo README explain the split, and the
demo README names the symptom, because a CORS failure looks like a broken
service while `/isalive` still answers.

**The Windows bootstrap is fixed and covered.** It generated randomness with
`RandomNumberGenerator::Fill`, which does not exist on the .NET Framework
runtime behind the `powershell` command the `.bat` launchers invoke, so the
Windows quickstart would have failed while generating credentials. It also
wrote `.env` with PowerShell's implicit encoding — ANSI on 5.1, and a
byte-order mark under `-Encoding utf8` — which Compose would have folded into
the first variable's name. It now writes UTF-8 without BOM and LF explicitly,
restricts the file to the current account, and is covered by
`tests/test_compose_bootstrap.py` on every interpreter present, with a
`windows-latest` CI job running both PowerShells. CI also builds and tests the
bundled demo, and rejects newly introduced trailing whitespace.

That CI job immediately earned its place: it caught a third failure of the same
kind. Restricting the file used `Get-Acl`, which lives in
`Microsoft.PowerShell.Security`, and 5.1 cannot autoload that module when
`PSModulePath` omits the Windows PowerShell module directories. Under a `Stop`
error preference this aborted the run before either environment file existed,
so the Windows quickstart failed outright. Permissions are now set with
`icacls`, which needs no module.

The shell script sets `umask 077` for the same reason. It rewrites `.env`
through a temporary file, and a redirect obeys the caller's umask, so the
generated secret was briefly world-readable under the common `022` — as was
`.env` itself, since `mv` carries the source's mode across.

Failure there still stops the run, matching `set -e` in the shell script:
git-ignored keeps a file out of commits, not away from another account on the
same machine, so continuing with a readable credential is not an option. The
secret path restricts the file before writing rather than after — truncating an
existing file keeps its permissions — so an abort leaves the generated value in
memory and never on disk.

**Internal material removed.** The `dev-usc1`, `qa-usc1`, `sb-usc1` and
`prod-usc1` environment directories carried private gateway hostnames and are
deleted. A superseded demo planning document went with them, and a comment
naming a sibling repository was generalised.

**Line endings are normalised.** `.gitattributes` stores text as LF, pinning
`.bat`, `.cmd` and `.ps1` to CRLF on checkout. Fifteen files had been committed
with CRLF and several mixed both within one file, so editing them elsewhere
rewrote the whole file — a six-line README edit produced eighty-six changed
lines. `.git-blame-ignore-revs` keeps the normalisation commit out of blame.

**Community-health files added:** `SECURITY.md`, `CONTRIBUTING.md`,
`CODE_OF_CONDUCT.md`, issue forms and a pull request template. The README gains
a Security model section beside the quick start, a vocabulary table for the
caller-facing terms the lifecycle rules depend on, a state diagram for the
await and resume cycle, and build, Python and licence badges.

## Why

The repository is being made public. The service performs no authentication of
its own, so every default that had quietly assumed a private network was a
liability once anyone could run it — most acutely the wildcard CORS policy with
credentials enabled, which let any website call the API with a browser's
cookies, and the shell and hook execution paths that were on by default.

The documentation gap mattered as much: nothing told a stranger that a bound
playbook is executable input, how to report a vulnerability privately, or which
behavioural changes would be refused in review.

## Migration / breaking

- **Breaking.** `LOCAL_SHELL_ENABLED` and `HOOKS_ENABLED` now default to
  `false`. Deployments relying on the local shell or on project hooks must set
  them explicitly.
- **Breaking.** Cross-origin browser calls are refused until
  `CORS_ALLOWED_ORIGINS` names an origin. Previously every origin was allowed.
- **Breaking.** `ALLOW_INPLACE_WORKSPACE=true` now requires
  `WORKSPACE_ALLOWED_ROOTS` to be non-empty, and startup fails otherwise.
- **Breaking.** `LITELLM_MASTER_KEY` has no default. Compose refuses to start
  without it; the launchers generate one.
- **Breaking.** `LITELLM_API_KEY` has no default and is now required. Startup
  fails naming it, because the previous `sk-1234` default let a deployment run
  against an unintended gateway on a key every installation shared.
- **Breaking.** `POSTGRES_PASSWORD` has no default in `.env.example` and no
  Compose fallback. Existing `.env` files are untouched and keep working, since
  regenerating would orphan the data volume created with the old password. A
  deployment that relied on the fallback rather than setting the variable must
  now set it.
- The `dev-usc1`, `qa-usc1`, `sb-usc1` and `prod-usc1` environment directories
  are deleted. Deployments setting `ENV` to any of those must supply their own
  configuration directory.
- `src/.env/docker/.env` is no longer tracked. Existing checkouts keep their
  local copy; it is now ignored, and `src/.env/docker/.env.example` is the
  tracked source.
- Published Compose ports bind to loopback. Anything reaching the service from
  another host must set `BIND_ADDR`, and should put an authenticating proxy in
  front of it first.
- No Alembic revision.
