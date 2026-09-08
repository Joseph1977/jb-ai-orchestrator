#!/usr/bin/env bash
# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

# Prepare the two git-ignored environment files the Compose paths read, so a
# fresh clone starts in one command without shipping any credential.
#
#   ./.env                    compose layer: ports, shared Postgres, LiteLLM key
#   src/.env/docker/.env      the agent process inside the container
#
# Both are created from their tracked .env.example when absent, and every
# secret is generated per machine. Re-running changes nothing: an existing
# value is never overwritten, so the credentials keep matching the data volumes
# and containers already created from them.
#
# Called by start-shared-mac.sh and start-agent-mac.sh; the Windows launchers
# run scripts/ensure-compose-env.ps1, which does the same work.
set -euo pipefail
cd "$(dirname "$0")/.."

# Secrets to generate into ./.env, as "VARIABLE:prefix". The prefix exists
# because LiteLLM keys are conventionally sk-*; the password takes none.
SECRETS=(
  "LITELLM_MASTER_KEY:sk-"
  "POSTGRES_PASSWORD:"
)

if [ ! -f .env ]; then
  echo ">> creating .env from .env.example"
  cp .env.example .env
  chmod 600 .env
fi

if [ ! -f src/.env/docker/.env ]; then
  echo ">> creating src/.env/docker/.env from src/.env/docker/.env.example"
  cp src/.env/docker/.env.example src/.env/docker/.env
  chmod 600 src/.env/docker/.env
fi

for entry in "${SECRETS[@]}"; do
  name="${entry%%:*}"
  prefix="${entry#*:}"

  current="$(grep -E "^${name}=" .env | tail -1 | cut -d= -f2- || true)"
  if [ -n "$current" ]; then
    continue
  fi

  value="${prefix}$(openssl rand -hex 32)"
  # awk rather than sed -i: the in-place flag differs between BSD and GNU.
  # Substituting in place keeps the variable next to its explanatory comment.
  awk -v name="$name" -v value="$value" '
    $0 ~ "^" name "=" { print name "=" value; found = 1; next }
    { print }
    END { if (!found) print name "=" value }
  ' .env > .env.generated && mv .env.generated .env
  chmod 600 .env
  echo ">> generated $name in .env"
done
