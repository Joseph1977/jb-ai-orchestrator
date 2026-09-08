# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

# Windows counterpart of ensure-compose-env.sh. Prepares the two git-ignored
# environment files the Compose paths read, so a fresh clone starts in one
# command without shipping any credential.
#
#   .\.env                    compose layer: ports, shared Postgres, LiteLLM key
#   src\.env\docker\.env      the agent process inside the container
#
# Both are created from their tracked .env.example when absent, and every
# secret is generated per machine. Re-running changes nothing: an existing
# value is never overwritten, so the credentials keep matching the data volumes
# and containers already created from them.
#
# Must stay behaviourally identical to ensure-compose-env.sh;
# tests/test_compose_bootstrap.py runs both and compares the results.

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

# Windows PowerShell 5.1 does not define $IsWindows, and its absence means
# Desktop edition, which is Windows only.
$OnWindows = if ($null -eq $IsWindows) { $PSVersionTable.PSEdition -eq 'Desktop' } else { $IsWindows }

# Secrets to generate into .env, as name -> prefix. The prefix exists because
# LiteLLM keys are conventionally sk-*; the password takes none.
$Secrets = [ordered]@{
    'LITELLM_MASTER_KEY' = 'sk-'
    'POSTGRES_PASSWORD'  = ''
}

# Compose reads .env itself and does not strip a byte-order mark, so a BOM
# becomes part of the first variable's name. Set-Content cannot be trusted here:
# its default encoding is ANSI on 5.1 and UTF-8 on 7, and -Encoding utf8 still
# emits a BOM on 5.1. Write LF for the same reason: a trailing CR would become
# part of every value.
function Write-EnvFile {
    param([string]$Path, [string[]]$Lines)

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $text = ($Lines -join "`n") + "`n"
    [System.IO.File]::WriteAllText((Convert-Path $Path), $text, $utf8NoBom)
}

# These files hold generated credentials, so keep them readable only by the
# account that created them — the counterpart of chmod 600 in the shell script.
#
# Failure stops the run, as it does under set -e in the shell script: being
# git-ignored keeps a file out of commits, not away from another local account.
function Protect-EnvFile {
    param([string]$Path)

    $full = Convert-Path $Path
    if ($OnWindows) {
        # icacls.exe rather than Get-Acl/Set-Acl: those live in
        # Microsoft.PowerShell.Security, and a PSModulePath that omits the
        # Windows PowerShell module directories leaves 5.1 unable to autoload
        # them. icacls is always present and needs no module. /inheritance:r
        # drops inherited access, /grant:r replaces any rule for the principal,
        # and the SID avoids localized account names. (F) rather than (R,W) so
        # the account keeps the delete right the rewrite below and ordinary
        # cleanup need.
        $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $output = & icacls $full '/inheritance:r' '/grant:r' "*${sid}:(F)" 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "could not restrict permissions on ${Path}: $($output -join ' ')"
        }
    }
    elseif (Get-Command chmod -ErrorAction SilentlyContinue) {
        # Reached when the cross-platform test suite exercises this script.
        & chmod 600 $full
    }
}

function Read-EnvLines {
    param([string]$Path)

    $raw = [System.IO.File]::ReadAllText((Convert-Path $Path))
    return [regex]::Split($raw.TrimEnd("`n", "`r"), '\r?\n')
}

if (-not (Test-Path '.env')) {
    Write-Host '>> creating .env from .env.example'
    Copy-Item '.env.example' '.env'
    Protect-EnvFile '.env'
}

$dockerEnv = Join-Path 'src' (Join-Path '.env' (Join-Path 'docker' '.env'))
if (-not (Test-Path $dockerEnv)) {
    Write-Host ">> creating $dockerEnv from $dockerEnv.example"
    Copy-Item "$dockerEnv.example" $dockerEnv
    Protect-EnvFile $dockerEnv
}

foreach ($name in $Secrets.Keys) {
    $prefix = $Secrets[$name]
    $lines = Read-EnvLines '.env'

    $existing = $lines | Where-Object { $_ -match "^$name=" } | Select-Object -Last 1
    if ($existing -and $existing -notmatch "^$name=\s*$") {
        continue
    }

    # Create()/GetBytes() rather than the terser static Fill(): the .bat
    # launchers invoke `powershell`, which is Windows PowerShell 5.1 on .NET
    # Framework, and Fill() exists only on .NET Core.
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $value = $prefix + (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')

    if ($existing) {
        $updated = $lines | ForEach-Object {
            if ($_ -match "^$name=") { "$name=$value" } else { $_ }
        }
    } else {
        $updated = $lines + "$name=$value"
    }

    # Restrict before writing, not after: truncating an existing file keeps its
    # permissions, so this ordering means a failure here stops the run while the
    # secret is still only in memory. It also covers a .env the operator created
    # by hand, which nothing has restricted yet.
    Protect-EnvFile '.env'
    Write-EnvFile -Path '.env' -Lines $updated
    Write-Host ">> generated $name in .env"
}
