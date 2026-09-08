# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

# Windows counterpart of ensure-compose-env.sh. Prepares the two git-ignored
# environment files the Compose paths read, so a fresh clone starts in one
# command without shipping any credential.
#
#   .\.env                    compose layer: ports, shared Postgres, LiteLLM key
#   src\.env\docker\.env      the agent process inside the container
#
# Both are created from their tracked .env.example when absent, and
# LITELLM_MASTER_KEY is generated per machine. Re-running changes nothing.

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

if (-not (Test-Path '.env')) {
    Write-Host '>> creating .env from .env.example'
    Copy-Item '.env.example' '.env'
}

$dockerEnv = Join-Path 'src' (Join-Path '.env' (Join-Path 'docker' '.env'))
if (-not (Test-Path $dockerEnv)) {
    Write-Host ">> creating $dockerEnv from $dockerEnv.example"
    Copy-Item "$dockerEnv.example" $dockerEnv
}

$lines = Get-Content '.env'
$existing = $lines |
    Where-Object { $_ -match '^LITELLM_MASTER_KEY=' } |
    Select-Object -Last 1

if ($existing -and $existing -notmatch '^LITELLM_MASTER_KEY=\s*$') {
    return
}

$bytes = [byte[]]::new(32)
[System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$key = 'sk-' + (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')

if ($existing) {
    $updated = $lines | ForEach-Object {
        if ($_ -match '^LITELLM_MASTER_KEY=') { "LITELLM_MASTER_KEY=$key" } else { $_ }
    }
} else {
    $updated = $lines + "LITELLM_MASTER_KEY=$key"
}

Set-Content -Path '.env' -Value $updated
Write-Host '>> generated LITELLM_MASTER_KEY in .env'
