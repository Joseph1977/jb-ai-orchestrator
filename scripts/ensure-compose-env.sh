#!/usr/bin/env bash
# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

# Prepare the two git-ignored environment files the Compose paths read, so a
# fresh clone starts in one command without shipping any credential.
#
#   ./.env                    compose layer: ports, shared Postgres, LiteLLM key
#   src/.env/docker/.env      the agent process inside the container
#
# Both are created from their tracked .env.example when absent, and
# LITELLM_MASTER_KEY is generated per machine. Re-running changes nothing.
#
# Called by start-shared-mac.sh and start-agent-mac.sh; the Windows launchers
# carry the same logic in batch.
set -euo pipefail
cd "$(dirname "$0")/.."

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

current_key="$(grep -E '^LITELLM_MASTER_KEY=' .env | tail -1 | cut -d= -f2- || true)"
if [ -z "$current_key" ]; then
  key="sk-$(openssl rand -hex 32)"
  # awk rather than sed -i: the in-place flag differs between BSD and GNU.
  awk -v k="$key" '
    /^LITELLM_MASTER_KEY=/ { print "LITELLM_MASTER_KEY=" k; found = 1; next }
    { print }
    END { if (!found) print "LITELLM_MASTER_KEY=" k }
  ' .env > .env.generated && mv .env.generated .env
  chmod 600 .env
  echo ">> generated LITELLM_MASTER_KEY in .env"
fi
