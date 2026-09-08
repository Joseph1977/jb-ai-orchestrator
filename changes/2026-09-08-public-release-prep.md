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
