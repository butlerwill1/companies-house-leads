<#
.SYNOPSIS
    Bring up the self-hosted Langfuse stack (the eval-tracing backend).

.DESCRIPTION
    Starts Docker Desktop if its daemon is not already responding, waits for it
    to become ready, then runs `docker compose up -d` against the pinned compose
    file in ~/langfuse-server/ (which lives outside this repo -- see
    docs/LANGFUSE_SETUP.md). Finally polls the langfuse-web health endpoint.

    Idempotent: safe to run when the stack is already up.

.EXAMPLE
    powershell -NoProfile -File .\scripts\langfuse_up.ps1
#>

[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'

$composeFile = Join-Path $env:USERPROFILE 'langfuse-server\docker-compose.yaml'
$healthUrl   = 'http://localhost:3000/api/public/health'

if (-not (Test-Path $composeFile)) {
    throw "Compose file not found: $composeFile (see docs/LANGFUSE_SETUP.md)"
}

function Test-DockerReady {
    try { docker info *> $null; return $LASTEXITCODE -eq 0 } catch { return $false }
}

if (-not (Test-DockerReady)) {
    Write-Host 'Docker daemon not responding; starting Docker Desktop...'
    $dockerDesktop = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'
    if (-not (Test-Path $dockerDesktop)) {
        $dockerDesktop = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    }
    if (-not (Test-Path $dockerDesktop)) {
        throw 'Could not locate Docker Desktop.exe -- start Docker manually and re-run.'
    }
    Start-Process $dockerDesktop

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while (-not (Test-DockerReady)) {
        if ((Get-Date) -gt $deadline) {
            throw "Docker daemon did not become ready within $TimeoutSeconds seconds."
        }
        Start-Sleep -Seconds 5
    }
    Write-Host 'Docker daemon ready.'
}

Write-Host 'Starting Langfuse stack...'
docker compose -f $composeFile up -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed (exit $LASTEXITCODE)." }

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ($true) {
    try {
        $resp = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 5
        if ($resp.StatusCode -eq 200) { break }
    } catch { }
    if ((Get-Date) -gt $deadline) {
        throw "langfuse-web did not report healthy within $TimeoutSeconds seconds."
    }
    Start-Sleep -Seconds 3
}

Write-Host 'Langfuse is up: http://localhost:3000'
