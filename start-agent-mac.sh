#!/usr/bin/env bash
# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

# Start the orchestrator on macOS/Linux, joined to a SHARED stack (postgres +
# litellm). Infrastructure is never deployed from here.
#
#   ./start-agent-mac.sh                   # up -d --build (default)
#   ./start-agent-mac.sh up -d             # skip rebuild
#   ./start-agent-mac.sh down              # stop the agent
#   ./start-agent-mac.sh logs -f           # tail logs
#   ./start-agent-mac.sh --ensure-shared   # start the shared stack first if absent
#   ./start-agent-mac.sh --no-shared       # standalone: no overlay, no detection
#
# Which infrastructure gets used, in order:
#   1. --no-shared            -> docker-compose.yml alone; src/.env/docker/.env
#                                decides where Postgres and LiteLLM live. Use this
#                                when they are managed elsewhere (cloud, host).
#   2. jb-ai-shared, if its network and containers are running.
#   3. otherwise -> refuse with the command to run, unless --ensure-shared was
#                   passed, in which case jb-ai-shared is started first.
#
# Anything left after our own flags is forwarded to `docker compose`.
set -euo pipefail
cd "$(dirname "$0")"

ENSURE_SHARED=0
USE_SHARED=1
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --ensure-shared) ENSURE_SHARED=1 ;;
    --no-shared)     USE_SHARED=0 ;;
    *)               ARGS+=("$arg") ;;
  esac
done
if [ "${#ARGS[@]}" -eq 0 ]; then ARGS=(up -d --build); fi
CMD1="${ARGS[0]}"

running() { docker ps --filter "name=^$1$" --filter "status=running" -q | grep -q .; }

stack_up() {  # stack_up <network> <postgres-container> <litellm-container>
  docker network inspect "$1" >/dev/null 2>&1 && running "$2" && running "$3"
}

COMPOSE=(docker compose -f docker-compose.yml)

if [ "$USE_SHARED" -eq 0 ]; then
  echo ">> standalone mode — src/.env/docker/.env decides where postgres + litellm are"
else
  COMPOSE+=(-f docker-compose.stack.yml)

  if [ "$CMD1" = "up" ] || [ "$CMD1" = "start" ] || [ "$CMD1" = "create" ]; then
    if stack_up jb-ai-shared-net jb-ai-shared-postgres jb-ai-shared-litellm; then
      export SHARED_NET=jb-ai-shared-net
      echo ">> jb-ai-shared detected — reusing postgres:5432 + litellm:4000 on jb-ai-shared-net"
    elif [ "$ENSURE_SHARED" -eq 1 ]; then
      echo ">> no shared stack running — starting jb-ai-shared first"
      ./start-shared-mac.sh up -d
      export SHARED_NET=jb-ai-shared-net
    else
      echo "!! No shared stack is running, so there is no postgres or litellm to join."
      echo "   Start one:        ./start-shared-mac.sh"
      echo "   Or do it for me:  ./start-agent-mac.sh --ensure-shared"
      echo "   Or run standalone against externally managed infra:"
      echo "                     ./start-agent-mac.sh --no-shared"
      exit 1
    fi
  else
    # down/logs/ps don't need a live stack, but the overlay still has to resolve
    # its external network, so name it anyway.
    export SHARED_NET=jb-ai-shared-net
  fi
fi

echo ">> ${COMPOSE[*]} ${ARGS[*]}"
exec "${COMPOSE[@]}" "${ARGS[@]}"
