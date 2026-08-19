# Shared-stack deployment for the agent (Postgres + LiteLLM + Ollama)

- **Date:** 2026-03-09
- **Branch:** `feature/shared-stack-deploy`
- **Target:** `on-going-dev`

## What changed

Running the agent under Docker previously assumed Postgres, LiteLLM and MCP were
already reachable, usually at `host.docker.internal`. `docker-compose.yml` builds
only the agent, so there was no supported way to bring up the infrastructure it
depends on from this repository.

A shared stack now exists, deployed as its own Compose project:

- `docker-compose.shared.yml` — project `jb-ai-shared` on network
  `jb-ai-shared-net`, with `postgres` (16-alpine, healthcheck, named volume) and
  `litellm` (`main-stable`, DB-backed admin UI). Container names are
  `jb-ai-shared-*`.
- `docker-compose.shared.mac.yml` — macOS: Ollama runs natively on the host for
  the Metal GPU, fronted by an nginx proxy aliased `ollama` on the shared network,
  so `api_base: http://ollama:11434` needs no change.
- `docker-compose.shared.windows.yml` — Windows/Linux: Ollama in a container, with
  a one-shot pull of the default model.
- `litellm/` — `config.yaml` and `ollama-host-proxy.conf`, carried over unchanged.

`docker-compose.yml` is untouched, so the existing standalone path behaves
identically. A new `docker-compose.stack.yml` overlay attaches the agent to the
shared network instead:

- the network is `external`, so the overlay can only ever join infrastructure;
- its name comes from `${SHARED_NET:-jb-ai-shared-net}`, so the overlay can be
  pointed at a differently named external network;
- `DATABASE_URL`, `LITELLM_BASE_URL` and `LITELLM_API_KEY` are overridden to the
  in-network service names, while everything else still comes from
  `src/.env/docker/.env`;
- the published port is replaced rather than added (`ports: !override`).

Four launchers:

- `start-shared-mac.sh` / `start-shared-windows.bat` bring the shared stack up.
  Both start Postgres first and create LiteLLM's admin-UI database if it is
  missing, because its Prisma migrations require the database to already exist.
- `start-agent-mac.sh` / `start-agent-windows.bat` start the orchestrator and
  join a running `jb-ai-shared`, else refuse and print the command — unless
  `--ensure-shared` was passed, which starts `jb-ai-shared` first. `--no-shared`
  skips the overlay and detection entirely, which is the mode for externally
  managed Postgres and LiteLLM. Remaining arguments are forwarded to
  `docker compose`.

Supporting changes: `.env.example` documents the compose layer (host ports,
`POSTGRES_*`, `LITELLM_*`, provider keys, Ollama, workspace mount) with
placeholders only, and `/.env` is git-ignored.

`readme.md` §Quick start now leads with the shared-stack path, keeps the
bring-your-own-infrastructure path as 1b, and states the resolution order and the
two-environment-file split (`.env` for the stack, `src/.env/docker/.env` for the
agent process).

## Why

The orchestrator is the only service in this repository, but it is useless
without a database and a model gateway. Making infrastructure a separate,
detected stack means one Postgres and one LiteLLM can serve several callers, and
no app stack can accidentally deploy a second copy that silently splits the data.

Conditional service deployment inside a single Compose file was considered and
rejected: Compose has no way to skip a service because something else already
provides it, and profiles would still contend for the host ports. Detection
belongs in the launcher, which is also where the "start it if absent" behaviour
can live.

## Migration or breaking notes

None for existing users. `docker-compose.yml` and `src/.env/docker/.env` are
unchanged, so `docker compose up --build -d` still works exactly as documented.
The shared stack and the overlay are additive and only used through the launchers
or an explicit second `-f`.

Verified with `docker compose config` on all four combinations (shared+mac,
shared+windows, agent+overlay, agent alone) and by inspecting the rendered
overlay: one published port, the renamed container, the overridden connection
variables, and the external network name following `SHARED_NET`. Containers were
deliberately not started as part of this change.
