#!/usr/bin/env bash
# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

# Start the SHARED STACK on macOS (postgres + litellm + host Ollama via proxy).
# Run this FIRST, then start the orchestrator with ./start-agent-mac.sh.
#
#   ./start-shared-mac.sh          # up -d (default)
#   ./start-shared-mac.sh down     # stop the shared stack
#   ./start-shared-mac.sh logs -f  # tail logs
#
# Uses public images only (no git auth). Reads keys/ports from .env.
#
# Do not run this while another Postgres or LiteLLM already owns host ports
# 5432 / 4000 — point the orchestrator at those instead (--no-shared).
set -euo pipefail
cd "$(dirname "$0")"

COMPOSE=(docker compose -f docker-compose.shared.yml -f docker-compose.shared.mac.yml)

envget() { [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }

# Create the litellm admin-UI database on the shared Postgres if it's missing, so
# LiteLLM's prisma migrations succeed on first boot (the agent's own DB is created
# by init_db() at startup; LiteLLM's must exist before it starts).
ensure_litellm_db() {
  local user db
  user="$(envget POSTGRES_USER)"; user="${user:-postgres}"
  db="$(envget LITELLM_DB)"; db="${db:-litellm}"
  echo ">> waiting for shared postgres..."
  for _ in $(seq 1 30); do
    docker exec jb-ai-shared-postgres pg_isready -U "$user" >/dev/null 2>&1 && break
    sleep 1
  done
  if docker exec jb-ai-shared-postgres psql -U "$user" -tAc \
       "SELECT 1 FROM pg_database WHERE datname='$db'" 2>/dev/null | grep -q 1; then
    echo ">> litellm DB '$db' already exists"
  else
    echo ">> creating litellm DB '$db'"
    docker exec jb-ai-shared-postgres psql -U "$user" -c "CREATE DATABASE \"$db\";" >/dev/null
  fi
}

if [ "$#" -eq 0 ]; then set -- up -d; fi
CMD1="${1:-up}"

if [ "$CMD1" = "up" ] || [ "$CMD1" = "start" ] || [ "$CMD1" = "create" ]; then
  # Host Ollama must be up (Metal GPU) and bound on all interfaces so the proxy
  # container can reach it via host.docker.internal.
  if command -v ollama >/dev/null 2>&1; then
    if ! curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
      echo "!! Ollama isn't responding on 127.0.0.1:11434."
      echo "   Start it with:  OLLAMA_HOST=0.0.0.0 ollama serve"
      echo "   (or persist:    launchctl setenv OLLAMA_HOST 0.0.0.0  then restart Ollama.app)"
      exit 1
    fi
  else
    echo "!! Ollama not found. Install it:  brew install ollama"
    echo "   Then:  OLLAMA_HOST=0.0.0.0 ollama serve && ollama pull qwen2.5:7b"
    exit 1
  fi

  # Bring up Postgres first and create the litellm DB before the rest boots.
  echo ">> ${COMPOSE[*]} up -d postgres"
  "${COMPOSE[@]}" up -d postgres
  ensure_litellm_db
fi

echo ">> ${COMPOSE[*]} $*"
exec "${COMPOSE[@]}" "$@"
